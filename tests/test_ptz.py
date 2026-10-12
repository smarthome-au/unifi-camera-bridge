import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as E
import ptz_control as p
import dual_camera as d
from test_dual_camera import request, AUTH, HEADER
from test_bridge import settings, TransportTests

FIX=json.loads(Path(__file__).with_name('ptz-fixtures.json').read_text())
def envelope(body):
 return f'<s:Envelope xmlns:s="{p.S}">{body}</s:Envelope>'.encode()

class PTZTests(unittest.TestCase):
 def setUp(self):
  self.c=settings();self.c.adapter='cam-c44he';self.c.lens='ptz';self.c.ptz=True
 def test_both_virtual_profiles_map_to_native_motor_profile(self):
  for token in ['Profile_0','Profile_1']:
   raw=request('Stop',f'<m:ProfileToken>{token}</m:ProfileToken>',p.P)
   out=p.prepare(raw)
   self.assertIn(HEADER.encode(),out)
   self.assertEqual(p.operation(out).findtext('{'+p.P+'}ProfileToken'),'Profile_0')
 def test_movement_requires_valid_velocity_and_bounds_timeout(self):
  inner='<m:ProfileToken>Profile_1</m:ProfileToken><m:Velocity><tt:PanTilt xmlns:tt="'+p.TT+'" x="0.2" y="-0.1"/></m:Velocity>'
  for suffix, expected in [('', 'PT1S'),('<m:Timeout>PT5M</m:Timeout>','PT2S')]:
   root=p.operation(p.prepare(request('ContinuousMove',inner+suffix,p.P)))
   self.assertEqual(root.findtext('{'+p.P+'}Timeout'),expected)
  for value in ['NaN','inf','1.1']:
   with self.assertRaises(ValueError):p.prepare(request('ContinuousMove',inner.replace('x="0.2"',f'x="{value}"'),p.P))
  with self.assertRaises(ValueError):p.prepare(request('AbsoluteMove','',p.P))
  with self.assertRaises(ValueError):p.prepare(request('Stop','<m:ProfileToken>other</m:ProfileToken>',p.P))
 def test_capabilities_omit_unreliable_position_and_zoom(self):
  for op in ['GetNodes','GetConfigurationOptions','GetConfigurations','GetStatus']:
   out=p.response(envelope(FIX[op]['body']))
   self.assertNotIn(b'Zoom',out);self.assertNotIn(b'Absolute',out);self.assertNotIn(b'Relative',out)
   if op=='GetStatus':self.assertNotIn(b':Position',out)
 def test_ptz_configuration_attached_to_both_qualities_only_on_ptz_lens(self):
  auth=envelope(FIX['GetProfiles']['body'])
  for lens in ['fixed','ptz']:
   self.c.lens=lens
   status,out=d.media_response(auth,request('GetProfiles'),self.c)
   root=E.fromstring(out);ptzs=root.findall('.//{'+d.M2+'}PTZ')
   self.assertEqual(len(ptzs),2 if lens=='ptz' else 0)
   for ptz in ptzs:self.assertEqual(ptz.get('token'),'PTZCfgToken')
 def test_services_and_device_capabilities_only_on_ptz_lens(self):
  for lens in ['fixed','ptz']:
   self.c.lens=lens
   out=d.device_response(envelope(FIX['GetServices']['body']),'GetServices',self.c)
   root=E.fromstring(out);names=[e.text for e in root.findall('.//{'+d.D+'}Namespace')]
   self.assertEqual(p.P in names,lens=='ptz')
 def test_encoder_query_respects_configuration_filter(self):
  auth=envelope(FIX['GetProfiles']['body'])
  query=request('GetProfiles','<m:Type>VideoEncoder</m:Type><m:Type>AudioEncoder</m:Type>')
  for lens in ['ptz','fixed']:
   self.c.lens=lens
   status,out=d.media_response(auth,query,self.c)
   self.assertEqual(status,200)
   self.assertEqual(len(E.fromstring(out).findall('.//{'+d.M2+'}PTZ')),0)

class PTZTransportTests(TransportTests):
 def test_fixed_lens_cannot_forward_ptz_and_auth_failure_preserved(self):
  self.bridge.config.adapter='cam-c44he';self.bridge.config.lens='fixed';self.bridge.config.ptz=True
  raw=request('Stop','<m:ProfileToken>Profile_1</m:ProfileToken>',p.P)
  self.assertEqual(self.call(raw)[0],400);self.assertEqual(len(self.camera.seen),0)
  self.bridge.config.lens='ptz';self.camera.response_status=401;self.camera.response_data=b'Not authorized'
  self.assertEqual(self.call(raw)[0],401)
  self.assertIn(HEADER.encode(),self.camera.seen[0][1])
  self.assertEqual(p.operation(self.camera.seen[0][1]).findtext('{'+p.P+'}ProfileToken'),'Profile_0')

class WatchdogTests(unittest.TestCase):
 def test_timeout_stop_preserves_auth_and_stale_timer_cannot_stop_new_move(self):
  from unittest.mock import patch
  import threading
  pending=[];sent=[]
  class Timer:
   def __init__(self,delay,fn,args=()):self.fn=fn;self.args=args;self.delay=delay;pending.append(self)
   def start(self):pass
   def cancel(self):pass
  raw=request('ContinuousMove','<m:ProfileToken>Profile_1</m:ProfileToken><m:Velocity><tt:PanTilt xmlns:tt="'+p.TT+'" x="0.1" y="0"/></m:Velocity>',p.P)
  data=p.prepare(raw);w=p.Watchdog('192.0.2.1',80)
  def send(body,headers):sent.append((body,headers));return True
  with patch.object(threading,'Timer',Timer):
   w.arm(data,{},send);old=pending[-1]
   w.arm(data,{},send);new=pending[-1]
   old.fn();self.assertEqual(sent,[])
   new.fn();self.assertEqual(len(sent),1)
   self.assertIn(HEADER.encode(),sent[0][0]);self.assertTrue(p.operation(sent[0][0]).tag.endswith('}Stop'))
   self.assertEqual(int(sent[0][1]['Content-Length']),len(sent[0][0]))
   w.arm(data,{},send);last=pending[-1];w.cancel();last.fn();self.assertEqual(len(sent),1)

class ZeroStopTests(unittest.TestCase):
 def test_zero_stop_uses_motor_profile_and_preserves_auth(self):
  raw=request('Stop','<m:ProfileToken>Profile_1</m:ProfileToken>',p.P)
  converted=p.stop_request(raw,zero=True);root=p.operation(converted)
  self.assertIn(HEADER.encode(),converted)
  self.assertEqual(root.tag,'{'+p.P+'}ContinuousMove')
  self.assertEqual(root.findtext('{'+p.P+'}ProfileToken'),'Profile_0')
  self.assertEqual(root.find('.//{'+p.TT+'}PanTilt').attrib,{'x':'0','y':'0'})
