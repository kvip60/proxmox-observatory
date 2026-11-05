#!/usr/bin/env python3
"""Outbound-only Proxmox telemetry agent. No inbound server, no remote shell."""
import argparse,concurrent.futures,json,logging,os,ssl,time,urllib.error,urllib.request,urllib.parse
from pathlib import Path
from collectors import COLLECTORS,OPTIONS
log=logging.getLogger('observatory')
def request(url,token,data=None):
 headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
 req=urllib.request.Request(url,data=None if data is None else json.dumps(data).encode(),headers=headers)
 with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)
def selected(metrics,settings):
 return {k:m for k,m in metrics.items() if k not in settings.get('disabled_metrics',[]) and m.get('source') not in settings.get('disabled_sources',[])}
def run(config,once=False):
 base=config['dashboard_url'].rstrip('/');token=Path(config['token_file']).read_text().strip()
 if not base.startswith(('https://','http://')):raise ValueError('dashboard_url must use HTTP or HTTPS')
 if len(token)<32:raise ValueError('Token must have at least 32 characters')
 interval=max(15,int(config.get('interval_seconds',30)));OPTIONS.update(config.get('options',{}))
 suffix='?collector='+urllib.parse.quote(config.get('collector_id','proxmox'))
 spool=Path(config.get('spool_dir','/var/lib/proxmox-observatory/spool'));spool.mkdir(parents=True,exist_ok=True)
 selection=spool.parent/'selection.json'
 try:settings=json.loads(selection.read_text())
 except (OSError,ValueError):settings={}
 catalog={}
 while True:
  start=time.time()
  try:
   settings=request(base+'/api/collector-config'+suffix,token)
   temp=selection.with_suffix('.tmp');temp.write_text(json.dumps(settings));os.chmod(temp,0o600);temp.replace(selection)
  except Exception as e:log.warning('Settings unavailable (%s); retaining last selection',type(e).__name__)
  disabled=set(settings.get('disabled_sources',[]))|set(config.get('disabled_sources',[]))
  sample={'ts':time.time(),'metrics':{},'sources':{},'entities':{},'catalog':{}}
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
   jobs={pool.submit(fn):name for name,fn in COLLECTORS.items() if name not in disabled}
   for name in disabled:sample['sources'][name]={'ok':False,'disabled':True,'ts':0,'error':'Disabled by configuration'}
   for job in concurrent.futures.as_completed(jobs):
    name=jobs[job]
    try:
     metrics,entities=job.result()
     for k,m in metrics.items():m['source']=name;catalog[k]={a:b for a,b in m.items() if a!='value'}
     for entity in entities.values():entity['source']=name
     sample['metrics'].update(metrics);sample['entities'].update(entities);sample['sources'][name]={'ok':True,'ts':time.time()}
    except Exception as e:sample['sources'][name]={'ok':False,'ts':0,'error':'Collection failed: '+type(e).__name__};log.warning('%s unavailable (%s)',name,type(e).__name__)
  settings['disabled_sources']=list(disabled);sample['metrics']=selected(sample['metrics'],settings);sample['catalog']=catalog
  path=spool/(str(int(sample['ts']))+'.json');path.write_text(json.dumps(sample));os.chmod(path,0o600)
  # At most one day of pending telemetry. Old data is deleted, not uploaded late.
  pending=sorted(spool.glob('*.json'))
  for p in pending:
   if int(p.stem)<time.time()-86400:p.unlink()
  for p in sorted(spool.glob('*.json'))[:32]:
   try:request(base+'/api/ingest'+suffix,token,json.loads(p.read_text()));p.unlink()
   except Exception as e:log.warning('Telemetry upload unavailable (%s); queued locally',type(e).__name__);break
  if once:return sample
  time.sleep(max(1,interval-(time.time()-start)))
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--config',default='/etc/proxmox-observatory/collector.json');parser.add_argument('--once',action='store_true');a=parser.parse_args()
 logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
 run(json.loads(Path(a.config).read_text()),a.once)
