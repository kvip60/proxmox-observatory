"""Unprivileged dashboard and authenticated telemetry receiver; Python stdlib only."""
import base64,gzip,hashlib,hmac,http.server,json,math,os,re,secrets,sqlite3,threading,time,urllib.parse
from contextlib import contextmanager
from http.cookies import SimpleCookie,CookieError
from pathlib import Path

STATIC=Path(__file__).parent/'static'
MAX_BODY=2*1024*1024
def password_hash(password):
 if not isinstance(password,str) or not 0<len(password)<=256:raise ValueError('Enter a password of up to 256 characters')
 salt=secrets.token_hex(16);digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()
 return 'pbkdf2_sha256$600000$'+salt+'$'+digest
def password_matches(password,encoded):
 try:
  kind,rounds,salt,digest=encoded.split('$');actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)).hex()
  return kind=='pbkdf2_sha256' and hmac.compare_digest(actual,digest)
 except (ValueError,AttributeError,TypeError):return False
def valid_username(username):
 if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{2,63}',username):raise ValueError('Username must be 3–64 letters, numbers, or . _ @ -')
 return username
def read_secret(name):
 path=os.environ.get(name+'_FILE')
 return Path(path).read_text().strip() if path else os.environ.get(name,'').strip()
class Store:
 def __init__(self,path,retention=7):
  self.path=str(path);self.retention=retention;Path(path).parent.mkdir(parents=True,exist_ok=True)
  with self.connect() as c:
   c.execute('PRAGMA journal_mode=WAL')
   c.execute('CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY,data BLOB NOT NULL)')
   c.execute('CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
   c.execute('CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,username TEXT NOT NULL COLLATE NOCASE UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN (\'admin\',\'viewer\')))')
   c.execute('CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES accounts(id),expires REAL NOT NULL)')
   c.execute('CREATE TABLE IF NOT EXISTS preferences(user_id TEXT PRIMARY KEY REFERENCES accounts(id),value TEXT NOT NULL)')
   c.execute('CREATE TABLE IF NOT EXISTS collectors(id TEXT PRIMARY KEY,label TEXT NOT NULL,platform TEXT NOT NULL,host TEXT NOT NULL,token_hash TEXT NOT NULL,created REAL NOT NULL)')
   c.execute("INSERT OR IGNORE INTO collectors VALUES ('proxmox','Proxmox','proxmox','','',?)",(time.time(),))
   c.execute('CREATE TABLE IF NOT EXISTS collector_samples(collector_id TEXT NOT NULL,ts INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(collector_id,ts))')
   if not c.execute("SELECT 1 FROM state WHERE key='multi_host_migrated'").fetchone():
    c.execute("INSERT OR IGNORE INTO collector_samples SELECT 'proxmox',ts,data FROM samples")
    c.execute("INSERT INTO state VALUES ('multi_host_migrated','true')")
 @contextmanager
 def connect(self):
  c=sqlite3.connect(self.path,timeout=15)
  c.execute('PRAGMA foreign_keys=ON')
  try:
   yield c;c.commit()
  except Exception:
   c.rollback();raise
  finally:c.close()
 def get(self,key,default):
  with self.connect() as c:r=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
  return json.loads(r[0]) if r else default
 def key(self,name,collector_id):return name if collector_id=='proxmox' else name+':'+collector_id
 def settings(self,collector_id='proxmox'):return self.get(self.key('settings',collector_id),{'disabled_metrics':[],'disabled_sources':[]})
 def save_settings(self,obj,collector_id='proxmox'):
  if set(obj)!={'disabled_metrics','disabled_sources'}:raise ValueError('Unexpected settings')
  for v in obj.values():
   if not isinstance(v,list) or len(v)>10000 or any(not isinstance(s,str) or len(s)>256 for s in v):raise ValueError('Invalid selection')
  obj={k:sorted(set(v)) for k,v in obj.items()}
  with self.connect() as c:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(self.key('settings',collector_id),json.dumps(obj)))
  return obj
 def ingest(self,payload,collector_id='proxmox'):
  if not isinstance(payload,dict) or not isinstance(payload.get('metrics'),dict):raise ValueError('Invalid payload')
  ts=payload.get('ts');now=time.time()
  if not isinstance(ts,(int,float)) or not math.isfinite(ts) or ts>now+300 or ts<now-86400:raise ValueError('Timestamp outside accepted window')
  if len(payload['metrics'])>10000:raise ValueError('Too many metrics')
  for k,m in payload['metrics'].items():
   if not isinstance(k,str) or len(k)>256 or not isinstance(m,dict):raise ValueError('Invalid metric')
   v=m.get('value')
   if not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError('Invalid numeric value')
  for name in ['sources','entities','catalog']:
   if not isinstance(payload.get(name,{}),dict):raise ValueError('Invalid '+name)
  settings=self.settings(collector_id);disabled=set(settings['disabled_metrics']);sources=set(settings['disabled_sources'])
  metrics={k:m for k,m in payload['metrics'].items() if k not in disabled and m.get('source') not in sources}
  payload={**payload,'metrics':metrics}
  with self.connect() as c:
   c.execute('INSERT OR REPLACE INTO collector_samples VALUES (?,?,?)',(collector_id,int(ts),gzip.compress(json.dumps({k:m['value'] for k,m in metrics.items()}).encode())))
   latest=c.execute('SELECT value FROM state WHERE key=?',(self.key('latest',collector_id),)).fetchone()
   if not latest or json.loads(latest[0]).get('ts',0)<=ts:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(self.key('latest',collector_id),json.dumps(payload)))
   cat=c.execute('SELECT value FROM state WHERE key=?',(self.key('catalog',collector_id),)).fetchone();cat=json.loads(cat[0]) if cat else {}
   cat.update(payload.get('catalog',{}));cat.update({k:{x:v for x,v in m.items() if x!='value'} for k,m in payload['metrics'].items()})
   c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(self.key('catalog',collector_id),json.dumps(cat)))
   c.execute('DELETE FROM collector_samples WHERE ts<?',(int(now-self.retention*86400),))
 def current(self,collector_id='proxmox'):
  data=self.get(self.key('latest',collector_id),{'ts':0,'metrics':{},'sources':{},'entities':{}});s=self.settings(collector_id)
  data['metrics']={k:m for k,m in data['metrics'].items() if k not in s['disabled_metrics'] and m.get('source') not in s['disabled_sources']}
  data['entities']={k:e for k,e in data['entities'].items() if e.get('source') not in s['disabled_sources']}
  data['settings']=s;data['catalog']=self.get(self.key('catalog',collector_id),{});data['collector_id']=collector_id;data['collectors']=self.collectors();data['links']={'proxmox':os.environ.get('PROXMOX_URL',''),'idrac':os.environ.get('IDRAC_URL','')};return data
 def history(self,hours,collector_id='proxmox'):
  if not math.isfinite(hours):raise ValueError('Invalid range')
  hours=min(self.retention*24,max(.1,hours));s=self.settings(collector_id);disabled=set(s['disabled_metrics']);cat=self.get(self.key('catalog',collector_id),{})
  disabled.update(k for k,m in cat.items() if m.get('source') in s['disabled_sources'])
  with self.connect() as c:rows=c.execute('SELECT ts,data FROM collector_samples WHERE collector_id=? AND ts>=? ORDER BY ts',(collector_id,int(time.time()-hours*3600))).fetchall()
  step=max(1,math.ceil(len(rows)/360));points=[]
  for i in range(0,len(rows),step):
   chunk=rows[i:i+step];bucket={}
   for ts,blob in chunk:
    for k,v in json.loads(gzip.decompress(blob)).items():
     if k not in disabled:bucket.setdefault(k,[]).append(v)
   points.append([chunk[-1][0],{k:max(v) for k,v in bucket.items()}])
  return {'points':points,'aggregation':'bucket maximum','retention_days':self.retention}
 def bootstrap(self,username,password):
  with self.connect() as c:
   c.execute('BEGIN IMMEDIATE')
   if c.execute('SELECT count(*) FROM accounts').fetchone()[0]==0:
    c.execute('INSERT INTO accounts VALUES (?,?,?,?)',(secrets.token_hex(16),valid_username(username),password_hash(password),'admin'))
 def account(self,username=None,user_id=None):
  with self.connect() as c:
   row=c.execute('SELECT id,username,password_hash,role FROM accounts WHERE '+('id=?' if user_id else 'username=?'),(user_id or username,)).fetchone()
  return dict(zip(['id','username','password_hash','role'],row)) if row else None
 def accounts(self):
  with self.connect() as c:rows=c.execute('SELECT id,username,role FROM accounts ORDER BY username').fetchall()
  return [dict(zip(['id','username','role'],r)) for r in rows]
 def create_account(self,username,password,role):
  valid_username(username)
  if role not in ['admin','viewer']:raise ValueError('Invalid role')
  encoded=password_hash(password);uid=secrets.token_hex(16)
  try:
   with self.connect() as c:c.execute('INSERT INTO accounts VALUES (?,?,?,?)',(uid,username,encoded,role))
  except sqlite3.IntegrityError:raise ValueError('Username already exists')
  return uid
 def update_account(self,user_id,username,new_password=None,role=None):
  valid_username(username);encoded=password_hash(new_password) if new_password else None
  try:
   with self.connect() as c:
    c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT role FROM accounts WHERE id=?',(user_id,)).fetchone()
    if not row:raise ValueError('Account not found')
    if role is not None and role not in ['admin','viewer']:raise ValueError('Invalid role')
    if row[0]=='admin' and role=='viewer' and c.execute("SELECT count(*) FROM accounts WHERE role='admin'").fetchone()[0]<=1:raise ValueError('Keep at least one administrator')
    c.execute('UPDATE accounts SET username=?,role=COALESCE(?,role) WHERE id=?',(username,role,user_id))
    if encoded:
     c.execute('UPDATE accounts SET password_hash=? WHERE id=?',(encoded,user_id));c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
  except sqlite3.IntegrityError:raise ValueError('Username already exists')
 def new_session(self,user_id):
  token=secrets.token_urlsafe(36)
  with self.connect() as c:
   c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),));c.execute('INSERT INTO sessions VALUES (?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,time.time()+43200))
  return token
 def session_user(self,token):
  with self.connect() as c:r=c.execute('SELECT user_id FROM sessions WHERE token_hash=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
  return self.account(user_id=r[0]) if r else None
 def revoke_session(self,token):
  with self.connect() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
 def preferences(self,user_id):
  with self.connect() as c:r=c.execute('SELECT value FROM preferences WHERE user_id=?',(user_id,)).fetchone()
  return json.loads(r[0]) if r else {'theme':'dark','accent':'#5de0d0'}
 def save_preferences(self,user_id,data):
  if not isinstance(data,dict) or set(data)!={'theme','accent'} or data['theme'] not in ['dark','light','midnight','system'] or not isinstance(data['accent'],str) or not re.fullmatch('#[a-fA-F0-9]{6}',data['accent']):raise ValueError('Choose a supported theme and a six-digit hex colour')
  data={'theme':data['theme'],'accent':data['accent'].lower()}
  with self.connect() as c:c.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)',(user_id,json.dumps(data)))
  return data
 def collectors(self):
  with self.connect() as c:rows=c.execute('SELECT id,label,platform,host FROM collectors ORDER BY created').fetchall()
  result=[]
  for row in rows:
   item=dict(zip(['id','label','platform','host'],row));item['last_seen']=self.get(self.key('latest',item['id']),{}).get('ts',0);result.append(item)
  return result
 def register_collector(self,cid,label,platform,host=''):
  if not isinstance(cid,str) or not re.fullmatch('[a-z][a-z0-9-]{1,39}',cid) or not isinstance(label,str) or not 1<=len(label)<=80 or platform not in ['proxmox','unraid'] or not isinstance(host,str) or len(host)>253:raise ValueError('Invalid collector details')
  token=secrets.token_urlsafe(36)
  try:
   with self.connect() as c:c.execute('INSERT INTO collectors VALUES (?,?,?,?,?,?)',(cid,label,platform,host,hashlib.sha256(token.encode()).hexdigest(),time.time()))
  except sqlite3.IntegrityError:raise ValueError('Collector ID already exists')
  return {'id':cid,'label':label,'platform':platform,'host':host,'token':token}
 def collector_exists(self,cid):
  with self.connect() as c:return bool(c.execute('SELECT 1 FROM collectors WHERE id=?',(cid,)).fetchone())
 def collector_auth(self,cid,token):
  with self.connect() as c:r=c.execute('SELECT token_hash FROM collectors WHERE id=?',(cid,)).fetchone()
  return bool(r and r[0] and hmac.compare_digest(r[0],hashlib.sha256(token.encode()).hexdigest()))

def create_server(store,token,password,username='admin',bind='0.0.0.0',port=9106):
 if len(token)<32 or not password:raise ValueError('Use a token of at least 32 and a non-empty password')
 store.bootstrap(username,password)
 attempts={};attempt_lock=threading.Lock()
 def safe_user(user):return {k:user[k] for k in ['id','username','role']}
 class Handler(http.server.BaseHTTPRequestHandler):
  def selected_collector(self):
   return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('collector',['proxmox'])[0]
  def cookie_token(self):
   try:return SimpleCookie(self.headers.get('Cookie','')).get('observatory_session').value
   except (AttributeError,ValueError,CookieError):return ''
  def cookie(self,value,clear=False):
   secure='; Secure' if os.environ.get('SESSION_COOKIE_SECURE')=='1' else ''
   return 'observatory_session='+value+'; Path=/; HttpOnly; SameSite=Lax; Max-Age='+('0' if clear else '43200')+secure
  def auth(self,agent=False):
   value=self.headers.get('Authorization','')
   if agent:
    cid=self.selected_collector()
    if cid=='proxmox' and hmac.compare_digest(value.encode(),('Bearer '+token).encode()):return True
    if value.startswith('Bearer ') and store.collector_auth(cid,value[7:]):return True
   else:
    self.user=store.session_user(self.cookie_token())
    if not self.user and value.startswith('Basic '):
     try:
      name,pw=base64.b64decode(value[6:],validate=True).decode().split(':',1);user=store.account(username=name)
      if user and password_matches(pw,user['password_hash']):self.user=user
     except (ValueError,UnicodeError):pass
    if self.user:return True
   self.send({'error':'Sign in required'},status=401);return False
  def mutation_allowed(self,action):
   origin=self.headers.get('Origin')
   if self.headers.get('X-Observatory-Action')!=action or (origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host')):
    self.send({'error':'Invalid origin or action'},status=403);return False
   return True
  def admin(self):
   if self.user['role']=='admin':return True
   self.send({'error':'Administrator access required'},status=403);return False
  def send(self,data,kind='application/json',status=200,cookie=None):
   if kind=='application/json':data=json.dumps(data).encode()
   self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','no-referrer')
   if cookie:self.send_header('Set-Cookie',cookie)
   self.end_headers();self.wfile.write(data)
  def do_GET(self):
   p=urllib.parse.urlsplit(self.path)
   if p.path=='/healthz':return self.send({'status':'ok'})
   files={'/':'index.html','/settings':'settings.html','/login':'login.html','/app.js':'app.js','/settings.js':'settings.js','/login.js':'login.js','/appearance.js':'appearance.js','/style.css':'style.css','/favicon.svg':'favicon.svg','/favicon.ico':'favicon.svg'}
   if p.path in files:
    f=STATIC/files[p.path];return self.send(f.read_bytes(),{'html':'text/html','js':'text/javascript','css':'text/css','svg':'image/svg+xml'}[f.suffix[1:]])
   agent=p.path=='/api/collector-config'
   if not self.auth(agent):return
   try:
    cid=self.selected_collector()
    if not store.collector_exists(cid):return self.send({'error':'Collector not found'},status=404)
    if agent:return self.send(store.settings(cid))
    if p.path=='/api/collectors':return self.send({'collectors':store.collectors()})
    if p.path=='/api/me':return self.send({**safe_user(self.user),'preferences':store.preferences(self.user['id'])})
    if p.path=='/api/users':
     if not self.admin():return
     return self.send({'users':store.accounts()})
    if p.path=='/api/current':return self.send({**store.current(cid),'user':safe_user(self.user),'preferences':store.preferences(self.user['id'])})
    if p.path=='/api/history':return self.send(store.history(float(urllib.parse.parse_qs(p.query).get('hours',['1'])[0]),cid))
    return self.send({'error':'Not found'},status=404)
   except ValueError:return self.send({'error':'Invalid request'},status=400)
   except Exception:return self.send({'error':'Unable to read telemetry'},status=500)
  def do_POST(self):
   path=urllib.parse.urlsplit(self.path).path
   if path not in ['/api/ingest','/api/settings','/api/login','/api/logout','/api/account','/api/users','/api/preferences','/api/collectors'] and not re.fullmatch('/api/users/[a-f0-9]{32}',path):return self.send({'error':'Not found'},status=404)
   agent=path=='/api/ingest'
   login=path=='/api/login'
   if not login and not self.auth(agent):return
   if not agent:
    action=path.split('/')[2]
    if not self.mutation_allowed(action):return
   try:
    n=int(self.headers.get('Content-Length','0'))
    if not 0<n<=MAX_BODY:return self.send({'error':'Request too large or empty'},status=413)
    data=json.loads(self.rfile.read(n))
    cid=self.selected_collector()
    if not store.collector_exists(cid):return self.send({'error':'Collector not found'},status=404)
    if agent:store.ingest(data,cid);return self.send({'accepted':True})
    if login:
     key=self.client_address[0]
     with attempt_lock:
      failed=[t for t in attempts.get(key,[]) if t>time.time()-60]
      if len(failed)>=10:return self.send({'error':'Too many attempts. Try again in one minute.'},status=429)
     user=store.account(username=data.get('username',''))
     if not user or not password_matches(data.get('password',''),user['password_hash']):
      with attempt_lock:attempts[key]=failed+[time.time()]
      return self.send({'error':'Invalid username or password'},status=401)
     with attempt_lock:attempts.pop(key,None)
     return self.send({'user':safe_user(user)},cookie=self.cookie(store.new_session(user['id'])))
    if path=='/api/logout':store.revoke_session(self.cookie_token());return self.send({'ok':True},cookie=self.cookie('',True))
    if path=='/api/settings':
     if not self.admin():return
     return self.send(store.save_settings(data,cid))
    if path=='/api/preferences':return self.send(store.save_preferences(self.user['id'],data))
    if not password_matches(data.get('current_password',''),self.user['password_hash']):return self.send({'error':'Current password is incorrect'},status=403)
    if path=='/api/account':
     store.update_account(self.user['id'],data.get('username',self.user['username']),data.get('password') or None)
     return self.send({'user':safe_user(store.account(user_id=self.user['id']))},cookie=self.cookie(store.new_session(self.user['id'])))
    if not self.admin():return
    if path=='/api/collectors':return self.send(store.register_collector(data.get('id'),data.get('label'),data.get('platform'),data.get('host','')),status=201)
    if path=='/api/users':
     uid=store.create_account(data.get('username'),data.get('password'),data.get('role','viewer'))
     return self.send({'user':safe_user(store.account(user_id=uid))},status=201)
    uid=path.rsplit('/',1)[1];target=store.account(user_id=uid)
    if not target:return self.send({'error':'Account not found'},status=404)
    store.update_account(uid,data.get('username',target['username']),data.get('password') or None,data.get('role'))
    return self.send({'user':safe_user(store.account(user_id=uid))},cookie=self.cookie(store.new_session(uid)) if uid==self.user['id'] else None)
   except (ValueError,TypeError,KeyError) as e:return self.send({'error':str(e) if not agent else 'Invalid telemetry'},status=400)
   except Exception:return self.send({'error':'Unable to store data'},status=500)
  def log_message(self,fmt,*args):
   # Never log headers, credentials, payloads or query strings.
   print('%s %s'%(self.command,urllib.parse.urlsplit(self.path).path),flush=True)
 return http.server.ThreadingHTTPServer((bind,port),Handler)
if __name__=='__main__':
 store=Store(Path(os.environ.get('DATA_DIR','/data'))/'history.db',int(os.environ.get('RETENTION_DAYS','7')))
 create_server(store,read_secret('INGEST_TOKEN'),read_secret('ADMIN_PASSWORD'),os.environ.get('ADMIN_USER','admin'),port=int(os.environ.get('PORT','9106'))).serve_forever()
