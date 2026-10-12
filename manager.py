"""Unifi Camera Bridge manager; no Docker socket or external database."""
import base64
import copy
import datetime
import hashlib
import hmac
import http.client
import http.cookies
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import discovery
import rtsp_camera
import presets
import diagnostics

VERSION = '1.6.1'
APP = Path(__file__).resolve().parent
PRIVATE = [ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]


def address(value):
    value = str(value).strip().lower()
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('Enter an IPv4 address or hostname, without a URL or port.')
    if '..' in value:
        raise ValueError('Invalid hostname.')
    try:
        ip = ipaddress.ip_address(value)
        if ip.version != 4 or not any(ip in n for n in PRIVATE):
            raise ValueError('Use a private LAN IPv4 address.')
    except ValueError:
        if re.fullmatch(r'[0-9.]+', value):
            raise ValueError('Use a private LAN IPv4 address.')
    return value



def suggested_host(authority, excluded=()):
    """Suggest a private IPv4 used to reach the web interface, without DNS.

    Docker's internal address or gateway is not a reliable host LAN address.
    Forwarded headers are intentionally ignored; the suggestion stays editable.
    """
    try:
        url = urllib.parse.urlsplit('http://' + authority)
        if url.username is not None or url.password is not None or url.path or url.query or url.fragment:
            return ''
        if url.port is not None and not 1 <= url.port <= 65535:
            return ''
        ip = ipaddress.ip_address(url.hostname or '')
        if ip.version == 4 and any(ip in n for n in PRIVATE) and str(ip) not in excluded:
            return str(ip)
    except (ValueError, TypeError):
        pass
    return ''


def resolved(value):
    ip = ipaddress.ip_address(socket.gethostbyname(value))
    if not any(ip in n for n in PRIVATE):
        raise ValueError('Address must resolve to a private LAN IPv4 address.')
    return str(ip)


def port(value):
    if isinstance(value, bool):
        raise ValueError('Invalid port.')
    n = int(value)
    if not 1024 <= n <= 65535:
        raise ValueError('Bridge ports must be between 1024 and 65535.')
    return n


def camera_port(value):
    n = int(value)
    if isinstance(value, bool) or not 1 <= n <= 65535:
        raise ValueError('Camera port must be between 1 and 65535.')
    return n


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {'salt': salt, 'digest': digest}


