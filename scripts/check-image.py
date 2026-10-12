#!/usr/bin/env python3
"""Check a release image in an isolated container with disposable settings."""
import argparse
import json
import secrets
import subprocess
import time

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('image');p.add_argument('--platform',required=True,choices=['linux/amd64','linux/arm64'])
p.add_argument('--version',required=True);a=p.parse_args()
name='unifi-bridge-check-'+secrets.token_hex(5)
def docker(*args):return subprocess.check_output(['docker',*args],stderr=subprocess.PIPE,text=True).strip()
try:
    docker('run','-d','--name',name,'--platform',a.platform,'--network','none','--read-only','--cap-drop','ALL',
           '--security-opt','no-new-privileges:true','--health-interval','2s','--health-start-period','2s',
           '--tmpfs','/data:rw,size=16m,uid=65532,gid=65532,mode=0700',
           '--tmpfs','/run/bridge:rw,size=16m,uid=65532,gid=65532,mode=0700',
           '--tmpfs','/tmp:rw,size=32m,uid=65532,gid=65532,mode=0700',a.image)
    for _ in range(90):
        state=json.loads(docker('inspect','--format','{{json .State}}',name))
        if state.get('Health',{}).get('Status')=='healthy':break
        if state.get('Status') in ('exited','dead'):raise RuntimeError('Image exited during startup.')
        time.sleep(1)
    else:raise RuntimeError('Image health check timed out.')
    version=docker('exec',name,'python','-c','import manager; print(manager.VERSION)')
    if version!=a.version:raise RuntimeError('Application version does not match release tag.')
    print(a.platform+': application starts, becomes healthy and reports '+version)
finally:
    subprocess.run(['docker','rm','-f','-v',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
