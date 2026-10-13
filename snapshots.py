"""Bounded, authenticated JPEG snapshots. Camera secrets never enter child argv."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dual_camera import M1, M2, TT, node, envelope, error
from discovery import media_config, value

PATH = '/snapshot.jpg'
REALM = 'Unifi Camera Bridge'


def uri_response(op, public, tokens):
    ns = op.tag[1:].split('}')[0]
    if ns not in (M1, M2) or value(op, 'ProfileToken') not in tokens:
        return error('InvalidArgVal', 'Unknown snapshot profile')
    root, r = envelope(ns, 'GetSnapshotUri')
    if ns == M1:
        r = node(r, M1, 'MediaUri')
        node(r, TT, 'Uri', public + PATH)
        node(r, TT, 'InvalidAfterConnect', 'false')
        node(r, TT, 'InvalidAfterReboot', 'false')
        node(r, TT, 'Timeout', 'PT0S')
    else:
        node(r, M2, 'Uri', public + PATH)
    return 200, ET.tostring(root, encoding='utf-8', xml_declaration=True)


def advertise(data):
    root = ET.fromstring(data)
    for e in root.iter():
        if 'SnapshotUri' in e.attrib: e.set('SnapshotUri', 'true')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


class Service:
    def __init__(self, camera, uri, local=False):
        self.camera, self.uri, self.local = camera, uri, local
        self.secret = secrets.token_bytes(32)
        self.lock, self.auth_lock = threading.Lock(), threading.Lock()
        self.used = {}
        self.cached, self.cached_at = None, 0

    def challenge(self):
        stamp = str(int(time.time()))
        nonce = stamp + ':' + hmac.new(self.secret, stamp.encode(), hashlib.sha256).hexdigest()
        return f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"'

    def authenticated(self, header, path):
        if not header or not header.startswith('Digest ') or len(header) > 8192: return False
        try:
            parts = urllib.request.parse_http_list(header[7:])
            d = urllib.request.parse_keqv_list(parts)
            if len(d) != len(parts): return False
            if d.get('realm') != REALM or d.get('uri') != path or d.get('qop') != 'auth': return False
            if d.get('algorithm', 'MD5').upper() != 'MD5': return False
            if not hmac.compare_digest(d.get('username', '').encode(), self.camera['username'].encode()): return False
            stamp, signature = d['nonce'].split(':')
            age = time.time() - int(stamp)
            if not 0 <= age <= 120: return False
            if not hmac.compare_digest(signature, hmac.new(self.secret, stamp.encode(), hashlib.sha256).hexdigest()): return False
            if len(d['nc']) != 8 or not d.get('cnonce'): return False
            count = int(d['nc'], 16)
            md5 = lambda x: hashlib.md5(x.encode()).hexdigest()
            a1 = md5(f"{self.camera['username']}:{REALM}:{self.camera['password']}")
            expected = md5(f"{a1}:{d['nonce']}:{d['nc']}:{d['cnonce']}:auth:{md5('GET:'+path)}")
            if not hmac.compare_digest(d.get('response', ''), expected): return False
            key = (d['nonce'], d['cnonce'])
            with self.auth_lock:
                self.used = {k:v for k,v in self.used.items() if time.time()-v[1] <= 120}
                if count <= self.used.get(key, (0, 0))[0]: return False
                if key not in self.used and len(self.used) >= 1024: return False
                self.used[key] = (count, time.time())
            return True
        except (ValueError, KeyError, TypeError): return False

    def capture(self):
        if not self.lock.acquire(timeout=10): raise TimeoutError()
        try:
            if self.cached is not None and time.monotonic()-self.cached_at < 1: return self.cached
            if self.local:
                data = self.frame(self.uri)
            else:
                with socket.socket() as s:
                    s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
                with tempfile.TemporaryDirectory(prefix='snapshot-', dir=os.environ.get('RUNTIME_DIR', '/run/bridge')) as folder:
                    path = Path(folder)/'media.json'
                    path.write_text(json.dumps(media_config(self.camera, {'snapshot': self.uri}, port)))
                    path.chmod(0o600)
                    proxy = subprocess.Popen(['/mediamtx', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        time.sleep(.4)
                        data = self.frame(f'rtsp://127.0.0.1:{port}/snapshot')
                    finally:
                        proxy.terminate()
                        try: proxy.wait(timeout=2)
                        except subprocess.TimeoutExpired: proxy.kill(); proxy.wait()
            self.cached, self.cached_at = data, time.monotonic()
            return data
        finally: self.lock.release()

    @staticmethod
    def frame(uri):
        result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet',
            '-rtsp_transport', 'tcp', '-timeout', '6000000', '-threads', '2',
            '-i', uri, '-map', '0:v:0', '-frames:v', '1', '-an',
            '-vf', "scale=w='min(1280,iw)':h=-2", '-threads', '2', '-c:v', 'mjpeg',
            '-q:v', '3', '-f', 'image2pipe', '-'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=9)
        data = result.stdout
        if result.returncode or not 4 <= len(data) <= 4*1024*1024 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
            raise ValueError('Snapshot unavailable')
        return data

    def serve(self, handler):
        if not self.authenticated(handler.headers.get('Authorization'), handler.path):
            status, data, mime = 401, b'Authentication required', 'text/plain'
        else:
            try: status, data, mime = 200, self.capture(), 'image/jpeg'
            except (OSError, ValueError, subprocess.SubprocessError):
                status, data, mime = 503, b'Snapshot temporarily unavailable', 'text/plain'
            print(json.dumps({'snapshot_status': status}), flush=True)
        handler.send_response(status)
        if status == 401: handler.send_header('WWW-Authenticate', self.challenge())
        handler.send_header('Content-Type', mime)
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.wfile.write(data)
        handler.close_connection = True
