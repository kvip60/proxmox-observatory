"""Unprivileged dashboard and authenticated telemetry receiver; Python stdlib only."""
import base64,gzip,hmac,http.server,json,math,os,secrets,sqlite3,time,urllib.parse
from contextlib import contextmanager
from pathlib import Path

STATIC=Path(__file__).parent/'static'
MAX_BODY=2*1024*1024
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
 @contextmanager
 def connect(self):
  c=sqlite3.connect(self.path,timeout=15)
  try:
   yield c;c.commit()
  except Exception:
   c.rollback();raise
  finally:c.close()
 def get(self,key,default):
  with self.connect() as c:r=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
  return json.loads(r[0]) if r else default
 def settings(self):return self.get('settings',{'disabled_metrics':[],'disabled_sources':[]})
 def save_settings(self,obj):
  if set(obj)!={'disabled_metrics','disabled_sources'}:raise ValueError('Unexpected settings')
  for v in obj.values():
   if not isinstance(v,list) or len(v)>10000 or any(not isinstance(s,str) or len(s)>256 for s in v):raise ValueError('Invalid selection')
  obj={k:sorted(set(v)) for k,v in obj.items()}
  with self.connect() as c:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('settings',json.dumps(obj)))
  return obj
 def ingest(self,payload):
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
  settings=self.settings();disabled=set(settings['disabled_metrics']);sources=set(settings['disabled_sources'])
  metrics={k:m for k,m in payload['metrics'].items() if k not in disabled and m.get('source') not in sources}
  payload={**payload,'metrics':metrics}
  with self.connect() as c:
   c.execute('INSERT OR REPLACE INTO samples VALUES (?,?)',(int(ts),gzip.compress(json.dumps({k:m['value'] for k,m in metrics.items()}).encode())))
   latest=c.execute("SELECT value FROM state WHERE key='latest'").fetchone()
   if not latest or json.loads(latest[0]).get('ts',0)<=ts:c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('latest',json.dumps(payload)))
   cat=c.execute("SELECT value FROM state WHERE key='catalog'").fetchone();cat=json.loads(cat[0]) if cat else {}
   cat.update(payload.get('catalog',{}));cat.update({k:{x:v for x,v in m.items() if x!='value'} for k,m in payload['metrics'].items()})
   c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('catalog',json.dumps(cat)))
   c.execute('DELETE FROM samples WHERE ts<?',(int(now-self.retention*86400),))
 def current(self):
  data=self.get('latest',{'ts':0,'metrics':{},'sources':{},'entities':{}});s=self.settings()
  data['metrics']={k:m for k,m in data['metrics'].items() if k not in s['disabled_metrics'] and m.get('source') not in s['disabled_sources']}
  data['entities']={k:e for k,e in data['entities'].items() if e.get('source') not in s['disabled_sources']}
  data['settings']=s;data['catalog']=self.get('catalog',{});data['links']={'proxmox':os.environ.get('PROXMOX_URL',''),'idrac':os.environ.get('IDRAC_URL','')};return data
 def history(self,hours):
  if not math.isfinite(hours):raise ValueError('Invalid range')
  hours=min(self.retention*24,max(.1,hours));s=self.settings();disabled=set(s['disabled_metrics']);cat=self.get('catalog',{})
  disabled.update(k for k,m in cat.items() if m.get('source') in s['disabled_sources'])
  with self.connect() as c:rows=c.execute('SELECT ts,data FROM samples WHERE ts>=? ORDER BY ts',(int(time.time()-hours*3600),)).fetchall()
  step=max(1,math.ceil(len(rows)/360));points=[]
  for i in range(0,len(rows),step):
   chunk=rows[i:i+step];bucket={}
   for ts,blob in chunk:
    for k,v in json.loads(gzip.decompress(blob)).items():
     if k not in disabled:bucket.setdefault(k,[]).append(v)
   points.append([chunk[-1][0],{k:max(v) for k,v in bucket.items()}])
  return {'points':points,'aggregation':'bucket maximum','retention_days':self.retention}

def create_server(store,token,password,username='admin',bind='0.0.0.0',port=9106):
 if len(token)<32 or len(password)<16:raise ValueError('Use a token of at least 32 and password of at least 16 characters')
 class Handler(http.server.BaseHTTPRequestHandler):
  def auth(self,agent=False):
   value=self.headers.get('Authorization','');expected='Bearer '+token if agent else 'Basic '+base64.b64encode((username+':'+password).encode()).decode()
   if hmac.compare_digest(value.encode(),expected.encode()):return True
   self.send_response(401)
   if not agent:self.send_header('WWW-Authenticate','Basic realm="Observatory", charset="UTF-8"')
   self.end_headers();return False
  def send(self,data,kind='application/json',status=200):
   if kind=='application/json':data=json.dumps(data).encode()
   self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(data)
  def do_GET(self):
   p=urllib.parse.urlsplit(self.path)
   if p.path=='/healthz':return self.send({'status':'ok'})
   agent=p.path=='/api/collector-config'
   if not self.auth(agent):return
   try:
    if agent:return self.send(store.settings())
    if p.path=='/api/current':return self.send(store.current())
    if p.path=='/api/history':return self.send(store.history(float(urllib.parse.parse_qs(p.query).get('hours',['1'])[0])))
    files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
    if p.path not in files:return self.send({'error':'Not found'},status=404)
    f=STATIC/files[p.path];return self.send(f.read_bytes(),{'html':'text/html','js':'text/javascript','css':'text/css'}[f.suffix[1:]])
   except ValueError:return self.send({'error':'Invalid request'},status=400)
   except Exception:return self.send({'error':'Unable to read telemetry'},status=500)
  def do_POST(self):
   path=urllib.parse.urlsplit(self.path).path
   if path not in ['/api/ingest','/api/settings']:return self.send({'error':'Not found'},status=404)
   agent=path=='/api/ingest'
   if not self.auth(agent):return
   if not agent:
    origin=self.headers.get('Origin')
    if self.headers.get('X-Observatory-Action')!='settings' or (origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host')):return self.send({'error':'Invalid origin or action'},status=403)
   try:
    n=int(self.headers.get('Content-Length','0'))
    if not 0<n<=MAX_BODY:return self.send({'error':'Request too large or empty'},status=413)
    data=json.loads(self.rfile.read(n))
    if agent:store.ingest(data);return self.send({'accepted':True})
    return self.send(store.save_settings(data))
   except (ValueError,TypeError,KeyError):return self.send({'error':'Invalid telemetry or settings'},status=400)
   except Exception:return self.send({'error':'Unable to store data'},status=500)
  def log_message(self,fmt,*args):
   # Never log headers, credentials, payloads or query strings.
   print('%s %s'%(self.command,urllib.parse.urlsplit(self.path).path),flush=True)
 return http.server.ThreadingHTTPServer((bind,port),Handler)
if __name__=='__main__':
 store=Store(Path(os.environ.get('DATA_DIR','/data'))/'history.db',int(os.environ.get('RETENTION_DAYS','7')))
 create_server(store,read_secret('INGEST_TOKEN'),read_secret('ADMIN_PASSWORD'),os.environ.get('ADMIN_USER','admin'),port=int(os.environ.get('PORT','9106'))).serve_forever()
