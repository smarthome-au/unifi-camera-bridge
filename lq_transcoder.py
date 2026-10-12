"""Optional LQ-only transcoder. Secrets never enter argv, environment or logs."""
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

OUTPUTS = ['av_stream1/sec', 'av_stream0/sec']
STOP = False
RTSP_PORT = int(os.environ.get('TRANSCODER_RTSP_PORT', '8554'))
API_PORT = int(os.environ.get('TRANSCODER_API_PORT', '9997'))
RUN_DIR = Path(os.environ.get('TRANSCODER_RUN_DIR', '/run/transcoder'))


def configuration(credentials, camera, camera_port=554):
    ipaddress.ip_address(camera)
    user, password = credentials['username'], credentials['password']
    if not user or not password or user == 'any':
        raise ValueError('Invalid camera credentials')
    auth = urllib.parse.quote(user, safe='') + ':' + urllib.parse.quote(password, safe='')
    config = {
        'logLevel': 'error', 'rtspAddress': f'127.0.0.1:{RTSP_PORT}', 'rtspTransports': ['tcp'],
        'rtspAuthMethods': ['basic', 'digest'],
        'rtmp': False, 'hls': False, 'webrtc': False, 'srt': False, 'moq': False,
        'api': True, 'apiAddress': f'127.0.0.1:{API_PORT}',
        'authMethod': 'internal',
        'authInternalUsers': [
            {'user': user, 'pass': password, 'permissions': [
                {'action': 'read', 'path': path} for path in OUTPUTS]},
            {'user': 'any', 'ips': ['127.0.0.1', '::1'], 'permissions': [
                {'action': 'read'}, {'action': 'publish'}, {'action': 'api'}]},
        ],
        'paths': {}
    }
    for index, path in enumerate(OUTPUTS):
        config['paths'][f'input{index}'] = {
            'source': f'rtsp://{auth}@{camera}:{camera_port}/{path}',
            'rtspTransport': 'tcp', 'sourceOnDemand': False}
        config['paths'][path] = {'source': 'publisher', 'overridePublisher': False}
    return config


def command(index):
    # Camera authentication lives solely in MediaMTX's tmpfs configuration.
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet',
            '-rtsp_transport', 'tcp', '-timeout', '15000000',
            '-i', f'rtsp://127.0.0.1:{RTSP_PORT}/input{index}',
            '-map', '0:v:0', '-map', '0:a:0?', '-threads', '2',
            '-c:v', 'libx265', '-preset', 'veryfast', '-tune', 'zerolatency',
            '-pix_fmt', 'yuv420p', '-crf', '25', '-maxrate', '5000k', '-bufsize', '1000k',
            '-x265-params', 'pools=1:frame-threads=1:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1:log-level=none',
            '-c:a', 'copy', '-f', 'rtsp', '-rtsp_transport', 'tcp',
            f'rtsp://127.0.0.1:{RTSP_PORT}/{OUTPUTS[index]}']


def healthy():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{API_PORT}/v3/paths/list', timeout=3) as r:
            paths = {p['name']: p for p in json.load(r)['items']}
        return all(paths.get(p, {}).get('ready') for p in OUTPUTS)
    except Exception:
        return False


def stop(*_):
    global STOP
    STOP = True


def run():
    os.umask(0o077)
    credentials = json.loads(Path(os.environ.get('CAMERA_LOGIN_FILE', '/run/secrets/camera_login')).read_text())
    config = configuration(credentials, os.environ['CAMERA_IP'], int(os.environ.get('CAMERA_RTSP_PORT', '554')))
    path = RUN_DIR / 'mediamtx.json'
    path.write_text(json.dumps(config))
    del credentials, config
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = {}
    try:
        # Child output may contain source URLs on errors, so never forward it.
        children['server'] = subprocess.Popen(['/mediamtx', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        retry = [0, 0]
        while not STOP:
            if children['server'].poll() is not None:
                print('LQ RTSP server exited; restarting container.', flush=True)
                return 1
            for i in range(2):
                p = children.get(i)
                if p is not None and p.poll() is not None:
                    print(f'LQ encoder {i} exited ({p.returncode}); retrying.', flush=True)
                    del children[i]
                    retry[i] = time.monotonic() + 5
                if i not in children and time.monotonic() >= retry[i]:
                    children[i] = subprocess.Popen(command(i), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f'LQ encoder {i} started.', flush=True)
            time.sleep(1)
    finally:
        for p in children.values():
            if p.poll() is None: p.terminate()
        for p in children.values():
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        path.unlink(missing_ok=True)
    return 0


if __name__ == '__main__':
    if '--healthcheck' in sys.argv:
        sys.exit(0 if healthy() else 1)
    try:
        sys.exit(run())
    except Exception:
        print('LQ transcoder startup failed; check secret and camera settings.', flush=True)
        sys.exit(1)
