"""Normalize Docker/OCI exports to classic docker-save archives for NAS importers."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

SKOPEO_IMAGE = 'quay.io/skopeo/stable@sha256:e5d5d2815b94d74767d40be7111647c3e9cdc84406a638ffe1c7ea1f286f52a3'


def metadata(path):
    with tarfile.open(path, 'r:*') as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        if len(manifest) != 1:
            raise ValueError('Release archives must contain exactly one image.')
        config = archive.extractfile(manifest[0]['Config']).read()
        return manifest[0], config


def verify_classic(path, original_config, architecture):
    """Check image identity, layer contents and legacy importer metadata."""
    with tarfile.open(path, 'r:*') as archive:
        names = set(archive.getnames())
        if 'oci-layout' in names or 'index.json' in names or 'repositories' not in names:
            raise ValueError('Expected classic Docker archive with repositories metadata.')
        manifest = json.load(archive.extractfile('manifest.json'))
        if len(manifest) != 1:
            raise ValueError('Expected one image.')
        entry = manifest[0]
        config_bytes = archive.extractfile(entry['Config']).read()
        if config_bytes != original_config:
            raise ValueError('Conversion changed image configuration.')
        config = json.loads(config_bytes)
        if config['architecture'] != architecture or config['os'] != 'linux':
            raise ValueError('Wrong image architecture.')
        diff_ids = config['rootfs']['diff_ids']
        if len(entry['Layers']) != len(diff_ids):
            raise ValueError('Layer count changed.')
        for layer, expected in zip(entry['Layers'], diff_ids):
            with archive.extractfile(layer) as stream:
                actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected:
                raise ValueError('Layer contents changed.')
        repositories = json.load(archive.extractfile('repositories'))
        for tag in entry['RepoTags']:
            repo, version = tag.rsplit(':', 1)
            top = repositories[repo][version]
            if not {top+'/json', top+'/VERSION', top+'/layer.tar'} <= names:
                raise ValueError('Missing legacy layer metadata.')


def convert(source, destination, image, architecture):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    _, original_config = metadata(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.docker-export-', dir=destination.parent) as tmp:
        work = Path(tmp)
        (work/'tmp').mkdir()
        subprocess.run([
            'docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--user', f'{os.getuid()}:{os.getgid()}',
            '-v', f'{source}:/input/image.tar:ro', '-v', f'{work}:/output',
            SKOPEO_IMAGE, '--tmpdir', '/output/tmp', '--override-os', 'linux',
            '--override-arch', architecture, 'copy', 'docker-archive:/input/image.tar',
            f'docker-archive:/output/image.tar:{image}',
        ], check=True)
        verify_classic(work/'image.tar', original_config, architecture)
        with (work/'image.tar').open('rb') as src, gzip.open(work/'image.tar.gz', 'wb', compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.replace(work/'image.tar.gz', destination)
