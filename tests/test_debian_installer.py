import hashlib
import os
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bridge_debian_install',ROOT/'deploy/debian/install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def result(stdout='',code=0):return subprocess.CompletedProcess([],code,stdout,'')
def interface(name,ip):return {'ifname':name,'addr_info':[{'family':'inet','local':ip}]}

class PreflightTests(unittest.TestCase):
 def test_supported_os_and_derivatives(self):
  self.assertEqual(m.parse_os('ID=debian\nVERSION_ID="13"'),'trixie')
  self.assertEqual(m.parse_os('ID=debian\nVERSION_ID=12'),'bookworm')
  for data in ['ID=ubuntu\nVERSION_ID=13','ID=debian\nVERSION_ID=11']:
   with self.assertRaises(m.InstallError):m.parse_os(data)
 def test_address_selection_avoids_docker_and_vpn(self):
  interfaces=[interface('docker0','172.17.0.1'),interface('tun0','10.0.0.5'),interface('ens18','192.168.50.4')]
  self.assertEqual(m.choose_address(interfaces,[{'dev':'tun0','metric':1},{'dev':'ens18','metric':100}]),'192.168.50.4')
  self.assertEqual(m.choose_address(interfaces,[],override='10.0.0.5'),'10.0.0.5')
  with self.assertRaises(m.InstallError):m.choose_address(interfaces,[],override='192.168.50.99')
 def test_ambiguous_addresses_require_override(self):
  interfaces=[interface('ens18','192.168.50.4'),interface('ens19','192.168.60.4')]
  with self.assertRaises(m.InstallError):m.choose_address(interfaces,[])
  self.assertEqual(m.choose_address(interfaces,[{'dev':'ens19'}]),'192.168.60.4')
  with self.assertRaises(m.InstallError):m.choose_address([interface('ens18','8.8.8.8')],[])
 def test_compose_fixed_port_mapping_and_persistent_folder(self):
  value=m.compose_text((ROOT/'deploy/compose.yaml').read_text(),'1.4.0','amd64','192.168.50.4',18080)
  for text in ['192.168.50.4:18080:8080','192.168.50.4:8100-8115:8100-8115/tcp','192.168.50.4:8600-8615:8600-8615/tcp','local/unifi-camera-bridge:1.4.0-amd64','type: bind','target: /data','create_host_path: false',str(m.INSTALL_DIR/'data')]:
   self.assertIn(text,value)
  self.assertEqual(value.count(m.LABEL),1)
  self.assertNotIn("\nvolumes:",value)
  for port in [80,8105,8609,65536]:
   with self.assertRaises(m.InstallError):m.compose_text('', '1.4.0','amd64','192.168.50.4',port)
 def test_port_conflicts_include_docker_nat_without_host_listener(self):
  with patch.object(m.socket,'socket') as sock:
   with self.assertRaisesRegex(m.InstallError,'8103'):m.check_ports('192.168.50.4',18080,published={8103})
   sock.return_value.__enter__.return_value.bind.side_effect=OSError('busy')
   with self.assertRaisesRegex(m.InstallError,'18080'):m.check_ports('192.168.50.4',18080)
 def test_unmanaged_container_is_left_alone(self):
  with patch.object(m,'run',return_value=result(json.dumps([{'Config':{'Labels':{}}}]))):
   with self.assertRaisesRegex(m.InstallError,'left untouched'):m.inspect_container()
 def test_repository_names_migrate_with_valid_signing_key_reference(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'keyrings').mkdir();(root/'sources.list.d').mkdir()
   key=root/f'keyrings/{m.LEGACY_NAME}-docker.asc';key.write_text('public-key')
   source=root/f'sources.list.d/{m.LEGACY_NAME}-docker.sources'
   source.write_text(f'URIs: https://download.docker.com/linux/debian\nSigned-By: {key}\n')
   with patch.object(m,'APT_DIR',root):m.migrate_repository_names()
   new_key=root/'keyrings/unifi-camera-bridge-docker.asc'
   new_source=root/'sources.list.d/unifi-camera-bridge-docker.sources'
   self.assertEqual(new_key.read_text(),'public-key')
   self.assertIn('Signed-By: '+str(new_key),new_source.read_text())
   self.assertFalse(key.exists());self.assertFalse(source.exists())
 def test_repository_destination_collision_keeps_original_files(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'keyrings').mkdir();(root/'sources.list.d').mkdir()
   key=root/f'keyrings/{m.LEGACY_NAME}-docker.asc';key.write_text('public-key')
   source=root/f'sources.list.d/{m.LEGACY_NAME}-docker.sources'
   source.write_text(f'URIs: https://download.docker.com/linux/debian\nSigned-By: {key}\n')
   (root/'keyrings/unifi-camera-bridge-docker.asc').write_text('existing')
   with patch.object(m,'APT_DIR',root):
    with self.assertRaisesRegex(m.InstallError,'Both old and new'):m.migrate_repository_names()
   self.assertTrue(source.exists());self.assertEqual(key.read_text(),'public-key')
 def test_working_docker_reused_without_package_changes(self):
  calls=[]
  with patch.object(m.shutil,'which',return_value='/usr/bin/docker'),patch.object(m,'installed',return_value=False),patch.object(m,'run',side_effect=lambda args,**kw:(calls.append(args) or result())):
   m.ensure_docker('trixie','amd64')
  self.assertFalse(any(x[0] in ('apt-get','systemctl') for x in calls))
 def test_conflicting_packages_are_not_removed(self):
  calls=[]
  with patch.object(m.shutil,'which',return_value=None),patch.object(m,'installed',side_effect=lambda p:p=='containerd'),patch.object(m,'run',side_effect=lambda args,**kw:(calls.append(args) or result())):
   with self.assertRaisesRegex(m.InstallError,'No packages were removed'):m.ensure_docker('trixie','amd64')
  self.assertEqual(calls,[])
 def test_health_failure_restores_previous_compose(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'compose.yaml').write_text('previous');(root/'installer-state.json').write_text('{"version":"old"}')
   calls=[]
   with patch.object(m,'INSTALL_DIR',root),patch.object(m,'run',side_effect=lambda args,**kw:(calls.append(args) or result())),patch.object(m,'wait_healthy',side_effect=m.InstallError('failed')):
    with self.assertRaises(m.InstallError):m.deploy('new',{'version':'new'})
   self.assertEqual((root/'compose.yaml').read_text(),'previous')
   self.assertEqual(json.loads((root/'installer-state.json').read_text()),{'version':'old'})
   self.assertEqual(sum('up' in c for c in calls),2)
   self.assertFalse(any('-v' in c for c in calls))
 def test_failed_first_install_retains_data_folder(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);calls=[]
   with patch.object(m,'INSTALL_DIR',root),patch.object(m,'run',side_effect=lambda args,**kw:(calls.append(args) or result())),patch.object(m,'wait_healthy',side_effect=m.InstallError('failed')):
    with self.assertRaises(m.InstallError):m.deploy('new',{})
   self.assertFalse((root/'compose.yaml').exists())
   self.assertTrue(any('down' in c for c in calls));self.assertFalse(any('-v' in c or '--volumes' in c for c in calls))

class InstallerFlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.bundle=self.root/'bundle';self.bundle.mkdir()
  self.target=self.root/'installed';self.systemd=self.root/'systemd';self.systemd.mkdir()
  self.osfile=self.root/'os-release';self.osfile.write_text('ID=debian\nVERSION_ID=13\n')
  (self.bundle/'image-amd64.tar.gz').write_bytes(b'checked-image')
  (self.bundle/'compose.template.yaml').write_text((ROOT/'deploy/compose.yaml').read_text())
  checks={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.bundle.iterdir()}
  (self.bundle/'release.json').write_text(json.dumps({'version':'1.4.0','sha256':checks}))
  self.calls=[];self.docker_ready=False;self.container=None
  self.patches=[patch.object(m,'DATA_UID',os.getuid()),patch.object(m,'DATA_GID',os.getgid()),patch.object(m,'BUNDLE',self.bundle),patch.object(m,'INSTALL_DIR',self.target),patch.object(m,'LEGACY_INSTALL_DIRS',(self.root/'legacy',self.root/'intermediate')),patch.object(m,'SYSTEMD_DIR',self.systemd),patch.object(m,'OS_RELEASE',self.osfile),patch.object(m,'LOCK_PATH',self.root/'lock'),patch.object(m,'LEGACY_LOCK_PATH',self.root/'old-lock'),patch.object(m.os,'geteuid',return_value=0),patch.object(m.shutil,'disk_usage',return_value=type('Disk',(),{'free':4*1024**3})()),patch.object(m.shutil,'which',return_value='/usr/bin/docker'),patch.object(m,'run',side_effect=self.command),patch.object(m,'check_ports'),patch.object(m,'ensure_docker',side_effect=self.install_docker)]
  for p in self.patches:p.start()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def install_docker(self,*args):self.calls.append(['install-docker']);self.docker_ready=True
 def command(self,args,**kwargs):
  self.calls.append(args)
  if args[0]=='dpkg':return result('amd64')
  if args[0]=='ip':return result(json.dumps([interface('ens18','192.168.50.4')] if 'address' in args else [{'dev':'ens18'}]))
  if 'image' in args and 'inspect' in args:return result(json.dumps({'Architecture':'amd64','Os':'linux','Config':{'Labels':{'org.opencontainers.image.version':'1.6.2'}}}))
  if 'container' in args and 'inspect' in args:
   return result(json.dumps([self.container])) if self.container else result(code=1)
  if 'up' in args or 'create' in args:
   target=self.target/'data'
   if target.is_dir() and not (target/'config.json').exists():(target/'config.json').write_text('{}')
  if 'info' in args:return result('x86_64') if self.docker_ready else result(code=1)
  if 'ps' in args:return result('')
  if 'inspect' in args:return result('{"Status":"running","Health":{"Status":"healthy"}}')
  return result()
 def test_fresh_install_and_rerun_preserve_settings_and_address(self):
  m.main([])
  state=json.loads((self.target/'installer-state.json').read_text())
  self.assertEqual(state['bind_address'],'192.168.50.4')
  self.assertTrue(any('load' in c for c in self.calls));self.assertTrue(any('up' in c for c in self.calls))
  m.main([])
  self.assertIn('type: bind',(self.target/'compose.yaml').read_text())
  self.assertEqual(json.loads((self.target/'installer-state.json').read_text()),state)
 def test_check_is_read_only(self):
  m.main(['--check'])
  self.assertFalse(self.target.exists())
  self.assertFalse(any(c[0] in ('install-docker','systemctl') or 'load' in c or 'up' in c for c in self.calls))
 def test_corrupt_image_stops_before_installing_docker(self):
  (self.bundle/'image-amd64.tar.gz').write_bytes(b'tampered')
  with self.assertRaisesRegex(m.InstallError,'checksum'):m.main([])
  self.assertNotIn(['install-docker'],self.calls)
 def test_unmanaged_destination_prevents_deployment(self):
  self.target.mkdir();(self.target/'compose.yaml').write_text('existing')
  with self.assertRaisesRegex(m.InstallError,'unmanaged'):m.main([])
  self.assertNotIn(['install-docker'],self.calls)

 def test_older_directory_requires_fresh_install_without_changing_files(self):
  legacy=self.root/'legacy';legacy.mkdir();(legacy/'sentinel').write_text('keep')
  with self.assertRaisesRegex(m.InstallError,'fresh installation'):m.main([])
  self.assertEqual((legacy/'sentinel').read_text(),'keep');self.assertFalse(self.target.exists())
 def test_fresh_install_and_rerun_keep_settings_in_directory(self):
  m.main([])
  data=self.target/'data';config=data/'config.json'
  config.write_text('{"identity":"retained"}')
  m.main([])
  self.assertEqual(config.read_text(),'{"identity":"retained"}')
  self.assertEqual(data.stat().st_mode&0o777,0o700)
  self.assertEqual(json.loads((self.target/'installer-state.json').read_text())['storage'],'directory')
  self.assertIn(str(data),(self.target/'compose.yaml').read_text())
  self.assertFalse(any('volume' in c or 'stop' in c for c in self.calls))

 def registry_bundle(self):
  p=self.bundle/'release.json';metadata=json.loads(p.read_text())
  metadata.update(distribution='registry',image='ghcr.io/example/unifi-camera-bridge:stable')
  p.write_text(json.dumps(metadata))
 def test_registry_pull_precedes_recreation_and_persists_real_version(self):
  self.registry_bundle();m.main([])
  pull=next(i for i,c in enumerate(self.calls) if 'pull' in c)
  up=next(i for i,c in enumerate(self.calls) if 'up' in c)
  self.assertLess(pull,up);self.assertFalse(any('load' in c for c in self.calls))
  compose=(self.target/'compose.yaml').read_text()
  self.assertIn('ghcr.io/example/unifi-camera-bridge:stable',compose)
  self.assertNotIn('pull_policy:',compose)
  self.assertIn('platform: linux/amd64',compose)
  self.assertEqual(json.loads((self.target/'installer-state.json').read_text())['version'],'1.6.2')
 def test_registry_preflight_never_pulls(self):
  self.registry_bundle();m.main(['--check'])
  self.assertFalse(any('pull' in c or 'up' in c for c in self.calls))
 def test_registry_pull_failure_does_not_touch_existing_deployment(self):
  self.registry_bundle();self.target.mkdir()
  (self.target/'compose.yaml').write_text('previous configuration')
  (self.target/'installer-state.json').write_text('{"version":"1.5.1","storage":"directory"}')
  m.create_data_directory(self.target/'data');(self.target/'data/config.json').write_text('{}')
  command=self.command
  def fail(args,**kw):
   if 'pull' in args:raise m.InstallError('pull failed')
   return command(args,**kw)
  with patch.object(m,'run',side_effect=fail):
   with self.assertRaisesRegex(m.InstallError,'pull failed'):m.main([])
  self.assertEqual((self.target/'compose.yaml').read_text(),'previous configuration')
  self.assertFalse(any('up' in c or 'down' in c or 'stop' in c for c in self.calls))
 def test_wrong_registry_architecture_never_deploys(self):
  self.registry_bundle();command=self.command
  def wrong(args,**kw):
   if 'image' in args and 'inspect' in args:return result('{"Architecture":"arm64","Os":"linux"}')
   return command(args,**kw)
  with patch.object(m,'run',side_effect=wrong):
   with self.assertRaisesRegex(m.InstallError,'does not match'):m.main([])
  self.assertFalse(any('up' in c for c in self.calls))

