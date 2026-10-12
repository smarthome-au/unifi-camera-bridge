"""Private stream relay; native video/audio copied, only mismatched LQ encoded."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from discovery import media_config

STOP = False


def configuration(camera, port):
    paths, commands = {}, []
    permissions = []
    for i, view in enumerate(camera['discovery']['views']):
        for stream in view['streams']:
            output = f'view{i}_{stream["quality"]}'
            source = 'input_' + output if stream['convert_video'] else output
            paths[source] = stream['uri']
            permissions.append({'action': 'read', 'path': output})
            if stream['convert_video']:
                codec = 'libx265' if stream['output_codec'] == 'H265' else 'libx264'
                cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet', '-rtsp_transport', 'tcp', '-timeout', '15000000', '-i', f'rtsp://127.0.0.1:{port}/{source}', '-map', '0:v:0', '-map', '0:a:0?', '-threads', '2', '-c:v', codec, '-preset', 'veryfast', '-tune', 'zerolatency', '-pix_fmt', 'yuv420p', '-crf', '25']
                if codec == 'libx265': cmd += ['-x265-params', 'pools=1:frame-threads=1:log-level=none']
                cmd += ['-c:a', 'copy', '-f', 'rtsp', '-rtsp_transport', 'tcp', f'rtsp://127.0.0.1:{port}/{output}']
                commands.append(cmd)
    config = media_config(camera, paths, port)
    config['rtspAuthMethods'] = ['basic', 'digest']
    config['authInternalUsers'] = [
        {'user': camera['username'], 'pass': camera['password'], 'permissions': permissions},
        {'user': 'any', 'ips': ['127.0.0.1'], 'permissions': [{'action': 'read'}, {'action': 'publish'}]}]
    for cmd in commands:
        config['paths'][cmd[-1].rsplit('/', 1)[-1]] = {'source': 'publisher', 'overridePublisher': False}
    return config, commands


def stop(*_):
    global STOP
    STOP = True


def run():
    os.umask(0o077)
    path = Path(os.environ['GENERIC_CAMERA_FILE'])
    camera = json.loads(path.read_text())
    config, commands = configuration(camera, int(os.environ['GENERIC_MEDIA_PORT']))
    config_path = path.parent/'generic-media.json'
    config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    children = []
    try:
        server = subprocess.Popen(['/mediamtx', str(config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        children.append(server)
        time.sleep(.5)
        encoders = {}
        while not STOP:
            if server.poll() is not None:
                print(json.dumps({"worker_event":"media_exit"}),flush=True)
                return 1
            for i, cmd in enumerate(commands):
                if i not in encoders or encoders[i].poll() is not None:
                    if i in encoders: print(json.dumps({"worker_event":"encoder_exit"}),flush=True)
                    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    encoders[i] = p
                    children = [server] + list(encoders.values())
            time.sleep(3)
    finally:
        for p in children:
            if p.poll() is None: p.terminate()
        for p in children:
            try: p.wait(timeout=4)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
        config_path.unlink(missing_ok=True)
    return 0

if __name__ == '__main__':
    try: raise SystemExit(run())
    except Exception:
        print('Automatic media bridge could not start; check camera settings.', flush=True)
        raise SystemExit(1)
