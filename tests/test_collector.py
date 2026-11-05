import json,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'collector'))
import agent
class CollectorTests(unittest.TestCase):
 def test_disabled_sources_are_not_polled_and_disabled_sensors_not_sent(self):
  with tempfile.TemporaryDirectory() as d:
   token=Path(d)/'token';token.write_text('x'*40);calls=[]
   config={'dashboard_url':'http://example.invalid','token_file':str(token),'spool_dir':d+'/spool'}
   def good():return {'a':{'value':1,'label':'a'},'b':{'value':2,'label':'b'}},{}
   def never():self.fail('Disabled source was polled')
   def req(url,token,data=None):
    if data is None:return {'disabled_sources':['Off'],'disabled_metrics':['a']}
    calls.append(data);return {'accepted':True}
   with patch.dict(agent.COLLECTORS,{'On':good,'Off':never},clear=True),patch.object(agent,'request',req):agent.run(config,once=True)
   self.assertEqual(set(calls[0]['metrics']),{'b'});self.assertIn('a',calls[0]['catalog'])
   self.assertTrue(calls[0]['sources']['Off']['disabled']);self.assertFalse(list(Path(d+'/spool').glob('*')))
 def test_outage_queues_data_and_recovery_flushes(self):
  with tempfile.TemporaryDirectory() as d:
   token=Path(d)/'token';token.write_text('x'*40);config={'dashboard_url':'http://example.invalid','token_file':str(token),'spool_dir':d+'/spool'}
   def fail(*a,**k):raise OSError('offline')
   with patch.dict(agent.COLLECTORS,{},clear=True),patch.object(agent,'request',fail):agent.run(config,once=True)
   self.assertTrue(list(Path(d+'/spool').glob('*.json')))
   def recover(url,token,data=None):return {'accepted':True} if data is not None else {'disabled_sources':[],'disabled_metrics':[]}
   with patch.dict(agent.COLLECTORS,{},clear=True),patch.object(agent,'request',recover):agent.run(config,once=True)
   self.assertFalse(list(Path(d+'/spool').glob('*.json')))
 def test_cached_selection_survives_agent_restart_during_outage(self):
  with tempfile.TemporaryDirectory() as d:
   token=Path(d)/'token';token.write_text('x'*40)
   (Path(d)/'selection.json').write_text(json.dumps({'disabled_sources':['Off'],'disabled_metrics':[]}))
   config={'dashboard_url':'http://example.invalid','token_file':str(token),'spool_dir':d+'/spool'}
   def never():self.fail('Source disabled before restart was polled')
   with patch.dict(agent.COLLECTORS,{'Off':never},clear=True),patch.object(agent,'request',side_effect=OSError('offline')):
    sample=agent.run(config,once=True)
   self.assertTrue(sample['sources']['Off']['disabled'])
if __name__=='__main__':unittest.main()