class RegistryValidationTests(unittest.TestCase):
 def test_registry_references_and_injection_rejected(self):
  for value in ('ghcr.io/example/bridge:stable','ghcr.io/example/bridge:1.6.2','ghcr.io/example/bridge@sha256:'+'a'*64):
   self.assertEqual(m.registry_image(value),value)
  for value in ('ghcr.io/example/bridge','docker.io/example/bridge:latest','ghcr.io/example/bridge:stable\n    privileged: true','ghcr.io/user:password@example/bridge:stable',None):
   with self.assertRaises(m.InstallError):m.registry_image(value)
 def test_rollback_pins_original_image_when_stable_tag_has_moved(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);previous='services:\n  manager:\n    image: ghcr.io/example/bridge:stable\n'
   (root/'compose.yaml').write_text(previous);(root/'installer-state.json').write_text('{}')
   old_id='sha256:'+'a'*64
   with patch.object(m,'INSTALL_DIR',root),patch.object(m,'run',return_value=result()),patch.object(m,'wait_healthy',side_effect=m.InstallError('failed')):
    with self.assertRaises(m.InstallError):m.deploy('new',{},old_id)
   self.assertIn('image: '+old_id,(root/'compose.yaml').read_text())
   self.assertNotIn(':stable',(root/'compose.yaml').read_text())
 def test_small_registry_bundle_is_self_contained_without_image_tarballs(self):
  import tarfile
  with tempfile.TemporaryDirectory() as tmp:
   subprocess.run([__import__('sys').executable,str(ROOT/'scripts/package-registry-installer.py'),'--image','ghcr.io/example/bridge:stable',tmp],check=True,stdout=subprocess.PIPE)
   archive=next(Path(tmp).glob('*.tar.gz'))
   with tarfile.open(archive) as tar:
    self.assertFalse(any('image-' in n for n in tar.getnames()))
   folder=next(p for p in Path(tmp).iterdir() if p.is_dir())
   self.assertEqual(m.verify_payload(folder)['image'],'ghcr.io/example/bridge:stable')
   self.assertLess(archive.stat().st_size,100000)

