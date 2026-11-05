import base64,json,sys,tempfile,threading,time,unittest,urllib.error,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'dashboard'))
from server import Store,create_server
TOKEN='test-ingestion-token-'+'x'*32
PASSWORD='test-admin-password-only'
class DashboardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'history.db')
  self.server=create_server(self.store,TOKEN,PASSWORD,bind='127.0.0.1',port=0)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url='http://127.0.0.1:'+str(self.server.server_address[1])
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def call(self,path,data=None,agent=False,auth=True,action=True,action_name='settings',cookie=None,extra_headers=None):
  headers={'Content-Type':'application/json'}
  if auth:headers['Authorization']='Bearer '+TOKEN if agent else 'Basic '+base64.b64encode(('admin:'+PASSWORD).encode()).decode()
  if action:headers['X-Observatory-Action']=action_name
  if cookie:headers['Cookie']=cookie
  if extra_headers:headers.update(extra_headers)
  req=urllib.request.Request(self.url+path,headers=headers,data=None if data is None else json.dumps(data).encode())
  try:
   with urllib.request.urlopen(req) as r:
    self.last_headers=dict(r.headers);return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,None
 def payload(self,ts=None):return {'ts':ts or time.time(),'metrics':{'cpu':{'value':12,'label':'CPU','group':'Host','source':'Host'}},'sources':{'Host':{'ok':True}},'entities':{},'catalog':{'cpu':{'label':'CPU','source':'Host','group':'Host'}}}
 def test_ingest_and_admin_credentials_are_not_interchangeable(self):
  self.assertEqual(self.call('/api/current',auth=False)[0],401)
  self.assertEqual(self.call('/api/current',agent=True)[0],401)
  self.assertEqual(self.call('/api/ingest',self.payload())[0],401)
  self.assertEqual(self.call('/api/ingest',self.payload(),agent=True)[0],200)
  self.assertEqual(self.call('/api/current')[1]['metrics']['cpu']['value'],12)
 def test_selection_persists_and_rejects_disabled_values_in_queued_samples(self):
  self.call('/api/ingest',self.payload(),agent=True)
  settings={'disabled_metrics':['cpu'],'disabled_sources':[]}
  self.assertEqual(self.call('/api/settings',settings)[0],200)
  self.assertEqual(Store(self.store.path).settings(),settings)
  self.call('/api/ingest',self.payload(time.time()-30),agent=True)
  self.assertNotIn('cpu',self.call('/api/current')[1]['metrics'])
  self.assertIn('cpu',self.call('/api/current')[1]['catalog'])
  self.assertTrue(all('cpu' not in p[1] for p in self.call('/api/history?hours=1')[1]['points']))
  self.assertEqual(self.call('/api/collector-config',agent=True)[1],settings)
 def test_source_selection_and_reenable(self):
  self.call('/api/ingest',self.payload(),agent=True)
  self.call('/api/settings',{'disabled_metrics':[],'disabled_sources':['Host']})
  self.assertFalse(self.call('/api/current')[1]['metrics'])
  self.call('/api/settings',{'disabled_metrics':[],'disabled_sources':[]})
  self.assertIn('cpu',self.call('/api/current')[1]['metrics'])
 def test_old_queued_sample_does_not_replace_latest(self):
  p=self.payload();self.call('/api/ingest',p,agent=True)
  old=self.payload(p['ts']-60);old['metrics']['cpu']['value']=2
  self.call('/api/ingest',old,agent=True)
  self.assertEqual(self.call('/api/current')[1]['metrics']['cpu']['value'],12)
 def test_settings_csrf_and_validation(self):
  s={'disabled_metrics':[],'disabled_sources':[]}
  self.assertEqual(self.call('/api/settings',s,action=False)[0],403)
  self.assertEqual(self.call('/api/settings',{'disabled_metrics':'bad','disabled_sources':[]})[0],400)
  p=self.payload();p['metrics']['cpu']['value']=float('nan')
  self.assertEqual(self.call('/api/ingest',p,agent=True)[0],400)
  self.assertEqual(self.call('/api/ingest',self.payload(time.time()+3600),agent=True)[0],400)
 def test_history_retains_peaks(self):
  for i in range(365):
   p=self.payload(time.time()-400+i);p['metrics']['cpu']['value']=84 if i==2 else 33;self.store.ingest(p)
  points=self.store.history(1)['points'];self.assertLessEqual(len(points),360)
  self.assertEqual(max(p[1]['cpu'] for p in points),84)
 def test_unknown_route_and_no_shell_endpoint(self):
  self.assertEqual(self.call('/api/exec',{'command':'id'})[0],404)
  self.assertEqual(self.call('/../../etc/passwd')[0],404)
 def login(self,username='admin',password=PASSWORD):
  status,data=self.call('/api/login',{'username':username,'password':password},auth=False,action_name='login')
  self.assertEqual(status,200);return self.last_headers['Set-Cookie'].split(';')[0]
 def test_session_login_logout_and_password_change(self):
  first=self.login();second=self.login()
  self.assertIn('HttpOnly',self.last_headers['Set-Cookie']);self.assertIn('SameSite=Lax',self.last_headers['Set-Cookie'])
  payload={'username':'renamed-admin','password':'replacement-password-for-tests','current_password':PASSWORD}
  self.assertEqual(self.call('/api/account',payload,auth=False,cookie=first,action_name='account')[0],200)
  updated=self.last_headers['Set-Cookie'].split(';')[0]
  self.assertEqual(self.call('/api/me',auth=False,cookie=second)[0],401)
  self.store.bootstrap('admin',PASSWORD)
  self.assertIsNone(self.store.account(username='admin'))
  self.assertEqual(self.call('/api/me',auth=False,cookie=updated)[1]['username'],'renamed-admin')
  self.assertEqual(self.call('/api/logout',{},auth=False,cookie=updated,action_name='logout')[0],200)
  self.assertEqual(self.call('/api/me',auth=False,cookie=updated)[0],401)
 def test_viewer_permissions_and_own_account(self):
  status,data=self.call('/api/users',{'username':'viewer','password':'viewer-password-for-tests','role':'viewer','current_password':PASSWORD},action_name='users')
  self.assertEqual(status,201);cookie=self.login('viewer','viewer-password-for-tests')
  self.assertEqual(self.call('/api/users',auth=False,cookie=cookie)[0],403)
  self.assertEqual(self.call('/api/settings',{'disabled_sources':[],'disabled_metrics':[]},auth=False,cookie=cookie)[0],403)
  self.assertEqual(self.call('/api/account',{'username':'viewer-new','current_password':'viewer-password-for-tests'},auth=False,cookie=cookie,action_name='account')[0],200)
 def test_current_password_duplicate_names_and_last_admin(self):
  status,data=self.call('/api/me');uid=data['id']
  self.assertEqual(self.call('/api/account',{'username':'changed','current_password':'wrong'},action_name='account')[0],403)
  self.assertEqual(self.call('/api/users/'+uid,{'role':'viewer','current_password':PASSWORD},action_name='users')[0],400)
  self.assertEqual(self.call('/api/users',{'username':'ADMIN','password':PASSWORD,'role':'viewer','current_password':PASSWORD},action_name='users')[0],400)
 def test_preferences_are_per_user_and_validated(self):
  c=self.login();pref={'theme':'light','accent':'#aa44cc'}
  self.assertEqual(self.call('/api/preferences',pref,auth=False,cookie=c,action_name='preferences')[0],200)
  self.assertEqual(self.call('/api/me',auth=False,cookie=c)[1]['preferences'],pref)
  uid=self.store.create_account('other',PASSWORD,'viewer');self.assertEqual(self.store.preferences(uid)['theme'],'dark')
  self.assertEqual(self.call('/api/preferences',{'theme':'evil','accent':'url(x)'},action_name='preferences')[0],400)
  self.assertEqual(self.call('/api/preferences',pref,action_name='preferences',extra_headers={'Origin':'https://evil.invalid'})[0],403)
 def test_multi_host_tokens_history_and_selections_are_isolated(self):
  status,c=self.call('/api/collectors',{'id':'unraid','label':'Unraid','platform':'unraid','current_password':PASSWORD},action_name='collectors');self.assertEqual(status,201)
  self.call('/api/ingest',self.payload(),agent=True)
  headers={'Authorization':'Bearer '+c['token']};p=self.payload();p['metrics']['cpu']['value']=77
  self.assertEqual(self.call('/api/ingest?collector=unraid',p,auth=False,extra_headers=headers)[0],200)
  self.assertEqual(self.call('/api/current?collector=unraid')[1]['metrics']['cpu']['value'],77)
  self.assertEqual(self.call('/api/current')[1]['metrics']['cpu']['value'],12)
  self.assertEqual(self.call('/api/ingest',p,auth=False,extra_headers=headers)[0],401)
  self.assertEqual(self.call('/api/ingest?collector=unraid',p,agent=True)[0],401)
  self.call('/api/settings?collector=unraid',{'disabled_metrics':['cpu'],'disabled_sources':[]})
  self.assertTrue(self.call('/api/current')[1]['metrics']);self.assertFalse(self.call('/api/current?collector=unraid')[1]['metrics'])
if __name__=='__main__':unittest.main()

class MigrationTests(unittest.TestCase):
 def test_existing_history_and_selection_survive_upgrade(self):
  import sqlite3,gzip
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'history.db';ts=int(time.time());c=sqlite3.connect(path)
   c.execute('CREATE TABLE samples(ts INTEGER PRIMARY KEY,data BLOB NOT NULL)')
   c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
   c.execute('INSERT INTO samples VALUES (?,?)',(ts,gzip.compress(json.dumps({'cpu':42}).encode())))
   c.execute('INSERT INTO state VALUES (?,?)',('settings',json.dumps({'disabled_metrics':[],'disabled_sources':['UPS']})))
   c.commit();c.close();store=Store(path)
   self.assertEqual(store.history(1)['points'],[[ts,{'cpu':42}]])
   self.assertEqual(store.settings()['disabled_sources'],['UPS'])
   self.assertEqual(Store(path).history(1)['points'],[[ts,{'cpu':42}]])
