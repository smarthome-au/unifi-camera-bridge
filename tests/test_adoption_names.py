"""Protect uses Manufacturer + Model; display names must not change identity."""
import copy
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
import adapters
import dual_camera as dual
import generic_onvif as generic
from test_bridge import settings
from test_discovery import plan, stream

D=dual.D
TT=dual.TT

def request(op):
 return ET.fromstring(f'<s:Envelope xmlns:s="{dual.S}" xmlns:d="{D}"><s:Body><d:{op}/></s:Body></s:Envelope>')

def native_info():
 root,body=dual.envelope(D,'GetDeviceInformation')
 for k,v in [('Manufacturer','SONOFF'),('Model','Native model'),('SerialNumber','serial-123'),('HardwareId','hardware-123'),('FirmwareVersion','1.0')]:dual.node(body,D,k,v)
 return ET.tostring(root)

def fields(raw):
 return {dual.local(e.tag):e.text for e in ET.fromstring(raw).iter() if dual.local(e.tag) in ('Manufacturer','Model','SerialNumber','HardwareId','FirmwareVersion')}

class AdoptionNames(unittest.TestCase):
 def test_rtsp_and_discovered_models_keep_metadata_and_stable_identity_on_rename(self):
  for model,expected in [('rtsp','RTSP'),('auto','Unknown'),('cam-pt2','CAM-PT2')]:
   camera=dict(name='Lounge & <Door> café',model=model,discovery=plan())
   if model=='cam-pt2':camera['discovery']['device']['Model']='CAM-PT2'
   before=copy.deepcopy(camera)
   args=(0,'http://192.0.2.1:8101',8601,'02:00:00:00:00:01')
   status,raw=generic.response(request('GetDeviceInformation'),camera,*args)
   self.assertEqual(status,200);a=fields(raw)
   self.assertEqual(a['Manufacturer'],camera['name']);self.assertEqual(a['Model'],expected)
   self.assertEqual(camera,before)
   camera['name']='Renamed'
   b=fields(generic.response(request('GetDeviceInformation'),camera,*args)[1])
   self.assertEqual(b['Manufacturer'],'Renamed')
   for key in ('SerialNumber','HardwareId','FirmwareVersion','Model'):self.assertEqual(a[key],b[key])
 def test_discovered_multilens_name_and_scope_match(self):
  camera=dict(name='Garden',model='auto',discovery=plan([stream(),stream(source='lens2')]))
  names=[]
  for i in (0,1):
   args=(camera,i,'http://192.0.2.1:8101',8601,'02:00:00:00:00:01')
   name=fields(generic.response(request('GetDeviceInformation'),*args)[1])['Manufacturer'];names.append(name)
   raw=generic.response(request('GetScopes'),*args)[1]
   scopes=[e.text for e in ET.fromstring(raw).iter() if dual.local(e.tag)=='ScopeItem']
   self.assertIn('onvif://www.onvif.org/name/'+urllib.parse.quote(name,safe=''),scopes)
  self.assertEqual(names,['Garden · View 1','Garden · View 2'])
 def test_s3w_named_identity_escaped_and_original_model_for_passthrough(self):
  c=settings();c.name='Fish Tank & <fish> café'
  for adapter,model in [('cam-s3w','CAM-S3W'),('passthrough','Native model')]:
   c.adapter=adapter
   for data in (native_info(),native_info().replace(b'>SONOFF</',b'></')):
    got=fields(adapters.cam_s3w_response(data,'GetDeviceInformation',c))
    self.assertEqual(got['Manufacturer'],c.name);self.assertEqual(got['Model'],model)
    self.assertEqual(got['SerialNumber'],'serial-123-bridge');self.assertEqual(got['HardwareId'],'hardware-123')
 def test_dual_lens_names_keep_distinct_serials_and_actual_model(self):
  c=settings();c.adapter='cam-c44he';serials=[]
  for lens in ('fixed','ptz'):
   c.lens=lens;c.name='Workshop & Garden · '+lens.upper()
   a=fields(dual.device_response(native_info(),'GetDeviceInformation',c))
   self.assertEqual(a['Manufacturer'],c.name);self.assertEqual(a['Model'],'CAM-C44HE');serials.append(a['SerialNumber'])
   root,body=dual.envelope(D,'GetScopes')
   raw=dual.device_response(ET.tostring(root),'GetScopes',c)
   self.assertIn('onvif://www.onvif.org/name/'+urllib.parse.quote(c.name,safe=''),[e.text for e in ET.fromstring(raw).iter()])
  self.assertNotEqual(*serials)
