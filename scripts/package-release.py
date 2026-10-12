#!/usr/bin/env python3
"""Build architecture-specific Docker installation kits, without runtime data."""
import argparse
import hashlib
import re
import shutil
import subprocess
import tempfile
from docker_archive import convert
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
parser.add_argument('--skip-build', action='store_true', help='Use existing architecture-tagged images')
args = parser.parse_args()
version = re.search(r"^VERSION = '([^']+)'", (root/'manager.py').read_text(), re.M)[1]
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
artifacts = []
for arch in ('amd64', 'arm64'):
    image = f'local/sonoff-protect-bridge:{version}-{arch}'
    if not args.skip_build:
        subprocess.run(['docker','buildx','build','--platform',f'linux/{arch}','--load','-t',image,str(root)], check=True)
    actual = subprocess.check_output(['docker','image','inspect',image,'--format','{{.Architecture}}'], text=True).strip()
    if actual != arch:
        raise SystemExit(f'{image}: expected {arch}, got {actual}')
    folder = output/f'sonoff-protect-bridge-{version}-{arch}'
    folder.mkdir(exist_ok=True)
    compose = (root/'deploy/compose.yaml').read_text().replace('VERSION',version).replace('ARCH',arch)
    (folder/'compose.yaml').write_text(compose)
    qnap = compose.replace("${ADMIN_BIND_IP:-127.0.0.1}:${ADMIN_PORT:-18080}", '0.0.0.0:18080')
    (folder/'compose.qnap.yaml').write_text('# No IP edits needed. Admin page listens on all NAS IPv4 interfaces at port 18080.\n'+qnap)
    for name in ('env.example','INSTALL.md'):
        shutil.copyfile(root/'deploy'/name,folder/name)
    (folder/'APPLICATION.md').write_text((root/'README.md').read_text().replace('(deploy/INSTALL.md)', '(INSTALL.md)'))
    # Normalize modern containerd/OCI exports to a legacy-compatible Docker archive.
    # Keep the export subprocess checked: a shell pipe can hide docker save failures.
    with tempfile.TemporaryDirectory(prefix='.export-', dir=output) as tmp:
        raw = Path(tmp)/'image.tar'
        with raw.open('wb') as stream:
            subprocess.run(['docker','save','--platform',f'linux/{arch}',image],stdout=stream,check=True)
        convert(raw, folder/'image.tar.gz', image, arch)
    sums=[]
    members = ['compose.yaml','compose.qnap.yaml','env.example','INSTALL.md','APPLICATION.md','image.tar.gz']
    for path in (folder/name for name in members):
        if path.is_file():
            with path.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
            sums.append(f'{digest}  {path.name}\n')
    (folder/'SHA256SUMS.txt').write_text(''.join(sums))
    bundle=output/f'{folder.name}-docker.zip'
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_STORED) as z:
        for name in members+['SHA256SUMS.txt']: z.write(folder/name,Path(folder.name)/name)
    artifacts.append(bundle)
    print(f'Packaged {arch}: {bundle.name}',flush=True)
# Explicit source allowlist: no data volumes, credentials, build caches or local env files.
source=output/f'sonoff-protect-bridge-{version}-source.zip'
with zipfile.ZipFile(source,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(root.rglob('*')):
        relative=p.relative_to(root)
        if not p.is_file() or '__pycache__' in p.parts: continue
        allowed=(len(relative.parts)==1 and (p.suffix=='.py' or p.name in ('Dockerfile','.dockerignore','compose.yaml','README.md','.gitignore')))
        allowed |= relative.parts[0] in ('static','tests','deploy','scripts','.github') and p.suffix in ('.py','.js','.css','.html','.json','.md','.yaml','.yml','.example','.sh')
        if allowed: z.write(p,Path(root.name)/relative)
artifacts.append(source)
with (output/'SHA256SUMS.txt').open('w') as out:
    for p in artifacts:
        with p.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        out.write(f'{digest}  {p.name}\n')
print('Release checksums written.',flush=True)
