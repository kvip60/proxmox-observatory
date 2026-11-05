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
 def call(self,path,data=None,agent=False,auth=True,action=True):
  headers={'Content-Type':'application/json'}
  if auth:headers['Authorization']='Bearer '+TOKEN if agent else 'Basic '+base64.b64encode(('admin:'+PASSWORD).encode()).decode()
  if action:headers['X-Observatory-Action']='settings'
  req=urllib.request.Request(self.url+path,headers=headers,data=None if data is None else json.dumps(data).encode())
  try:
   with urllib.request.urlopen(req) as r:return r.status,json.load(r)
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
if __name__=='__main__':unittest.main()
