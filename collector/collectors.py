"""Read-only collectors. No credentials or deployment-specific addresses."""
import csv,glob,json,math,os,re,subprocess,time
from pathlib import Path
previous={}
OPTIONS={}
def command(args,timeout=20):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if p.returncode and not p.stdout: raise RuntimeError(p.stderr.strip()[:160] or 'command failed')
 return p.stdout
def js(args,timeout=20):return json.loads(command(args,timeout))
def metric(out,key,label,value,unit,group,entity='',warn=None,crit=None):
 try:value=float(value)
 except (ValueError,TypeError):return
 if not math.isfinite(value):return
 out[key]={'label':label,'value':value,'unit':unit,'group':group,'entity':entity,'warn':warn,'crit':crit}
def rate(key,value,now):
 old=previous.get(key);previous[key]=(now,value)
 return max(0,(value-old[1])/(now-old[0])) if old and now>old[0] else None
def host():
 m={};e={};now=time.time()
 cpu=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:]];total=sum(cpu[:8]);idle=cpu[3]+cpu[4]
 old=previous.get('cpu');previous['cpu']=(total,idle)
 if old and total>old[0]:metric(m,'host.cpu','CPU utilization',100*(1-(idle-old[1])/(total-old[0])),'%', 'Host',warn=80,crit=95)
 for line in Path('/proc/stat').read_text().splitlines():
  if not re.match(r'cpu\d+ ',line):continue
  parts=line.split();name=parts[0];v=list(map(int,parts[1:]));totalcore=sum(v[:8]);idlecore=v[3]+v[4];oldcore=previous.get(name);previous[name]=(totalcore,idlecore)
  if oldcore and totalcore>oldcore[0]:metric(m,'host.'+name,'Logical CPU '+name[3:],100*(1-(idlecore-oldcore[1])/(totalcore-oldcore[0])),'%','CPU cores',name)
 mem={a:int(b.split()[0])*1024 for a,b in (l.split(':',1) for l in Path('/proc/meminfo').read_text().splitlines())}
 metric(m,'host.memory','Memory used',100*(mem['MemTotal']-mem['MemAvailable'])/mem['MemTotal'],'%','Host',warn=85,crit=95)
 metric(m,'host.memory_gb','Memory used',(mem['MemTotal']-mem['MemAvailable'])/2**30,'GiB','Host')
 metric(m,'host.load','Load average',os.getloadavg()[0],'','Host')
 metric(m,'host.uptime','Host uptime',float(Path('/proc/uptime').read_text().split()[0])/3600,'hours','Host')
 for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
  name,raw=line.split(':');name=name.strip();v=raw.split()
  if name=='lo' or name.startswith(('veth','tap','fw','docker','br-')):continue
  for suffix,index in [('rx',0),('tx',8)]:metric(m,'net.'+name+'.'+suffix,name+' '+('receive' if suffix=='rx' else 'transmit'),rate('net.'+name+suffix,int(v[index]),now),'B/s','Network',name)
  metric(m,'net.'+name+'.errors',name+' errors',int(v[2])+int(v[10]),'errors','Network',name)
  metric(m,'net.'+name+'.drops',name+' drops',int(v[3])+int(v[11]),'drops','Network',name)
 for line in Path('/proc/diskstats').read_text().splitlines():
  v=line.split();name=v[2]
  if not re.fullmatch(r'(sd[a-z]+|nvme\d+n\d+)',name):continue
  for suffix,index in [('read',5),('write',9)]:metric(m,'io.'+name+'.'+suffix,name+' '+suffix,rate('io.'+name+suffix,int(v[index])*512,now),'B/s','Drive I/O',name)
 s=js(['sensors','-j'])
 for chip,features in s.items():
  for label,data in features.items():
   if not isinstance(data,dict):continue
   for field,value in data.items():
    if not field.endswith('_input'):continue
    kind=field.split('_')[0];unit='°C' if kind.startswith('temp') else 'RPM' if kind.startswith('fan') else 'W' if kind.startswith('power') else 'V' if kind.startswith('in') else 'A' if kind.startswith('curr') else ''
    scale=1;warn=data.get(kind+'_max');crit=data.get(kind+'_crit')
    metric(m,'sensor.'+chip+'.'+field,label,value,unit,'Temperatures' if unit=='°C' else 'Hardware sensors',chip,warn,crit)
 e['host']={'name':'Proxmox · pve','status':'running','detail':os.uname().release}
 return m,e
def ipmi():
 m={};e={};counts={}
 for row in csv.reader(command(['ipmitool','sensor','-c'],45).splitlines()):
  if len(row)<4:continue
  name,value,unit,status=row[:4];counts[name]=counts.get(name,0)+1;key=name+' '+str(counts[name]);ent=name if counts[name]==1 else key
  e['ipmi.'+key]={'name':ent,'status':status,'detail':unit+' · '+value,'group':'BMC sensors'}
  units={'degrees C':'°C','RPM':'RPM','Watts':'W','Volts':'V','Amps':'A','percent':'%'}
  if unit not in units:continue
  metric(m,'ipmi.'+key,ent,value,units[unit],'Fans & power' if unit!='degrees C' else 'Temperatures','iDRAC / BMC',row[7] if len(row)>7 and row[7]!='na' else None,row[8] if len(row)>8 and row[8]!='na' else None)
 return m,e
