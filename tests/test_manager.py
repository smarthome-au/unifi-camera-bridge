import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Store, Server, Supervisor, address, resolved, check_password, suggested_host

class StoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.store.setup('test-admin-password','192.168.1.27')
 def tearDown(self):self.tmp.cleanup()
 def camera(self,**kw):
  data=dict(revision=self.store.data['revision'],name='Entrance',model='cam-c44he',address='192.168.1.104',username='camera',password='private-camera-test-secret',enabled=True,ptz=True)
  data.update(kw);self.store.mutate('camera',data);return self.store.data['cameras'][-1]
 def test_two_cameras_unique_ports_identity_and_restart_persistence(self):
  a=self.camera();b=self.camera(name='Other',address='192.168.1.105',model='cam-s3w')
  self.assertEqual([e['slot'] for c in [a,b] for e in c['endpoints']],[0,1,2])
  public=json.dumps(self.store.public());self.assertNotIn('private-camera-test-secret',public);self.assertNotIn('digest',public)
  identities=copy.deepcopy(a['endpoints'])
  self.camera(id=a['id'],name='Changed',address='192.168.1.106',password='')
  restored=Store(self.tmp.name)
  self.assertEqual(restored.data['cameras'][0]['endpoints'],identities)
  self.assertEqual(restored.data['cameras'][0]['password'],'private-camera-test-secret')
  self.assertEqual(restored.path.stat().st_mode&0o777,0o600)
 def test_capacity_duplicate_and_revision_are_atomic(self):
  self.store.capacity=2;self.camera();before=copy.deepcopy(self.store.data)
  for params in [dict(address='192.168.1.105'),dict(revision=0),dict(address='192.168.1.104')]:
   with self.assertRaises(ValueError):self.camera(**params)
   self.assertEqual(self.store.data,before)
 def test_delete_and_readd_allocates_new_identity(self):
  a=self.camera();self.store.mutate('delete',dict(id=a['id'],revision=self.store.data['revision']))
  b=self.camera();self.assertNotEqual(a['endpoints'][0]['mac'],b['endpoints'][0]['mac'])
 def test_no_public_loopback_or_url_camera_targets(self):
  for value in ['http://192.168.1.1','127.0.0.1','169.254.169.254','8.8.8.8','1.2.3.999','a..b']:
   with self.assertRaises(ValueError):address(value)
  self.assertEqual(address('CAMERA.lan'),'camera.lan')
  with patch('socket.gethostbyname',return_value='127.0.0.1'):
   with self.assertRaises(ValueError):resolved('camera.lan')
 def test_worker_plan_secret_handling_and_camera_isolation(self):
  a=self.camera();b=self.camera(address='192.168.1.105',model='cam-s3w')
  supervisor=Supervisor(self.store,Path(self.tmp.name)/'runtime')
  with patch('manager.resolved',side_effect=lambda x:x):first=supervisor.desired()
  self.assertEqual(len(first),4)
  for env,script,_ in first.values():self.assertNotIn('private-camera-test-secret',json.dumps(env))
  self.camera(id=a['id'],name='New Name')
  with patch('manager.resolved',side_effect=lambda x:x):second=supervisor.desired()
  self.assertEqual(first[b['id']+'/fixed'],second[b['id']+'/fixed'])
  self.assertNotEqual(first[a['id']+'/fixed'],second[a['id']+'/fixed'])
  self.assertEqual(first[a['id']+'/converter'][0]['TRANSCODER_RTSP_PORT'],'19000')
 def test_setup_one_use_and_hashed_admin(self):
  self.assertFalse((self.store.directory/'setup-token').exists())
  self.assertTrue(check_password('test-admin-password',self.store.data['admin']))
  self.assertFalse(check_password('wrong',self.store.data['admin']))
  with self.assertRaises(ValueError):self.store.setup('other-password','192.168.1.27')

class HTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();store=Store(self.tmp.name)
  from diagnostics import Journal
  class SupervisorStub:
   logs=Journal()
   def status(self):return {}
   wake=threading.Event()
  self.server=Server(('127.0.0.1',0),store,SupervisorStub())
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.host='127.0.0.1:'+str(self.server.server_port);self.cookie='';self.csrf=''
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def request(self,path,data=None,origin=True,csrf=True,host=None,extra_headers=None):
  conn=http.client.HTTPConnection(self.host);headers={'Host':host or self.host}
  if extra_headers:headers.update(extra_headers)
  if self.cookie:headers['Cookie']=self.cookie
  if origin:headers['Origin']='http://'+(host or self.host)
  if csrf:headers['X-CSRF-Token']=self.csrf
  if data is not None:headers['Content-Type']='application/json'
  conn.request('POST' if data is not None else 'GET',path,None if data is None else json.dumps(data),headers)
  r=conn.getresponse();body=r.read();status=r.status
  cookie=r.getheader('Set-Cookie')
  if cookie:self.cookie=cookie.split(';')[0]
  conn.close();return status,json.loads(body)
 def setup_login(self):
  status,result=self.request('/api/setup',dict(password='test-admin-password',host='192.168.1.27'))
  self.assertEqual(status,200);self.csrf=result['csrf']
 def test_unauthenticated_configuration_blocked_and_setup_needs_valid_password(self):
  self.assertEqual(self.request('/api/state')[0],401)
  self.assertEqual(self.request('/api/setup',dict(password='short',host='192.168.1.27'))[0],400)
  self.setup_login();self.assertEqual(self.request('/api/state')[0],200)
 def test_setup_infers_lan_address_without_token_and_only_runs_once(self):
  host='192.168.50.20:18080'
  status,result=self.request('/api/session',host=host)
  self.assertEqual(result['suggested_host'],'192.168.50.20')
  self.assertTrue(result['setup_required'])
  status,result=self.request('/api/setup',dict(password='test-admin-password'),host=host,origin=False)
  self.assertEqual(status,403)
  status,result=self.request('/api/setup',dict(password='test-admin-password'),host=host)
  self.assertEqual(status,200)
  self.assertEqual(self.server.store.data['host'],'192.168.50.20')
  before=copy.deepcopy(self.server.store.data)
  self.assertEqual(self.request('/api/setup',dict(password='different-password',host='192.168.50.21'),host=host)[0],400)
  self.assertEqual(self.server.store.data,before)
  self.assertEqual(self.request('/api/session',host='192.168.60.30:18080')[1]['suggested_host'],'')
  self.assertEqual(self.server.store.data['host'],'192.168.50.20')
 def test_localhost_requires_manual_address_and_ignores_proxy_headers(self):
  result=self.request('/api/session',extra_headers={'X-Forwarded-Host':'192.168.99.2','Forwarded':'host=192.168.99.2'})[1]
  self.assertEqual(result['suggested_host'],'')
  self.assertEqual(self.request('/api/setup',dict(password='test-admin-password'))[0],400)
  self.assertIsNone(self.server.store.data['admin'])
  self.assertEqual(self.request('/api/setup',dict(password='test-admin-password',host='192.168.55.20'),host='192.168.60.20:18080')[0],200)
  self.assertEqual(self.server.store.data['host'],'192.168.55.20')
 def test_logs_require_login_and_validate_filters(self):
  self.assertEqual(self.request('/api/logs')[0],401)
  self.setup_login()
  self.server.supervisor.logs.add('warning','Snapshot unavailable.','camera/fixed')
  status,result=self.request('/api/logs?camera=camera&level=issues')
  self.assertEqual(status,200);self.assertEqual(len(result['entries']),1)
  self.assertEqual(self.request('/api/logs?limit=1001')[0],400)
  self.assertEqual(self.request('/api/logs?level=unknown')[0],400)
 def test_csrf_origin_and_logout(self):
  self.setup_login()
  for origin,csrf in [(False,True),(True,False)]:
   self.assertEqual(self.request('/api/host',dict(host='192.168.1.28',revision=1),origin=origin,csrf=csrf)[0],403)
  self.assertEqual(self.request('/api/host',dict(host='192.168.1.28',revision=1))[0],200)
  self.assertEqual(self.request('/api/logout',{})[0],200)
  self.assertEqual(self.request('/api/state')[0],401)
 def test_redaction_and_stale_updates(self):
  self.setup_login();payload=dict(revision=1,name='Test',model='cam-s3w',address='192.168.1.9',username='camera',password='sentinel-secret')
  self.assertEqual(self.request('/api/camera',payload)[0],200)
  status,body=self.request('/api/state');self.assertEqual(status,200)
  self.assertNotIn('sentinel-secret',json.dumps(body));self.assertTrue(body['cameras'][0]['has_password'])
  self.assertEqual(self.request('/api/camera',payload)[0],400)
 def test_pt2_inspection_keeps_preset_and_saves_verified_plan(self):
  from test_presets import pt2_plan
  import time
  self.setup_login()
  payload=dict(model='cam-pt2',name='PT2',address='192.168.1.10',username='camera',password='sentinel-secret',ptz=True)
  with patch('manager.resolved',return_value='192.168.1.10'), patch('discovery.inspect',return_value=pt2_plan()):
   status,job=self.request('/api/inspect',payload);self.assertEqual(status,202)
   for _ in range(100):
    status,result=self.request('/api/inspection/'+job['id'])
    if result['status']!='running':break
    time.sleep(.01)
   self.assertEqual(result['status'],'ready')
   self.assertNotIn('sentinel-secret',json.dumps(result))
   self.assertEqual(self.request('/api/add-inspected',dict(inspection=job['id'],revision=1))[0],200)
  saved=self.server.store.data['cameras'][0]
  self.assertEqual(saved['model'],'cam-pt2');self.assertTrue(saved['ptz'])
  self.assertEqual(len(saved['discovery']['views'][0]['streams']),1)
  self.assertEqual(self.request('/api/inspection/'+job['id'])[0],404)
 def test_login_throttle_and_wrong_credentials(self):
  self.setup_login();self.request('/api/logout',{})
  for _ in range(8):self.assertEqual(self.request('/api/login',{'password':'incorrect'})[0],401)
  self.assertEqual(self.request('/api/login',{'password':'incorrect'})[0],429)