class DirectoryStorageTests(unittest.TestCase):
 def test_folder_validation_and_no_volume_migration(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   with patch.object(m,'DATA_UID',os.getuid()),patch.object(m,'DATA_GID',os.getgid()):
    m.check_storage(root,None,{})
    with self.assertRaisesRegex(m.InstallError,'Reinstall'):m.check_storage(root,None,{'version':'old'})
    with self.assertRaisesRegex(m.InstallError,'missing'):m.check_storage(root,None,{'storage':'directory'})
    m.create_data_directory(root/'data')
    with self.assertRaisesRegex(m.InstallError,'config.json'):m.check_storage(root,None,{'storage':'directory'})
    (root/'data/config.json').write_text('{}')
    m.check_storage(root,None,{'storage':'directory'})
    (root/'data/config.json').unlink();(root/'data/config.json').symlink_to(root/'missing')
    with self.assertRaisesRegex(m.InstallError,'regular file'):m.check_storage(root,None,{})
    (root/'data/config.json').unlink();(root/'data').chmod(0o755)
    with self.assertRaisesRegex(m.InstallError,'0700'):m.check_storage(root,None,{})
 def test_only_expected_writable_bind_mount_is_accepted(self):
  c={'Config':{'Labels':{m.LABEL:m.MARKER,'com.docker.compose.project':m.PROJECT}},
     'Mounts':[{'Type':'bind','Source':str(m.INSTALL_DIR/'data'),'Destination':'/data','RW':True}]}
  with patch.object(m,'run',return_value=result(json.dumps([c]))):self.assertEqual(m.inspect_container(),c)
  for mount in ({'Type':'volume','Name':'unifi-camera-bridge-data','Destination':'/data'},
                {'Type':'bind','Source':'/wrong','Destination':'/data'},
                {'Type':'bind','Source':str(m.INSTALL_DIR/'data'),'Destination':'/data','RW':False}):
   c['Mounts']=[mount]
   with patch.object(m,'run',return_value=result(json.dumps([c]))):
    with self.assertRaisesRegex(m.InstallError,'settings folder'):m.inspect_container()