def drives():
 m={};e={}
 for dev in sorted(glob.glob('/dev/nvme*n1')+glob.glob('/dev/sd?')):
  try:j=js(['smartctl','-a','-j',dev],10)
  except Exception:continue
  if 'model_name' not in j:continue
  serial=j.get('serial_number',dev);model=j['model_name'];n=j.get('nvme_smart_health_information_log',{});health=j.get('smart_status',{}).get('passed');temp=j.get('temperature',{})
  e['drive.'+serial]={'name':model,'status':'healthy' if health else 'warning','detail':dev+' · '+j.get('firmware_version',''),'serial':serial,'capacity':j.get('user_capacity',{}).get('bytes'),'group':'Drives'}
  metric(m,'drive.'+serial+'.temperature',model+' temperature',temp.get('current'),'°C','Drives',serial,temp.get('op_limit_max'),temp.get('critical_limit_max'))
  for k,label,unit in [('percentage_used','Endurance used','%'),('available_spare','Available spare','%'),('media_errors','Lifetime media errors','errors'),('critical_warning','Critical warning','flags'),('warning_temp_time','Lifetime hot time','minutes')]:metric(m,'drive.'+serial+'.'+k,model+' · '+label,n.get(k),unit,'Drives',serial)
  for k,v in enumerate(n.get('temperature_sensors',[])):metric(m,'drive.'+serial+'.sensor'+str(k),model+' sensor '+str(k+1),v,'°C','Drives',serial)
  for a in j.get('ata_smart_attributes',{}).get('table',[]):
   if a['id'] in [5,187,188,197,198,199]:metric(m,'drive.'+serial+'.ata'+str(a['id']),model+' · '+a['name'].replace('_',' '),a.get('raw',{}).get('value'),'count','Drives',serial)
 return m,e
def pools():
 m={};e={}
 for line in command(['zpool','list','-Hp','-o','name,size,alloc,free,health']).splitlines():
  name,size,used,free,status=line.split('\t')
  e['pool.'+name]={'name':name,'status':status,'detail':'ZFS pool','group':'Storage'}
  metric(m,'pool.'+name+'.used',name+' pool allocated',100*int(used)/int(size),'%','Storage',name,80,90)
  metric(m,'pool.'+name+'.used_gb',name+' raw allocated',int(used)/2**30,'GiB','Storage',name)
  metric(m,'pool.'+name+'.free',name+' raw free',int(free)/2**30,'GiB','Storage',name)
 for line in command(['zfs','list','-Hp','-d','0','-o','name,used,available']).splitlines():
  name,used,free=line.split('\t');metric(m,'pool.'+name+'.available',name+' usable available',int(free)/2**30,'GiB','Storage',name)
 return m,e
def guests():
 m={};e={}
 for x in js(['pvesh','get','/cluster/resources','--type','vm','--output-format','json']):
  key=str(x['vmid']);name=x.get('name',key);status=x['status'];e['guest.'+key]={'name':name,'status':status,'detail':x['type'].upper()+' '+key,'group':'Guests'}
  metric(m,'guest.'+key+'.cpu',name+' CPU',100*x.get('cpu',0),'%','Guests',key)
  if x.get('maxmem'):metric(m,'guest.'+key+'.memory',name+' memory',100*x.get('mem',0)/x['maxmem'],'%','Guests',key,85,95)
  metric(m,'guest.'+key+'.memory_gb',name+' memory',x.get('mem',0)/2**30,'GiB','Guests',key)
 return m,e
def docker():
 m={};e={}
 lines=command(['pct','exec',str(OPTIONS.get('docker_ct_id',104)),'--','docker','stats','--no-stream','--format','{{json .}}'],25).splitlines()
 for l in lines:
  x=json.loads(l);name=x['Name'];metric(m,'docker.'+name+'.cpu',name+' CPU',x['CPUPerc'].strip('%'),'%','Docker',name)
  metric(m,'docker.'+name+'.memory',name+' memory',x['MemPerc'].strip('%'),'%','Docker',name)
 for l in command(['pct','exec',str(OPTIONS.get('docker_ct_id',104)),'--','docker','ps','-a','--format','{{json .}}'],15).splitlines():
  x=json.loads(l);e['docker.'+x['Names']]={'name':x['Names'],'status':x.get('State','unknown'),'detail':x['Status'],'group':'Docker'}
 return m,e
def ups():
 m={};e={};raw=command(['upsc',OPTIONS.get('ups_name','ups@localhost')]);d=dict(l.split(': ',1) for l in raw.splitlines() if ': ' in l)
 e['ups']={'name':d.get('ups.model','UPS'),'status':'online' if d.get('ups.status')=='OL' else d.get('ups.status','unknown'),'detail':d.get('device.serial',''),'group':'UPS'}
 for k,label,unit,scale in [('battery.charge','Battery charge','%',1),('battery.runtime','Estimated runtime','minutes',1/60),('ups.load','UPS load','%',1),('input.voltage','Input voltage','V',1),('battery.voltage','Battery voltage','V',1),('ups.realpower','UPS real power','W',1)]:
  if k in d:metric(m,'ups.'+k,label,float(d[k])*scale,unit,'UPS',d.get('ups.model','UPS'))
 return m,e
def gpu():
 m={};e={};raw=command(['nvidia-smi','--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed','--format=csv,noheader,nounits'])
 for row in csv.reader(raw.splitlines()):
  name=row[0];e['gpu']={'name':name,'status':'online','detail':'NVIDIA GPU','group':'GPU'}
  for i,key,label,unit in [(1,'temperature','GPU temperature','°C'),(2,'utilization','GPU utilization','%'),(3,'memory','GPU memory used','MiB'),(5,'power','GPU power','W'),(6,'fan','GPU fan','%')]:metric(m,'gpu.'+key,label,row[i].strip(),unit,'GPU',name)
 return m,e
COLLECTORS={'Host & Linux sensors':host,'BMC / IPMI':ipmi,'Drive SMART':drives,'ZFS':pools,'Proxmox guests':guests,'Docker':docker,'UPS':ups,'GPU':gpu}
