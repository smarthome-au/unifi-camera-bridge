"""Regression checks for the NAS-compatible release archive validator."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from docker_archive import verify_classic

class ArchiveTests(unittest.TestCase):
 def fixture(self,path,*,corrupt=False,oci=False,legacy=True):
  layer=b'fixture-layer-bytes'
  config=json.dumps({'architecture':'amd64','os':'linux','rootfs':{'diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]}}).encode()
  files={'config.json':config,'layer.tar':layer+(b'corrupted' if corrupt else b''),'manifest.json':json.dumps([{'Config':'config.json','Layers':['layer.tar'],'RepoTags':['local/test:1']}]).encode()}
  if legacy:files.update({'repositories':b'{"local/test":{"1":"legacy"}}','legacy/json':b'{}','legacy/VERSION':b'1.0','legacy/layer.tar':layer})
  if oci:files['oci-layout']=b'{"imageLayoutVersion":"1.0.0"}'
  with tarfile.open(path,'w') as archive:
   for name,body in files.items():
    info=tarfile.TarInfo(name);info.size=len(body);archive.addfile(info,io.BytesIO(body))
  return config
 def test_classic_archive_passes_and_architecture_or_config_changes_fail(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'image.tar';config=self.fixture(p)
   verify_classic(p,config,'amd64')
   with self.assertRaises(ValueError):verify_classic(p,config,'arm64')
   with self.assertRaises(ValueError):verify_classic(p,config+b' ','amd64')
 def test_layer_corruption_fails_even_with_valid_metadata(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'image.tar';config=self.fixture(p,corrupt=True)
   with self.assertRaisesRegex(ValueError,'Layer contents'):verify_classic(p,config,'amd64')
 def test_modern_layout_or_missing_legacy_metadata_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'image.tar'
   for kwargs in [{'oci':True},{'legacy':False}]:
    config=self.fixture(p,**kwargs)
    with self.assertRaises(ValueError):verify_classic(p,config,'amd64')
