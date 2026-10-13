#!/usr/bin/env python3
"""Create a small Debian installer that pulls a multi-platform GHCR image."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tarfile

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',root/'deploy/debian/install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--image',required=True,help='Complete GHCR image reference, normally ending in :stable')
p.add_argument('output',type=Path)
a=p.parse_args();installer.registry_image(a.image)
version=re.search(r"^VERSION = '([^']+)'",(root/'manager.py').read_text(),re.M)[1]
a.output.mkdir(parents=True,exist_ok=True)
folder=a.output/f'unifi-camera-bridge-{version}-debian';folder.mkdir(exist_ok=True)
for name in ('install.sh','install.py'):shutil.copyfile(root/'deploy/debian'/name,folder/name)
(folder/'install.sh').chmod(0o755)
shutil.copyfile(root/'deploy/compose.yaml',folder/'compose.template.yaml')
readme=(root/'deploy/debian/REGISTRY.md').read_text().replace('IMAGE_REFERENCE',a.image).replace('RELEASE_VERSION',version)
(folder/'README.md').write_text(readme)
names=['install.sh','install.py','compose.template.yaml','README.md']
checks={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}
metadata=dict(version=version,installer_version=5,distribution='registry',image=a.image,sha256=checks)
(folder/'release.json').write_text(json.dumps(metadata,indent=2)+'\n')
checks['release.json']=hashlib.sha256((folder/'release.json').read_bytes()).hexdigest()
(folder/'SHA256SUMS.txt').write_text(''.join(f'{digest}  {name}\n' for name,digest in checks.items()))
archive=a.output/f'{folder.name}-installer.tar.gz'
with tarfile.open(archive,'w:gz') as t:
    for name in names+['release.json','SHA256SUMS.txt']:t.add(folder/name,arcname=f'{folder.name}/{name}',recursive=False)
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.gz.sha256').write_text(f'{digest}  {archive.name}\n')
print(archive)