def check_password(password, record):
    return hmac.compare_digest(password_hash(password, record['salt'])['digest'], record['digest'])


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'config.json'
        self.lock = threading.RLock()
        self.capacity = int(os.environ.get('ENDPOINT_CAPACITY', '16'))
        self.onvif_base = port(os.environ.get('ONVIF_PORT_BASE', '8100'))
        self.rtsp_base = port(os.environ.get('RTSP_PORT_BASE', '8600'))
        self.admin_port = port(os.environ.get('ADMIN_PORT', '8080'))
        if not 2 <= self.capacity <= 100:
            raise ValueError('ENDPOINT_CAPACITY must be 2–100.')
        ranges = [set(range(b, b + self.capacity)) for b in (self.onvif_base, self.rtsp_base, 19000, 20000)]
        if any(max(r) > 65535 or self.admin_port in r for r in ranges) or any(a & b for i, a in enumerate(ranges) for b in ranges[i+1:]):
            raise ValueError('Configured port ranges overlap.')
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {'version': 1, 'revision': 0, 'host': '', 'cameras': [], 'admin': None}
        if self.data.get('version') != 1:
            raise ValueError('Unsupported configuration version.')
        slots = [e['slot'] for c in self.data['cameras'] for e in c['endpoints']]
        if len(set(slots)) != len(slots) or any(s < 0 or s >= self.capacity for s in slots):
            raise ValueError('Stored endpoints exceed configured capacity or overlap.')
        # Remove obsolete setup codes when upgrading an unconfigured installation.
        (self.directory / 'setup-token').unlink(missing_ok=True)

    def save(self, data):
        data['revision'] = self.data['revision'] + 1
        atomic(self.path, data)
        self.data = data

    def public(self):
        with self.lock:
            data = copy.deepcopy(self.data)
        data.pop('admin')
        for c in data['cameras']:
            c['has_password'] = bool(c.pop('password', ''))
            if 'discovery' in c: c['discovery'] = discovery.public_report(c['discovery'])
            for e in c['endpoints']:
                e['onvif_port'] = self.onvif_base + e['slot']
                e['rtsp_port'] = self.rtsp_base + e['slot']
                e['url'] = f"http://{data['host']}:{e['onvif_port']}/onvif/device_service"
        data.update(capacity=self.capacity, onvif_base=self.onvif_base, rtsp_base=self.rtsp_base, app_version=VERSION)
        return data

    def setup(self, password, host):
        with self.lock:
            if self.data['admin']:
                raise ValueError('Setup is already complete. Please sign in.')
            if not 12 <= len(password) <= 256:
                raise ValueError('Use an admin password of 12–256 characters.')
            data = copy.deepcopy(self.data)
            data['admin'] = password_hash(password)
            data['host'] = address(host)
            self.save(data)

    def mutate(self, action, payload):
        with self.lock:
            if payload.get('revision') != self.data['revision']:
                raise ValueError('Settings changed in another window. Refresh and try again.')
            data = copy.deepcopy(self.data)
            if action == 'host':
                data['host'] = address(payload.get('host', ''))
            elif action == 'delete':
                if not any(c['id'] == payload.get('id') for c in data['cameras']):
                    raise ValueError('Camera not found.')
                data['cameras'] = [c for c in data['cameras'] if c['id'] != payload['id']]
            elif action == 'camera':
                old = next((c for c in data['cameras'] if c['id'] == payload.get('id')), None)
                if payload.get('id') and old is None:
                    raise ValueError('Camera not found.')
                model = payload.get('model')
                if model not in ('auto', 'cam-s3w', 'cam-c44he', 'cam-pt2', 'rtsp') or (old and model != old['model']):
                    raise ValueError('Choose a supported camera model. Model cannot change after creation.')
                name = str(payload.get('name', '')).strip()
                if not 1 <= len(name) <= 80 or any(ord(x) < 32 for x in name):
                    raise ValueError('Enter a camera name of 1–80 characters.')
                username = str(payload.get('username', '')).strip()
                password = payload.get('password') or (old['password'] if old else '')
                if not username or username == 'any' or len(username) > 128 or not isinstance(password, str) or not 1 <= len(password) <= 256:
                    raise ValueError('Enter the camera / Protect username and password. Username “any” is reserved.')
                enabled, ptz = payload.get('enabled', True), payload.get('ptz', model in ('cam-c44he', 'cam-pt2'))
                motion_events = payload.get('events', old.get('events', True) if old else True)
                if not isinstance(motion_events, bool): raise ValueError('Invalid events switch.')
                if not isinstance(enabled, bool) or not isinstance(ptz, bool):
                    raise ValueError('Invalid switch value.')
                camera = {'id': old['id'] if old else uuid.uuid4().hex, 'name': name, 'model': model,
                          'address': address(payload.get('address', '')), 'onvif_port': camera_port(payload.get('onvif_port', 80)),
                          'rtsp_port': camera_port(payload.get('rtsp_port', 554)), 'username': username, 'password': password,
                          'enabled': enabled, 'events': motion_events, 'ptz': ptz and model in ('cam-c44he','auto','cam-pt2')}
                if model == 'rtsp':
                    camera['rtsp_anonymous'] = payload.get('rtsp_anonymous', old.get('rtsp_anonymous', False) if old else False)
                    if not isinstance(camera['rtsp_anonymous'], bool): raise ValueError('Invalid RTSP authentication switch.')
                    if old and camera['rtsp_anonymous'] != old.get('rtsp_anonymous', False): raise ValueError('Inspect again after changing connection settings.')
                    camera['ptz'] = camera['events'] = False
                if model != 'rtsp' and any(c['model'] != 'rtsp' and c['id'] != camera['id'] and c['address'] == camera['address'] and c['onvif_port'] == camera['onvif_port'] for c in data['cameras']):
                    raise ValueError('This camera is already configured.')
                if model in presets.DISCOVERED_MODELS:
                    plan = payload.get('_discovery') or (old.get('discovery') if old else None)
                    if not plan: raise ValueError('Inspect the camera before saving.')
                    if old and not payload.get('_discovery') and any(camera[k] != old[k] for k in ('address','onvif_port','rtsp_port','username','password')):
                        raise ValueError('Inspect again after changing connection settings.')
                    if old and [v['source'] for v in plan['views']] != [v['source'] for v in old['discovery']['views']]:
                        raise ValueError('Detected sources changed; existing bridge identities must be reviewed.')
                    camera['discovery'] = copy.deepcopy(presets.apply(model, plan))
                    if model == 'rtsp':
                        main_uri = plan['views'][0]['streams'][0]['uri']
                        if any(c['id'] != camera['id'] and c['model'] == 'rtsp' and c['discovery']['views'][0]['streams'][0]['uri'] == main_uri for c in data['cameras']):
                            raise ValueError('This RTSP main stream is already configured.')
                if old:
                    camera['endpoints'] = old['endpoints']
                    data['cameras'][data['cameras'].index(old)] = camera
                else:
                    used = {e['slot'] for c in data['cameras'] for e in c['endpoints']}
                    free = [s for s in range(self.capacity) if s not in used]
                    lenses = ([f'view{i}' for i in range(len(camera['discovery']['views']))] if model in presets.DISCOVERED_MODELS else (['fixed', 'ptz'] if model == 'cam-c44he' else ['fixed']))
                    if len(free) < len(lenses):
                        raise ValueError('Endpoint capacity reached. Increase the published port range and capacity first.')
                    camera['endpoints'] = [{'lens': lens, 'slot': free[i], 'mac': '02:' + ':'.join(f'{b:02x}' for b in secrets.token_bytes(5))} for i, lens in enumerate(lenses)]
                    data['cameras'].append(camera)
            else:
                raise ValueError('Unknown action.')
            self.save(data)


