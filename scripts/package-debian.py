#!/usr/bin/env python3
"""Bundle the Debian installer with both already-validated portable images."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('release',type=Path,help='Existing architecture release directory')
p.add_argument('output',type=Path)
a=p.parse_args()
version=re.search(r"^VERSION = '([^']+)'",(root/'manager.py').read_text(),re.M)[1]
a.output.mkdir(parents=True,exist_ok=True)
folder=a.output/f'unifi-camera-bridge-{version}-debian';folder.mkdir(exist_ok=True)
members=['install.sh','install.py','README.md','compose.template.yaml']
for name in ('install.sh','install.py','README.md'):shutil.copyfile(root/'deploy/debian'/name,folder/name)
(folder/'install.sh').chmod(0o755)
shutil.copyfile(root/'deploy/compose.yaml',folder/'compose.template.yaml')
for arch in ('amd64','arm64'):
    source=a.release/f'unifi-camera-bridge-{version}-{arch}'
    expected=dict(line.split('  ',1)[::-1] for line in (source/'SHA256SUMS.txt').read_text().splitlines())['image.tar.gz']
    with (source/'image.tar.gz').open('rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=expected:raise SystemExit(f'{arch} image checksum mismatch')
    with tarfile.open(source/'image.tar.gz') as image:
        manifest=json.load(image.extractfile('manifest.json'))
        config=json.load(image.extractfile(manifest[0]['Config']))
        assert len(manifest)==1 and config['architecture']==arch and config['os']=='linux'
        assert manifest[0]['RepoTags'][0].removeprefix('docker.io/')==f'local/unifi-camera-bridge:{version}-{arch}'
    name=f'image-{arch}.tar.gz';shutil.copyfile(source/'image.tar.gz',folder/name);members.append(name)
checks={}
for name in members:
    with (folder/name).open('rb') as f:checks[name]=hashlib.file_digest(f,'sha256').hexdigest()
(folder/'release.json').write_text(json.dumps({'version':version,'installer_version':5,'sha256':checks},indent=2)+'\n')
members.append('release.json')
with (folder/'release.json').open('rb') as f:checks['release.json']=hashlib.file_digest(f,'sha256').hexdigest()
(folder/'SHA256SUMS.txt').write_text(''.join(f'{digest}  {name}\n' for name,digest in checks.items()));members.append('SHA256SUMS.txt')
archive=a.output/f'unifi-camera-bridge-{version}-debian-installer.tar.gz'
with tarfile.open(archive,'w:gz',compresslevel=1) as tar:
    for name in members:tar.add(folder/name,arcname=str(Path(folder.name)/name),recursive=False)
with archive.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
archive.with_suffix(archive.suffix+'.sha256').write_text(f'{digest}  {archive.name}\n')
print(f'Created {archive} ({archive.stat().st_size/1024**2:.1f} MiB)')