class RelayIsolationTests(unittest.TestCase):
 def test_converted_clients_use_distinct_loopback_source(self):
  import socket
  from rtsp_relay import RTSPRelay
  relay=RTSPRelay(('127.0.0.1',0),'192.168.1.104',554,lq_target=('127.0.0.1',19000),lq_source_address='127.0.0.2')
  worker=threading.Thread(target=relay.serve_forever,daemon=True);worker.start()
  client=socket.create_connection(relay.server_address,timeout=3)
  try:
   with patch('rtsp_relay.socket.create_connection',side_effect=OSError('test boundary')) as dial:
    client.sendall(b'DESCRIBE rtsp://localhost/av_stream0/sec RTSP/1.0\r\nCSeq: 1\r\n\r\n')
    self.assertEqual(client.recv(1024),b'')
    dial.assert_called_once_with(('127.0.0.1',19000),timeout=8,source_address=('127.0.0.2',0))
  finally:client.close();relay.shutdown();relay.server_close();worker.join()

class SetupTests(unittest.TestCase):
 def test_suggestions_reject_non_lan_and_malformed_authorities(self):
  for host in ['localhost:8080','nas.lan:18080','127.0.0.1:8080','0.0.0.0:8080','8.8.8.8','[::1]:8080','169.254.169.254','192.168.1.2:65536','192.168.1.2:bad','evil@192.168.1.2','192.168.1.2/path','192.168.1.2?query','192.168.1.2#fragment']:
   with self.subTest(host=host):self.assertEqual(suggested_host(host),'')
  for host in ['192.168.50.1','172.20.4.1','10.11.11.1']:
   self.assertEqual(suggested_host(host+':18080'),host)
  self.assertEqual(suggested_host('172.18.0.2:8080',('172.18.0.2',)),'')
 def test_obsolete_token_removed_and_setup_is_atomic(self):
  with tempfile.TemporaryDirectory() as tmp:
   token=Path(tmp)/'setup-token';token.write_text('obsolete-token')
   store=Store(tmp);self.assertFalse(token.exists())
   with self.assertRaises(ValueError):store.setup('test-admin-password','127.0.0.1')
   self.assertIsNone(store.data['admin']);self.assertFalse(store.path.exists())
   barrier=threading.Barrier(2);results=[]
   def claim(host):
    barrier.wait()
    try:store.setup('test-admin-password',host);results.append('ok')
    except ValueError:results.append('rejected')
   threads=[threading.Thread(target=claim,args=(ip,)) for ip in ['192.168.50.1','192.168.60.1']]
   for t in threads:t.start()
   for t in threads:t.join()
   self.assertCountEqual(results,['ok','rejected'])
   self.assertEqual(Store(tmp).data,store.data)
