import copy,json,sys,time,unittest,xml.etree.ElementTree as E
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import generic_ptz as p
import generic_onvif as facade
from discovery import D,M2,public_report,attach_ptz
from test_discovery import plan
FIX=json.loads((Path(__file__).parent/'ptz-fixtures.json').read_text())

class Client:
 def __init__(self):self.calls=[];self.fail_move=False
 def pin(self,service,_):return service
 def call(self,service,ns,name,inner=''):
  self.calls.append((name,inner))
  if name=='ContinuousMove' and self.fail_move:raise OSError()
  if name in FIX:
   raw=FIX[name]['body']
   root=E.fromstring(raw)
   return root if root.tag.endswith('Response') else root[0] if root.tag.endswith('Body') else root.find('{'+p.S+'}Body')[0]
  return E.Element('{'+p.P+'}'+name+'Response')

def request(name,inner=''):
 return E.fromstring(f'<p:{name} xmlns:p="{p.P}" xmlns:tt="{p.TT}">{inner}</p:{name}>')

class PTZTests(unittest.TestCase):
 def setUp(self):
  self.client=Client();self.plan=p.discover(self.client,'http://192.168.1.10/ptz','Profile_0','PTZCfgToken');self.plan['profile']='OtherNativeProfile'
  self.controller=p.Controller({},self.plan,self.client);self.client.calls=[]
 def tearDown(self):self.controller.close()
 def test_native_mapping_and_bounded_move(self):
  with patch.object(self.controller,'arm') as arm:
   status,_=self.controller.response(request('ContinuousMove','<p:ProfileToken>profile1</p:ProfileToken><p:Velocity><tt:PanTilt x="0.2" y="-0.3"/></p:Velocity><p:Timeout>PT99S</p:Timeout>'),2)
   self.assertEqual(status,200);arm.assert_called_once_with(2)
   self.assertIn('OtherNativeProfile',self.client.calls[0][1]);self.assertIn('PT2S',self.client.calls[0][1])
 def test_invalid_profile_speed_zoom_and_write_rejected(self):
  for name,inner in [('ContinuousMove','<p:ProfileToken>wrong</p:ProfileToken>'),('ContinuousMove','<p:ProfileToken>profile0</p:ProfileToken><p:Velocity><tt:PanTilt x="nan" y="0"/></p:Velocity>'),('ContinuousMove','<p:ProfileToken>profile0</p:ProfileToken><p:Velocity><tt:PanTilt x="0" y="0"/><tt:Zoom x="1"/></p:Velocity>'),('SetPreset','')]:
   self.assertEqual(self.controller.response(request(name,inner),1)[0],400)
  self.assertEqual(self.client.calls,[])
 def test_lost_move_reply_still_arms_watchdog(self):
  self.client.fail_move=True
  with patch.object(self.controller,'arm') as arm:
   with self.assertRaises(OSError):self.controller.response(request('ContinuousMove','<p:ProfileToken>profile0</p:ProfileToken><p:Velocity><tt:PanTilt x="0.1" y="0"/></p:Velocity>'),1)
   arm.assert_called_once_with(1)
 def test_stop_uses_discovered_token_for_both_signals(self):
  self.assertEqual(self.controller.response(request('Stop','<p:ProfileToken>profile0</p:ProfileToken>'),1)[0],200)
  self.assertEqual([c[0] for c in self.client.calls],['ContinuousMove','Stop'])
  self.assertTrue(all('OtherNativeProfile' in c[1] for c in self.client.calls))
 def test_watchdog_retries_without_new_motion(self):
  import threading
  event=threading.Event()
  with patch.object(self.controller,'stop',side_effect=lambda:event.set() or True):
   self.controller.arm(.01);self.assertTrue(event.wait(1))
 def test_capabilities_consistent_and_disabled_view_hidden(self):
  camera=dict(name='Camera',ptz=True,discovery=plan());camera['discovery']['views'][0]['ptz']=self.plan
  for ns,operation in [(D,'GetServices'),(D,'GetCapabilities'),(M2,'GetProfiles')]:
   root=E.fromstring(f'<s:Envelope xmlns:s="{p.S}" xmlns:m="{ns}"><s:Body><m:{operation}/></s:Body></s:Envelope>')
   result=facade.response(root,camera,0,'http://192.168.1.27:8102',8602,'02:00:00:00:00:01',self.controller)[1]
   self.assertIn(b'PTZ' if operation=='GetProfiles' else b'ptz',result)
   camera['ptz']=False
   result=facade.response(root,camera,0,'http://192.168.1.27:8102',8602,'02:00:00:00:00:01',self.controller)[1]
   self.assertNotIn(b'PTZ',result);self.assertNotIn(b'/ptz_service',result);camera['ptz']=True
  self.assertNotIn('config_xml',json.dumps(public_report(camera['discovery'])))
 def test_multi_source_mapping_keeps_fixed_lens_without_ptz(self):
  views=[dict(source='fixed',label='Fixed',streams=[dict(token='fixed_profile')]),dict(source='moving',label='Moving',streams=[dict(token='ptz_profile')])]
  profiles=[dict(source='fixed',token='fixed_profile',ptz_config=''),dict(source='moving',token='ptz_profile',ptz_config='PTZCfgToken')]
  attach_ptz(self.client,'http://192.168.1.10/ptz',profiles,views,[])
  self.assertNotIn('ptz',views[0]);self.assertEqual(views[1]['ptz']['profile'],'ptz_profile')

if __name__=='__main__':unittest.main()