def probe(camera):
    """Read-only ONVIF authentication check; never return raw camera errors or URLs."""
    conn = None
    try:
        host = resolved(camera['address'])
        if camera['model'] == 'rtsp': return rtsp_camera.probe(camera, host)
        nonce = secrets.token_bytes(20)
        created = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + camera['password'].encode()).digest()).decode()
        body = f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://www.onvif.org/ver10/device/wsdl"><s:Header><wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"><wsse:UsernameToken><wsse:Username>{escape(camera['username'])}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header><s:Body><d:GetDeviceInformation/></s:Body></s:Envelope>'''.encode()
        conn = http.client.HTTPConnection(host, camera['onvif_port'], timeout=6)
        conn.request('POST', '/onvif/device_service', body, {'Content-Type': 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation"'})
        r = conn.getresponse(); raw = r.read(65537)
        if r.status != 200 or len(raw) > 65536 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            return {'ok': False, 'message': 'Camera did not accept the ONVIF request. Check login and ONVIF settings.'}
        root = ET.fromstring(raw)
        if not any(e.tag.endswith('}GetDeviceInformationResponse') for e in root.iter()):
            return {'ok': False, 'message': 'Camera returned an ONVIF fault. Check login and camera time.'}
        fields = {e.tag.rsplit('}', 1)[-1]: (e.text or '')[:120] for e in root.iter() if e.tag.rsplit('}', 1)[-1] in ('Manufacturer', 'Model', 'FirmwareVersion')}
        return {'ok': True, 'message': 'ONVIF login accepted.', 'device': fields}
    except (OSError, ValueError, ET.ParseError, http.client.HTTPException):
        return {'ok': False, 'message': 'Could not reach the ONVIF service. Check address, port and network access.'}
    finally:
        if conn: conn.close()


class Supervisor:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.children, self.states, self.retry = {}, {}, {}
        self.logs = diagnostics.Journal(store.directory / "diagnostics.json")
        self.observed_events = {}
        self.logs.add("info", "Bridge manager started.")
        self.lock, self.wake, self.done = threading.RLock(), threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def desired(self):
        with self.store.lock: data = copy.deepcopy(self.store.data)
        specs = {}
        for c in data['cameras']:
            if not c['enabled']: continue
            try: ip = resolved(c['address'])
            except (OSError, ValueError):
                if self.states.get(c['id']) != 'Address unavailable': self.logs.add('error', 'Camera address cannot be resolved. Check its address and network.', c['id'])
                self.states[c['id']] = 'Address unavailable'
                continue
            self.states[c['id']] = 'Running'
            root = self.runtime / c['id']; root.mkdir(mode=0o700, exist_ok=True)
            common = dict(os.environ, CAMERA_IP=ip, CAMERA_ONVIF_PORT=str(c['onvif_port']), CAMERA_RTSP_PORT=str(c['rtsp_port']))
            # No secrets in process arguments or environment. Persist only in protected volume/tmpfs.
            runtime_camera = copy.deepcopy(c)
            if c['model'] == 'rtsp':
                runtime_camera['discovery']['views'][0]['streams'] = rtsp_camera.current_records(c, ip)
            atomic(root / 'camera.json', runtime_camera)
            common['EVENT_CAMERA_FILE'] = str(root/'camera.json')
            if c['model'] in presets.DISCOVERED_MODELS:
                slot = c['endpoints'][0]['slot']
                common.update(GENERIC_CAMERA_FILE=str(root/'camera.json'), GENERIC_MEDIA_PORT=str(19000+slot))
                fingerprint = hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()
                specs[c['id']+'/media'] = (common, 'generic_media.py', fingerprint)
            if c['model'] == 'cam-c44he':
                atomic(root / 'login.json', {'username': c['username'], 'password': c['password']})
                slot = c['endpoints'][0]['slot']; converter_port = 19000 + slot
                env = dict(common, CAMERA_LOGIN_FILE=str(root/'login.json'), TRANSCODER_RUN_DIR=str(root), TRANSCODER_RTSP_PORT=str(converter_port), TRANSCODER_API_PORT=str(20000+slot))
                specs[c['id']+'/converter'] = (env, 'lq_transcoder.py', hashlib.sha256(json.dumps([c['username'], c['password']]).encode()).hexdigest())
            for e in c['endpoints']:
                op, rp = self.store.onvif_base + e['slot'], self.store.rtsp_base + e['slot']
                env = dict(common, CAMERA_ADAPTER=c['model'], CAMERA_LENS=e['lens'], BRIDGE_NAME=c['name'] + (' · '+e['lens'].upper() if c['model']=='cam-c44he' else ''), BRIDGE_MAC=e['mac'], LISTEN_PORT=str(op), RTSP_LISTEN_PORT=str(rp), RTSP_PORT=str(rp), BRIDGE_URL=f"http://{data['host']}:{op}", ENABLE_PTZ=str(c['ptz'] and e['lens']=='ptz').lower(), ENABLE_EVENTS=str(c.get('events',True)).lower(), EVENT_STATUS_FILE=str(root/f'events-{e["slot"]}.json'))
                if c['model']=='cam-c44he': env.update(LQ_H265_RTSP_PORT=str(rp), LQ_RELAY_HOST='127.0.0.1', LQ_RELAY_PORT=str(converter_port), LQ_RELAY_SOURCE_ADDRESS='127.0.0.2')
                if c['model'] in presets.DISCOVERED_MODELS:
                    env['GENERIC_VIEW_INDEX'] = str(c['endpoints'].index(e))
                    specs[c['id']+'/'+e['lens']] = (env, 'generic_onvif.py', fingerprint)
                else:
                    specs[c['id']+'/'+e['lens']] = (env, 'bridge.py', hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest())
        return specs

    def stop_child(self, key):
        record = self.children.pop(key, None)
        if not record: return
        p = record[0]
        # The converter handles SIGTERM and reaps ffmpeg/MediaMTX before exit.
        p.terminate()
        try: p.wait(timeout=12)
        except subprocess.TimeoutExpired:
            try: os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            p.wait()

    def restart(self, ident):
        self.logs.add("info", "Camera restart requested.", ident)
        with self.lock:
            for key in list(self.children):
                if key.startswith(ident+'/'): self.stop_child(key)
        self.wake.set()

    def event_status(self):
        result={}
        with self.store.lock: cameras=copy.deepcopy(self.store.data['cameras'])
        for c in cameras:
            result[c['id']]=[]
            for e in c['endpoints']:
                try: state=json.loads((self.runtime/c['id']/f'events-{e["slot"]}.json').read_text())
                except (OSError,ValueError): state={'supported':False,'connection':'checking'}
                if not c.get('events',True): state={'supported':False,'connection':'disabled'}
                elif not c['enabled']: state={'supported':False,'connection':'camera disabled'}
                result[c['id']].append(state)
        return result

    def status(self):
        with self.lock:
            result = dict(self.states)
            for key, (p, _) in self.children.items():
                if p.poll() is not None: result[key.split('/')[0]] = 'Restarting a worker'
            return result

    def loop(self):
        while not self.done.is_set():
            try:
                with self.lock:
                    specs = self.desired()
                    for key, (p, old) in list(self.children.items()):
                        if key not in specs or old != specs[key] or p.poll() is not None:
                            if p.poll() is not None:
                                self.logs.add('error', f'Camera worker exited (code {p.returncode}); retrying in 5 seconds.', key)
                                self.retry[key] = time.monotonic()+5
                            else: self.logs.add('info', 'Camera worker stopping after a settings change.', key)
                            self.stop_child(key)
                    for key, spec in specs.items():
                        if key not in self.children and time.monotonic() >= self.retry.get(key, 0):
                            env, script, _ = spec
                            process = subprocess.Popen([sys.executable, str(APP/script)], env=env, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                            threading.Thread(target=self.logs.consume,args=(key,process.stdout),daemon=True).start()
                            self.logs.add("info", "Camera worker started.", key)
                            self.children[key] = (process, spec)
                    active_ids = {key.split('/')[0] for key in specs}
                    for d in self.runtime.iterdir():
                        if d.is_dir() and not d.name.startswith(('inspect-', 'snapshot-')) and d.name not in active_ids:
                            for f in d.iterdir(): f.unlink(missing_ok=True)
                            d.rmdir()
                    for ident, states in self.event_status().items():
                        for index, state in enumerate(states):
                            key=f'{ident}/events{index+1}'; connection=state.get('connection')
                            if self.observed_events.get(key)!=connection:
                                self.observed_events[key]=connection
                                messages={'connected':('info','Motion event feed connected.'),'unavailable':('warning','Motion event feed unavailable; retrying.'),'reconnecting':('warning','Motion event feed reconnecting.'),'unsupported':('warning','Camera does not expose supported motion events.'),'disabled':('info','Motion event forwarding is off.'),'camera disabled':('info','Camera is disabled.')}
                                if connection in messages:self.logs.add(*messages[connection],key)
            except Exception:
                self.logs.add('error', 'Manager could not reconcile camera workers; retrying.')
            self.logs.flush()
            self.wake.wait(5); self.wake.clear()
        with self.lock:
            for key in list(self.children): self.stop_child(key)
        self.logs.add("info", "Bridge manager stopped."); self.logs.flush()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, listen, store, supervisor):
        self.store, self.supervisor = store, supervisor
        self.sessions, self.attempts = {}, {}
        self.inspections = {}
        self.inspection_slot = threading.BoundedSemaphore(1)
        self.auth_lock = threading.RLock()
        self.auth_slots = threading.BoundedSemaphore(2)
        self.probe_slots = threading.BoundedSemaphore(4)
        super().__init__(listen, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'SONOFF-Bridge-Manager'
    sys_version = ''
    def log_message(self, *_): pass
    def setup(self):
        super().setup(); self.connection.settimeout(10)
    def send(self, status, body, mime='application/json', cookie=None):
        data = json.dumps(body).encode() if mime=='application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Connection', 'close')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers(); self.wfile.write(data); self.close_connection=True
    def session(self):
        try:
            jar=http.cookies.SimpleCookie(); jar.load(self.headers.get('Cookie',''))
            token=jar['session'].value
        except (KeyError, http.cookies.CookieError): return None
        with self.server.auth_lock:
            rec=self.server.sessions.get(token)
            if rec and rec['expires']>time.time(): return rec
        return None
    def detect_setup_host(self):
        # Reject the container's own interface address if someone opens that URL.
        excluded = (self.connection.getsockname()[0],) if Path('/.dockerenv').exists() else ()
        return suggested_host(self.headers.get('Host', ''), excluded)
    def do_GET(self):
        if self.path=='/healthz': return self.send(200, {'status':'running'})
        if self.path=='/api/session':
            rec=self.session()
            return self.send(200, {'setup_required':not bool(self.server.store.data['admin']), 'authenticated':bool(rec), 'csrf':rec['csrf'] if rec else None, 'suggested_host':self.detect_setup_host() if not self.server.store.data['admin'] else ''})
        if self.path.startswith('/api/inspection/'):
            rec = self.session()
            if not rec: return self.send(401, {'error':'Please sign in.'})
            with self.server.auth_lock:
                job = self.server.inspections.get(self.path.rsplit('/',1)[-1])
                if not job or job['owner'] != rec['csrf'] or job['expires'] < time.monotonic(): return self.send(404, {'error':'Inspection expired. Inspect again.'})
                result = {k:job[k] for k in ('status','message')}
                if job.get('plan'): result['report'] = discovery.public_report(job['plan'])
            return self.send(200, result)
        if urllib.parse.urlsplit(self.path).path=='/api/logs':
            if not self.session(): return self.send(401, {'error':'Please sign in.'})
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            camera=query.get('camera',[''])[0]; level=query.get('level',['all'])[0]
            try: limit=int(query.get('limit',['250'])[0])
            except ValueError: return self.send(400, {'error':'Invalid log filter.'})
            if level not in ('all','issues','info','warning','error') or not 1<=limit<=1000 or len(camera)>64: return self.send(400, {'error':'Invalid log filter.'})
            return self.send(200,self.server.supervisor.logs.read(camera,level,limit))
        if self.path=='/api/state':
            if not self.session(): return self.send(401, {'error':'Please sign in.'})
            data=self.server.store.public(); data['runtime']=self.server.supervisor.status(); data['events']=getattr(self.server.supervisor,'event_status',lambda:{})()
            return self.send(200,data)
        files={'/':('index.html','text/html; charset=utf-8'), '/app.js':('app.js','text/javascript; charset=utf-8'), '/style.css':('style.css','text/css; charset=utf-8')}
        if self.path in files:
            name,mime=files[self.path]; return self.send(200,(APP/'static'/name).read_bytes(),mime)
        self.send(404,{'error':'Not found.'})
    def do_POST(self):
        try:
            # All browser writes must originate from this exact host (no CORS).
            origin=urllib.parse.urlsplit(self.headers.get('Origin',''))
            if origin.scheme not in ('http','https') or origin.netloc!=self.headers.get('Host'):
                return self.send(403,{'error':'Reload this page before submitting.'})
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                return self.send(400,{'error':'Invalid request framing.'})
            n=int(self.headers.get('Content-Length','0'))
            if not 1<=n<=16384 or self.headers.get_content_type()!='application/json':
                return self.send(400,{'error':'Invalid request.'})
            payload=json.loads(self.rfile.read(n))
            if not isinstance(payload,dict): raise ValueError('Invalid request.')
            if self.path in ('/api/setup','/api/login'):
                return self.authenticate(payload)
            rec=self.session()
            if not rec: return self.send(401,{'error':'Please sign in.'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),rec['csrf']):
                return self.send(403,{'error':'Reload this page before submitting.'})
            if self.path=='/api/logout':
                with self.server.auth_lock:
                    self.server.sessions={k:v for k,v in self.server.sessions.items() if v is not rec}
                return self.send(200,{'ok':True},cookie='session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            if self.path == '/api/inspect':
                return self.inspect_camera(payload, rec)
            if self.path == '/api/add-inspected':
                with self.server.auth_lock:
                    job = self.server.inspections.get(payload.get('inspection'))
                    if not job or job['owner'] != rec['csrf'] or job['expires'] < time.monotonic() or job['status'] != 'ready':
                        raise ValueError('Inspect again.')
                    camera = copy.deepcopy(job['camera'])
                    camera.update(revision=payload.get('revision'), _discovery=job['plan'])
                    self.server.store.mutate('camera', camera)
                    del self.server.inspections[payload['inspection']]
                self.server.supervisor.wake.set()
                return self.send(200, {'ok':True})
            if self.path in ('/api/camera','/api/delete','/api/host'):
                payload.pop('_discovery', None)
                self.server.store.mutate(self.path.rsplit('/',1)[-1],payload)
                self.server.supervisor.wake.set()
                return self.send(200,{'ok':True})
            if self.path in ('/api/probe','/api/restart'):
                with self.server.store.lock:
                    camera=next((copy.deepcopy(c) for c in self.server.store.data['cameras'] if c['id']==payload.get('id')),None)
                if camera is None: raise ValueError('Camera not found.')
                if self.path=='/api/restart':
                    self.server.supervisor.restart(camera['id']); return self.send(200,{'ok':True})
                if not self.server.probe_slots.acquire(blocking=False): return self.send(429,{'error':'Camera tests are busy. Try again shortly.'})
                try:
                    result=probe(camera)
                    self.server.supervisor.logs.add('info' if result['ok'] else 'warning', 'Camera connection test passed.' if result['ok'] else 'Camera connection test failed. Check network access and ONVIF login.', camera['id'])
                    return self.send(200,result)
                finally: self.server.probe_slots.release()
            self.send(404,{'error':'Not found.'})
        except (ValueError, TypeError, KeyError):
            # Validation messages are fixed text generated here, never camera data.
            self.send(400,{'error':'Invalid settings. Check required fields, ports, capacity, and refresh if another window changed settings.'})
        except (OSError, http.client.HTTPException):
            self.send(503,{'error':'Unable to save or connect. Check storage and network access.'})
    def inspect_camera(self, payload, rec):
        # Validate the account before creating a bounded, session-owned job.
        if payload.get('id'): raise ValueError('For rediscovery, add a separate camera after removing its old bridge.')
        username = str(payload.get('username','')).strip()
        password = payload.get('password','')
        name = str(payload.get('name','')).strip()
        if not username or username == 'any' or len(username)>128 or not isinstance(password,str) or not 1<=len(password)<=256 or not 1<=len(name)<=80:
            raise ValueError('Enter the camera name and camera / Protect login.')
        model = payload.get('model', 'auto')
        if model not in presets.DISCOVERED_MODELS: raise ValueError('Choose an inspection-based setup method.')
        camera = dict(name=name, model=model, address=address(payload.get('address','')), onvif_port=camera_port(payload.get('onvif_port',80)), rtsp_port=camera_port(payload.get('rtsp_port',554)), username=username, password=password, enabled=payload.get('enabled',True), ptz=payload.get('ptz',True), events=payload.get('events',True))
        if not isinstance(camera['events'],bool) or not isinstance(camera['enabled'],bool) or not isinstance(camera['ptz'],bool): raise ValueError('Invalid camera switch.')
        camera['ip'] = resolved(camera['address'])
        if model == 'rtsp': rtsp_camera.inputs(payload, camera)
        if not self.server.inspection_slot.acquire(blocking=False): return self.send(429,{'error':'Another camera inspection is running. Try again shortly.'})
        ident = secrets.token_urlsafe(24)
        job = dict(owner=rec['csrf'], expires=time.monotonic()+900, status='running', message='Connecting to the camera…', camera=camera)
        with self.server.auth_lock:
            self.server.inspections = {k:v for k,v in self.server.inspections.items() if v['expires']>time.monotonic()}
            self.server.inspections[ident] = job
        def progress(message):
            with self.server.auth_lock: job['message'] = message
        def run():
            try:
                plan = presets.apply(camera['model'], (rtsp_camera.inspect if camera['model'] == 'rtsp' else discovery.inspect)(camera, progress))
                with self.server.auth_lock: job.update(status='ready', message='Inspection complete. Review the detected views before adding.', plan=plan)
            except ValueError as exc:
                # Only our fixed discovery diagnostics; parsing errors may contain camera data.
                allowed = ('RTSP setup:', 'CAM-PT2 preset:', 'Camera omits video source identities.', 'A video source has no verified', 'An audio codec is not yet supported', 'No usable ONVIF', 'No usable RTSP', 'Camera advertised a different host', 'Camera inspection timed out')
                message = str(exc) if str(exc).startswith(allowed) else 'Camera inspection failed. Check the ONVIF login, ports and network access.'
                with self.server.auth_lock: job.update(status='failed', message=message); job.pop('camera',None)
            except Exception:
                with self.server.auth_lock: job.update(status='failed', message='Could not inspect the camera. Check ONVIF/RTSP access and credentials.'); job.pop('camera',None)
            finally: self.server.inspection_slot.release()
        def expire():
            with self.server.auth_lock: self.server.inspections.pop(ident, None)
        timer = threading.Timer(900, expire); timer.daemon = True; timer.start()
        threading.Thread(target=run, daemon=True).start()
        return self.send(202, {'id':ident})

    def authenticate(self,payload):
        with self.server.auth_lock:
            now=time.monotonic(); key=self.client_address[0]
            self.server.attempts={k:v for k,v in self.server.attempts.items() if now-v[0]<60}
            start,count=self.server.attempts.get(key,(now,0))
            if count>=8: return self.send(429,{'error':'Too many attempts. Wait one minute.'})
            self.server.attempts[key]=(start,count+1)
        password=payload.get('password','')
        if not isinstance(password,str) or len(password)>256: raise ValueError('Invalid password.')
        if not self.server.auth_slots.acquire(blocking=False): return self.send(429,{'error':'Sign-in is busy. Try again shortly.'})
        try:
            if self.path=='/api/setup':
                self.server.store.setup(password,payload.get('host') or self.detect_setup_host())
            else:
                record=self.server.store.data['admin']
                if not record or not check_password(password,record): return self.send(401,{'error':'Incorrect admin password.'})
            token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24)
            with self.server.auth_lock:
                self.server.sessions={k:v for k,v in self.server.sessions.items() if v['expires']>time.time()}
                if len(self.server.sessions)>=64: self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[token]={'csrf':csrf,'expires':time.time()+43200}
                self.server.attempts.pop(self.client_address[0],None)
            secure='; Secure' if os.environ.get('SECURE_COOKIES')=='true' else ''
            self.send(200,{'ok':True,'csrf':csrf},cookie=f'session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}')
        finally: self.server.auth_slots.release()


def main():
    os.umask(0o077)
    store=Store(os.environ.get('DATA_DIR','/data'))
    supervisor=Supervisor(store,os.environ.get('RUNTIME_DIR','/run/bridge'))
    server=Server((os.environ.get('ADMIN_BIND','0.0.0.0'),store.admin_port),store,supervisor)
    supervisor.thread.start()
    def stop(*_): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    print('Unifi Camera Bridge ready. Open the web interface to set up or sign in.',flush=True)
    try: server.serve_forever()
    finally:
        supervisor.done.set(); supervisor.wake.set(); supervisor.thread.join(timeout=60); server.server_close()

if __name__=='__main__': main()
