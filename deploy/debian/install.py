#!/usr/bin/env python3
"""Debian host installer. No camera credentials are read or printed."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

BUNDLE = Path(__file__).resolve().parent
INSTALL_DIR = Path('/opt/unifi-protect-bridge')
LEGACY_INSTALL_DIR = Path('/opt/sonoff-protect-bridge')
OS_RELEASE = Path('/etc/os-release')
SYSTEMD_DIR = Path('/run/systemd/system')
LOCK_PATH = Path('/run/lock/sonoff-protect-bridge-install.lock')
CONTAINER = 'sonoff-protect-bridge'
VOLUME = 'sonoff-protect-bridge-data'
PROJECT = 'sonoff-protect-bridge'
LABEL = 'io.sonoff-protect-bridge.installer'
MARKER = 'debian-v1'
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
PRIVATE = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))

class InstallError(Exception):
    pass


def installation_source():
    """Find an existing managed installation without changing either directory."""
    for directory in (INSTALL_DIR, LEGACY_INSTALL_DIR):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise InstallError(f'Installation path is not a regular directory: {directory}. No changes made.')
    if LEGACY_INSTALL_DIR.exists():
        if INSTALL_DIR.exists():
            raise InstallError('Both old and new installation directories exist. Review them before retrying; no changes made.')
        state_path = LEGACY_INSTALL_DIR/'installer-state.json'
        compose_path = LEGACY_INSTALL_DIR/'compose.yaml'
        if not state_path.is_file() or not compose_path.is_file():
            raise InstallError('Old installation directory is unmanaged; no changes made.')
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict) or not state or f'{LABEL}: {MARKER}' not in compose_path.read_text():
            raise InstallError('Old installation directory is unmanaged; no changes made.')
        return LEGACY_INSTALL_DIR
    return INSTALL_DIR


def run(args, *, capture=False, check=True):
    env = os.environ.copy()
    # Never deploy to a remote Docker context inherited from the caller.
    for name in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        env.pop(name, None)
    env['DEBIAN_FRONTEND'] = 'noninteractive'
    result = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode:
        raise InstallError(f'Command failed: {" ".join(args[:4])} (exit {result.returncode}).')
    return result


def parse_os(text):
    fields = {}
    for line in text.splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            fields[key] = value.strip().strip('"').strip("'")
    if fields.get('ID') != 'debian' or fields.get('VERSION_ID') not in ('12', '13'):
        raise InstallError('Supported hosts are Debian 12 and Debian 13, not derivatives or QNAP QTS.')
    return {'12':'bookworm','13':'trixie'}[fields['VERSION_ID']]


def private_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and any(ip in network for network in PRIVATE)
    except ValueError:
        return False


def choose_address(interfaces, routes, override=None):
    assigned = {a['local'] for interface in interfaces for a in interface.get('addr_info', [])
                if a.get('family') == 'inet' and private_ip(a.get('local', ''))}
    if override:
        if override not in assigned:
            raise InstallError('The requested bind address must be a private IPv4 assigned to this host.')
        return override
    candidates = {}
    for interface in interfaces:
        name = interface['ifname']
        if name.startswith(('lo','docker','br-','veth','tun','tap','wg','tailscale','virbr','vboxnet')):
            continue
        values = [a['local'] for a in interface.get('addr_info', [])
                  if a.get('family') == 'inet' and private_ip(a.get('local','')) and not a.get('secondary')]
        if values:
            candidates[name] = values
    defaults = [r for r in routes if r.get('dev') in candidates and r.get('dst','default') == 'default']
    if defaults:
        metric = min(r.get('metric',0) for r in defaults)
        preferred = {ip for r in defaults if r.get('metric',0)==metric for ip in candidates[r['dev']]}
        if len(preferred) == 1:
            return preferred.pop()
    addresses = {ip for values in candidates.values() for ip in values}
    if len(addresses) == 1:
        return addresses.pop()
    available = ', '.join(sorted(assigned)) or 'none'
    raise InstallError('Cannot choose one LAN address. Rerun with --bind-address LAN_IP. Assigned private addresses: '+available)


def verify_payload(bundle):
    metadata = json.loads((bundle/'release.json').read_text())
    for name, expected in metadata['sha256'].items():
        if Path(name).name != name:
            raise InstallError('Invalid release filename.')
        with (bundle/name).open('rb') as f:
            actual = hashlib.file_digest(f, 'sha256').hexdigest()
        if actual != expected:
            raise InstallError(f'Package checksum failed: {name}. Download and extract it again.')
    if metadata.get('distribution') == 'registry':
        registry_image(metadata.get('image', ''))
    return metadata


def registry_image(image):
    if not isinstance(image, str) or not re.fullmatch(r'ghcr\.io/[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_./-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})', image):
        raise InstallError('Invalid GHCR image reference in the release package.')
    return image


def compose_text(template, version, arch, bind, admin_port, image=None):
    if not private_ip(bind) or arch not in ('amd64','arm64'):
        raise InstallError('Invalid deployment address or architecture.')
    if not (1024 <= admin_port <= 65535) or admin_port in range(8100,8116) or admin_port in range(8600,8616):
        raise InstallError('Admin port must be 1024–65535 and outside the camera port ranges.')
    if image is not None:
        registry_image(image)
    rendered = (template.replace('VERSION',version).replace('ARCH',arch)
            .replace('${ADMIN_BIND_IP:-127.0.0.1}:${ADMIN_PORT:-18080}',f'{bind}:{admin_port}')
            .replace("'8100-8115:8100-8115/tcp'",f"'{bind}:8100-8115:8100-8115/tcp'")
            .replace("'8600-8615:8600-8615/tcp'",f"'{bind}:8600-8615:8600-8615/tcp'")
            .replace('    restart: unless-stopped',f'    labels:\n      {LABEL}: {MARKER}\n    restart: unless-stopped')
            .replace('    name: sonoff-protect-bridge-data',f'    name: sonoff-protect-bridge-data\n    labels:\n      {LABEL}: {MARKER}'))
    if image is not None:
        rendered = rendered.replace(f'local/sonoff-protect-bridge:{version}-{arch}', image).replace('    pull_policy: never\n', '')
    return rendered


def inspect_container():
    result = run(DOCKER+['container','inspect',CONTAINER],capture=True,check=False)
    if result.returncode:
        return None
    container = json.loads(result.stdout)[0]
    labels = container.get('Config',{}).get('Labels') or {}
    if labels.get(LABEL) != MARKER or labels.get('com.docker.compose.project') != PROJECT:
        raise InstallError('A container named sonoff-protect-bridge already exists and was not created by this installer. It has been left untouched.')
    mounts = container.get('Mounts', [])
    if not any(m.get('Type')=='volume' and m.get('Name')==VOLUME and m.get('Destination')=='/data' for m in mounts):
        raise InstallError('Existing bridge has an unexpected data mount; no changes made.')
    return container



def check_volume(existing, has_state):
    if existing or has_state:
        return
    result=run(DOCKER+['volume','inspect','--format','{{json .Labels}}',VOLUME],capture=True,check=False)
    if result.returncode==0 and (json.loads(result.stdout) or {}).get(LABEL)!=MARKER:
        raise InstallError('A bridge settings volume exists outside this installer. Preserve it and review migration before a new install.')


def foreign_published_ports(bind):
    result=run(DOCKER+['ps','--format','{{.Names}}'],capture=True)
    occupied=set()
    for name in result.stdout.splitlines():
        if name==CONTAINER:
            continue
        data=run(DOCKER+['inspect','--format','{{json .NetworkSettings.Ports}}',name],capture=True)
        for entries in (json.loads(data.stdout) or {}).values():
            for entry in entries or []:
                if entry['HostIp'] in (bind,'0.0.0.0','::'):
                    occupied.add(int(entry['HostPort']))
    return occupied


def check_ports(bind, admin_port, existing=None, published=()):
    owned = set()
    if existing and existing.get('State',{}).get('Running'):
        for mappings in (existing.get('NetworkSettings',{}).get('Ports') or {}).values():
            for mapping in mappings or []:
                if mapping['HostIp'] in (bind,'0.0.0.0','::'):
                    owned.add(int(mapping['HostPort']))
    busy = []
    for port in [admin_port,*range(8100,8116),*range(8600,8616)]:
        if port in published:
            busy.append(port);continue
        if port in owned:
            continue
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
            try:
                probe.bind((bind,port))
            except OSError:
                busy.append(port)
    if busy:
        raise InstallError('Required TCP ports are unavailable: '+', '.join(map(str,busy))+'. No running containers were stopped. Use --admin-port for an admin-port conflict.')


def installed(package):
    result=run(['dpkg-query','-W','-f=${Status}',package],capture=True,check=False)
    return result.returncode == 0 and result.stdout.strip() == 'install ok installed'


def ensure_docker(codename, arch):
    has_cli=shutil.which('docker') is not None
    if has_cli and installed('docker-ce'):
        run(['systemctl','start','docker'])
    # Preserve an existing working engine and Compose installation.
    if has_cli and run(DOCKER+['info'],capture=True,check=False).returncode==0 and run(DOCKER+['compose','version'],capture=True,check=False).returncode==0:
        return
    conflicts=[p for p in ('docker.io','docker-compose','docker-buildx','podman-docker','containerd','runc') if installed(p)]
    if conflicts:
        raise InstallError('Existing container packages need review before installing Docker CE: '+', '.join(conflicts)+'. No packages were removed. A working Docker engine with Compose v2 can be reused.')
    key_path=Path('/etc/apt/keyrings/sonoff-protect-bridge-docker.asc')
    source_path=Path('/etc/apt/sources.list.d/sonoff-protect-bridge-docker.sources')
    # Reuse an existing Docker repository instead of adding a conflicting Signed-By.
    sources=[Path('/etc/apt/sources.list')]
    sources+=list(Path('/etc/apt/sources.list.d').glob('*.list'))+list(Path('/etc/apt/sources.list.d').glob('*.sources'))
    has_repo=any('https://download.docker.com/linux/debian' in p.read_text() for p in sources if p.is_file())
    if not has_repo:
        print('Adding Docker’s official Debian package repository.',flush=True)
        with urllib.request.urlopen('https://download.docker.com/linux/debian/gpg',timeout=30) as response:
            key=response.read(65536)
        if not key.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
            raise InstallError('Docker signing-key download was invalid.')
        key_path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        key_path.write_bytes(key);key_path.chmod(0o644)
        source_path.write_text(f'Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key_path}\n')
        source_path.chmod(0o644)
    run(['apt-get','update'])
    run(['apt-get','install','-y','--no-remove','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin'])
    run(['systemctl','enable','--now','docker'])
    run(DOCKER+['info'],capture=True)
    run(DOCKER+['compose','version'],capture=True)


def wait_healthy(timeout=120):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=run(DOCKER+['inspect','--format','{{json .State}}',CONTAINER],capture=True,check=False)
        if result.returncode==0:
            state=json.loads(result.stdout)
            if state.get('Health',{}).get('Status')=='healthy':
                return
            if state.get('Status') in ('exited','dead'):
                break
        time.sleep(2)
    raise InstallError('Bridge did not become healthy. See the container logs for details.')


def deploy(compose, state, rollback_image=None):
    INSTALL_DIR.mkdir(mode=0o755,parents=True,exist_ok=True)
    compose_path=INSTALL_DIR/'compose.yaml'
    previous=compose_path.read_bytes() if compose_path.exists() else None
    if previous is not None and rollback_image:
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', rollback_image):
            raise InstallError('Invalid previous image identity; deployment stopped.')
        previous = re.sub(rb'(?m)^    image:.*$', b'    image: '+rollback_image.encode(), previous, count=1)
    if previous is not None and not (INSTALL_DIR/'installer-state.json').exists():
        raise InstallError('The installation directory contains an unmanaged Compose file; no changes made.')
    candidate=INSTALL_DIR/'compose.pending.yaml'
    candidate.write_text(compose);candidate.chmod(0o600)
    base=DOCKER+['compose','--project-name',PROJECT]
    try:
        run(base+['-f',str(candidate),'config','--quiet'],capture=True)
        os.replace(candidate,compose_path)
        try:
            run(base+['-f',str(compose_path),'up','-d','--no-build','--pull','never'])
            wait_healthy()
        except InstallError:
            if previous is not None:
                compose_path.write_bytes(previous);compose_path.chmod(0o600)
                restored=run(base+['-f',str(compose_path),'up','-d','--no-build','--pull','never'],check=False)
                message='Previous configuration restored; restart requested.' if restored.returncode==0 else 'Previous configuration restored, but its restart failed; inspect container logs.'
                print(message+' Settings volume retained.',file=sys.stderr)
            else:
                # Keep the settings volume even if setup was attempted during startup.
                run(base+['-f',str(compose_path),'down','--timeout','20'],check=False)
                compose_path.unlink(missing_ok=True)
                print('Failed first deployment removed; settings volume retained.',file=sys.stderr)
            raise
        state_path=INSTALL_DIR/'installer-state.json'
        pending=state_path.with_suffix('.tmp')
        pending.write_text(json.dumps(state,indent=2)+'\n');pending.chmod(0o600);os.replace(pending,state_path)
    finally:
        candidate.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Read-only preflight; do not install or start anything')
    parser.add_argument('--bind-address',help='Private LAN IPv4 override for hosts with multiple networks')
    parser.add_argument('--admin-port',type=int,help='Admin port (default 18080, or preserved on updates)')
    args=parser.parse_args(argv)
    codename=parse_os(OS_RELEASE.read_text())
    if not SYSTEMD_DIR.is_dir():
        raise InstallError('Run on a Debian host or VM with systemd, not inside a container or chroot.')
    arch=run(['dpkg','--print-architecture'],capture=True).stdout.strip()
    if arch not in ('amd64','arm64'):
        raise InstallError('This release supports amd64 and arm64 only.')
    release=verify_payload(BUNDLE)
    source=installation_source()
    old_path=source/'installer-state.json'
    old=json.loads(old_path.read_text()) if old_path.exists() else {}
    interfaces=json.loads(run(['ip','-j','-4','address','show','scope','global'],capture=True).stdout)
    routes=json.loads(run(['ip','-j','-4','route','show','default'],capture=True).stdout)
    bind=choose_address(interfaces,routes,args.bind_address or old.get('bind_address'))
    admin_port=args.admin_port if args.admin_port is not None else old.get('admin_port',18080)
    image=release.get('image') if release.get('distribution') == 'registry' else None
    compose=compose_text((BUNDLE/'compose.template.yaml').read_text(),release['version'],arch,bind,admin_port,image)
    ready=shutil.which('docker') and run(DOCKER+['info'],capture=True,check=False).returncode==0
    compose_ready=ready and run(DOCKER+['compose','version'],capture=True,check=False).returncode==0
    if not compose_ready:
        conflicts=[p for p in ('docker.io','docker-compose','docker-buildx','podman-docker','containerd','runc') if installed(p)]
        if conflicts:
            raise InstallError('Existing container packages need review: '+', '.join(conflicts)+'. No packages were removed.')
    existing=inspect_container() if ready else None
    if ready:
        check_volume(existing,bool(old))
    if (source/'compose.yaml').exists() and not old:
        raise InstallError('Installation directory contains an unmanaged Compose file; no changes made.')
    check_ports(bind,admin_port,existing,foreign_published_ports(bind) if ready else ())
    if shutil.disk_usage('/var').free < 2*1024**3:
        raise InstallError('At least 2 GiB free under /var is required for Docker and the image.')
    print(f'Debian {codename}, {arch}. Bridge address: {bind}. Admin page: http://{bind}:{admin_port}',flush=True)
    if source == LEGACY_INSTALL_DIR:
        print(f'Existing installation will move to {INSTALL_DIR}. Camera settings and Docker identities are retained.',flush=True)
    if args.check:
        print('Preflight passed. No changes made. Docker will be installed if needed.');return
    if os.geteuid()!=0:
        raise InstallError('Run with sudo: sudo ./install.sh')
    LOCK_PATH.parent.mkdir(exist_ok=True)
    with LOCK_PATH.open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise InstallError('Another bridge installer is running.')
        if installation_source() != source:
            raise InstallError('Installation directory changed during preflight. Rerun the installer.')
        ensure_docker(codename,arch)
        engine_arch=run(DOCKER+['info','--format','{{.Architecture}}'],capture=True).stdout.strip()
        if engine_arch not in {'amd64':('amd64','x86_64'),'arm64':('arm64','aarch64')}[arch]:
            raise InstallError('The local Docker engine architecture does not match this host.')
        # Recheck now that Docker is running and immediately before deployment.
        existing=inspect_container()
        check_volume(existing,bool(old))
        check_ports(bind,admin_port,existing,foreign_published_ports(bind))
        rollback_image=existing.get('Image') if existing else None
        installed_version=release['version']
        if image:
            print('Pulling the bridge image from GitHub Container Registry. The current bridge keeps running during download.',flush=True)
            run(DOCKER+['pull','--platform',f'linux/{arch}',image])
            details=json.loads(run(DOCKER+['image','inspect','--format','{{json .}}',image],capture=True).stdout)
            if details.get('Architecture')!=arch or details.get('Os')!='linux':
                raise InstallError('Pulled image does not match this host. Existing bridge left running.')
            installed_version=(details.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.version','unknown')
        else:
            image=f'local/sonoff-protect-bridge:{release["version"]}-{arch}'
            print('Loading the bundled bridge image.',flush=True)
            run(DOCKER+['load','-i',str(BUNDLE/f'image-{arch}.tar.gz')])
        run(['systemctl','enable','--now','docker'])
        if source == LEGACY_INSTALL_DIR:
            # Move only after a successful image pull/load. The named Docker
            # volume and project are unchanged, so camera identities survive.
            if INSTALL_DIR.exists():
                raise InstallError('New installation directory appeared during installation; no files moved.')
            source.rename(INSTALL_DIR)
        deploy(compose,{'version':installed_version,'image':image,'architecture':arch,'bind_address':bind,'admin_port':admin_port},rollback_image)

    print(f'\nUnifi Camera Bridge is ready: http://{bind}:{admin_port}\nOpen this address in your browser. Sign in, or choose an admin password for a new installation.\nSettings are retained across restarts and installer reruns.')

if __name__=='__main__':
    try:main()
    except (InstallError,OSError,ValueError,KeyError) as error:
        print(f'Installation stopped: {error}',file=sys.stderr);sys.exit(1)
