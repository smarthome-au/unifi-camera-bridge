"""CAM-S3W ONVIF compatibility bridge. Python standard library only.

Forwards camera authentication without saving credentials. RTSP is relayed byte-for-byte.
Designed and verified against CAM-S3W firmware 1.2.1; manual Protect adoption.
"""
import datetime
import hashlib
from adapters import fix_response
from rtsp_relay import RTSPRelay
import dual_camera
import events
import snapshots
from pathlib import Path
import ptz_control
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import signal
import threading
import urllib.parse
import xml.etree.ElementTree as ET

S = 'http://www.w3.org/2003/05/soap-envelope'
NS = {'s': S, 'tds': 'http://www.onvif.org/ver10/device/wsdl',
      'trt': 'http://www.onvif.org/ver10/media/wsdl',
      'tt': 'http://www.onvif.org/ver10/schema',
      'tev': 'http://www.onvif.org/ver10/events/wsdl',
      'tmd': 'http://www.onvif.org/ver10/deviceIO/wsdl'}
READ_OPERATIONS = {
    'GetDeviceInformation', 'GetServices', 'GetCapabilities',
    'GetServiceCapabilities', 'GetSystemDateAndTime', 'GetScopes',
    'GetNetworkInterfaces', 'GetHostname', 'GetDNS', 'GetNTP',
    'GetNetworkProtocols', 'GetNetworkDefaultGateway',
    'GetProfiles', 'GetProfile', 'GetVideoSources', 'GetVideoSourceConfigurations',
    'GetVideoSourceConfiguration', 'GetVideoEncoderConfigurations',
    'GetVideoEncoderConfiguration', 'GetVideoEncoderConfigurationOptions',
    'GetAudioSources', 'GetAudioOutputs', 'GetAudioSourceConfigurations',
    'GetAudioSourceConfiguration', 'GetAudioEncoderConfigurations',
    'GetAudioEncoderConfiguration', 'GetAudioEncoderConfigurationOptions',
    'GetStreamUri', 'GetSnapshotUri', 'GetEventProperties',
    'GetCompatibleVideoEncoderConfigurations', 'GetCompatibleAudioEncoderConfigurations',
    'GetCompatibleVideoSourceConfigurations', 'GetCompatibleAudioSourceConfigurations',
}
EVENT_OPERATIONS = {'CreatePullPointSubscription', 'PullMessages', 'Renew',
                    'Unsubscribe', 'SetSynchronizationPoint', 'Subscribe'}


def localname(tag):
    return tag.rsplit('}', 1)[-1]


class Settings:
    def __init__(self):
        self.camera = os.environ['CAMERA_IP']
        ipaddress.ip_address(self.camera)
        self.camera_port = int(os.environ.get('CAMERA_ONVIF_PORT', '80'))
        self.listen = os.environ.get('LISTEN_ADDRESS', '0.0.0.0')
        self.port = int(os.environ.get('LISTEN_PORT', '8096'))
        self.public = os.environ.get('BRIDGE_URL', '').rstrip('/')
        u = urllib.parse.urlsplit(self.public)
        if (u.scheme != 'http' or not u.hostname or u.username or u.password
                or u.path or u.query or u.fragment):
            raise ValueError('Set BRIDGE_URL to http://BRIDGE_HOST:PORT')
        self.public_port = u.port or 80
        self.public_host = u.hostname
        self.adapter = os.environ.get('CAMERA_ADAPTER', 'cam-s3w')
        if self.adapter not in {'cam-s3w', 'cam-c44he', 'passthrough'}:
            raise ValueError('Unsupported CAMERA_ADAPTER')
        self.lens = os.environ.get('CAMERA_LENS', 'fixed')
        if self.lens not in {'fixed', 'ptz'}:
            raise ValueError('CAMERA_LENS must be fixed or ptz')
        identity = self.camera + ('/' + self.lens if self.adapter == 'cam-c44he' else '')
        default_mac = '02:' + ':'.join(f'{x:02x}' for x in hashlib.sha256(identity.encode()).digest()[:5])
        self.mac = os.environ.get('BRIDGE_MAC', default_mac).lower()
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', self.mac):
            raise ValueError('Invalid BRIDGE_MAC')
        self.name = os.environ.get('BRIDGE_NAME', 'SONOFF CAM-S3W Bridge')
        # Optional client allowlist, e.g. console /32 plus local tests.
        self.allowed = [ipaddress.ip_network(x.strip()) for x in
                        os.environ.get('ALLOWED_CLIENTS', '').split(',') if x.strip()]
        self.events = os.environ.get('ENABLE_EVENTS', 'true').lower() == 'true'
        self.ptz = os.environ.get('ENABLE_PTZ', 'false').lower() == 'true' and self.adapter == 'cam-c44he' and self.lens == 'ptz'
        self.rtsp_port = int(os.environ.get('RTSP_PORT', '554'))
        # Optional Media2-only H.265 LQ restream. Zero preserves native streams.
        self.lq_h265_port = int(os.environ.get('LQ_H265_RTSP_PORT', '0'))
        if not 0 <= self.lq_h265_port <= 65535:
            raise ValueError('Invalid LQ_H265_RTSP_PORT')
        self.rtsp_listen_port = int(os.environ.get('RTSP_LISTEN_PORT', '8554'))
        self.camera_rtsp_port = int(os.environ.get('CAMERA_RTSP_PORT', '554'))
        self.lq_target = ((os.environ['LQ_RELAY_HOST'], int(os.environ.get('LQ_RELAY_PORT', '8554')))
                          if os.environ.get('LQ_RELAY_HOST') and self.lq_h265_port else None)
        self.lq_source_address = os.environ.get('LQ_RELAY_SOURCE_ADDRESS')
        if self.lq_source_address and self.lq_source_address != '127.0.0.2':
            raise ValueError('Invalid internal relay source address')
        self.camera_origins = {f'http://{self.camera}',
                               f'http://{self.camera}:{self.camera_port}'}
        self.advertised_netmask = int(os.environ.get('BRIDGE_PREFIX_LENGTH', '24'))


def fault(reason='Action not supported by bridge', code='ter:ActionNotSupported'):
    # Fixed strings only; do not reflect credentials or arbitrary input in faults.
    xml = f'<s:Envelope xmlns:s="{S}" xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>{code}</s:Value></s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>'
    return xml.encode()


def replace_camera_http_urls(text, config):
    # Rewrite HTTP service addresses here; the model adapter handles RTSP URIs.
    pattern = r'http://' + re.escape(config.camera) + r'(?::' + str(config.camera_port) + r')?(?=[/\s<"\']|$)'
    return re.sub(pattern, config.public, text)


def operation_of(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD not accepted')
    root = ET.fromstring(data)
    body = next((e for e in root if localname(e.tag) == 'Body'), None)
    if body is None or len(body) != 1:
        raise ValueError('Expected one SOAP operation')
    return localname(body[0].tag)




class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, config):
        self.config = config
        self.rtsp_relay = None
        self.ptz_watchdog = ptz_control.Watchdog(config.camera, config.camera_port)
        self.slots = threading.BoundedSemaphore(8)
        self.events = None
        self.event_camera = None
        self.snapshots = None
        event_file = os.environ.get('EVENT_CAMERA_FILE')
        if event_file:
            self.event_camera = json.loads(Path(event_file).read_text())
            if 'cameras' in self.event_camera:
                self.event_camera = next(c for c in self.event_camera['cameras'] if c['address']==config.camera)
            self.event_camera['ip'] = config.camera
            if config.adapter in ('cam-s3w', 'cam-c44he'):
                stream = '/av_stream/ch0' if config.adapter == 'cam-s3w' else ('/av_stream1/main' if config.lens == 'fixed' else '/av_stream0/main')
                self.snapshots = snapshots.Service(self.event_camera, f'rtsp://{config.camera}:{config.camera_rtsp_port}{stream}')
            self.events = events.Service(self.event_camera,config.public,multi=config.adapter=='cam-c44he',source_token=config.lens+'_VideoSource' if config.adapter=='cam-c44he' else 'VideoSource_0',status_file=os.environ.get('EVENT_STATUS_FILE')) if config.events else None
        super().__init__(address, Handler)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'CamS3WBridge/1.0'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass  # Never log URL queries, headers, request bodies or credentials.

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send_body(self, status, data, mime='application/soap+xml; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def permitted(self):
        allowed = self.server.config.allowed
        return not allowed or any(ipaddress.ip_address(self.client_address[0]) in n for n in allowed)

    def do_GET(self):
        if not self.permitted():
            self.send_body(403, b'Forbidden', 'text/plain'); return
        if self.path == snapshots.PATH and self.server.snapshots:
            self.server.snapshots.serve(self)
        elif self.path == '/healthz':
            result = {'status': 'running'}
            if self.server.rtsp_relay:
                result['rtsp'] = self.server.rtsp_relay.stats()
            self.send_body(200, json.dumps(result).encode(), 'application/json')
        else:
            self.send_body(404, b'Use the ONVIF device service at /onvif/device_service', 'text/plain')

    def read_body(self):
        transfer = self.headers.get('Transfer-Encoding', '').lower()
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) > 1 or (transfer and lengths):
            raise ValueError('Ambiguous request framing')
        if transfer:
            if transfer != 'chunked':
                raise ValueError('Unsupported transfer encoding')
            data = bytearray()
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b'\r\n'):
                    raise ValueError('Bad chunk framing')
                size = int(line.split(b';', 1)[0].strip(), 16)
                if size < 0 or size + len(data) > 1024 * 1024:
                    raise ValueError('Body too large')
                if size == 0:
                    # Consume a bounded set of trailers without forwarding them.
                    for _ in range(32):
                        trailer = self.rfile.readline(8192)
                        if trailer == b'\r\n':
                            return bytes(data)
                        if not trailer or not trailer.endswith(b'\r\n'):
                            raise ValueError('Bad trailer')
                    raise ValueError('Too many trailers')
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b'\r\n':
                    raise ValueError('Incomplete chunk')
                data.extend(chunk)
        size = int(lengths[0]) if lengths else 0
        if not 0 < size <= 1024 * 1024:
            raise ValueError('Invalid body length')
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError('Incomplete body')
        return data

    def do_POST(self):
        config = self.server.config
        if not self.permitted():
            self.send_body(403, fault('Client not allowed')); return
        url = urllib.parse.urlsplit(self.path)
        if url.scheme or url.netloc or not url.path.startswith('/onvif/'):
            self.send_body(404, fault()); return
        if not self.server.slots.acquire(blocking=False):
            self.send_body(503, fault('Bridge busy')); return
        operation = 'unparsed'
        status = 502
        connection = None
        diagnostics = {}
        ptz_locked = False
        try:
            data = self.read_body()
            operation = operation_of(data)
            original_data = data
            request = ET.fromstring(data)
            request_op = request.find('{'+S+'}Body')[0]
            if operation == 'GetSnapshotUri' and self.server.snapshots:
                from generic_onvif import authenticate
                if not authenticate(request, self.server.event_camera):
                    status=401; self.send_body(status, fault('Authentication required','ter:NotAuthorized')); return
                status, content = snapshots.uri_response(request_op, config.public, ['Profile_0', 'Profile_1'])
                self.send_body(status, content); return
            is_event = request_op.tag.startswith(('{'+events.E+'}', '{'+events.W+'}'))
            if is_event and self.server.event_camera is not None:
                from generic_onvif import authenticate
                if not authenticate(request,self.server.event_camera):
                    status=401;self.send_body(status,fault('Authentication required','ter:NotAuthorized'));return
                if not self.server.events:status,content=dual_camera.error('ActionNotSupported','Events disabled')
                else:status,content=self.server.events.response(request_op,self.path)
                self.send_body(status,content);return
            if is_event and not config.events:
                status=400;self.send_body(status,fault('Events disabled'));return
            is_ptz = ptz_control.is_ptz(data)
            if is_ptz and not ptz_control.enabled(config):
                status = 400
                self.send_body(status, fault('PTZ is not enabled for this lens')); return
            if operation == 'GetStreamUri':
                tokens = [e.text for e in ET.fromstring(data).iter() if localname(e.tag) == 'ProfileToken']
                diagnostics['profile'] = tokens[0] if tokens and tokens[0] in {'Profile_0', 'Profile_1'} else 'unknown'
            if operation == 'GetProfiles':
                request = ET.fromstring(data).find('{' + S + '}Body')[0]
                diagnostics['media_version'] = 2 if request.tag.startswith('{' + dual_camera.M2 + '}') else 1
                known_types = {'All', 'VideoSource', 'VideoEncoder', 'AudioSource', 'AudioEncoder', 'PTZ', 'Metadata', 'Analytics', 'AudioOutput', 'AudioDecoder'}
                diagnostics['configuration_types'] = [e.text if e.text in known_types else 'other'
                                                       for e in request if localname(e.tag) == 'Type']
            allowed = READ_OPERATIONS | (EVENT_OPERATIONS if config.events else set())
            if is_ptz:
                allowed = ptz_control.ALLOWED
            if operation not in allowed:
                status = 400
                self.send_body(status, fault()); return
            # Change addressing only; keep WSSE UsernameToken bytes intact.
            data = data.replace(config.public.encode(), f'http://{config.camera}:{config.camera_port}'.encode())
            upstream_path = self.path
            if is_ptz:
                data = ptz_control.prepare(data)
                upstream_path = '/onvif/ptz_service'
                if operation in {'ContinuousMove', 'Stop'}:
                    self.server.ptz_watchdog.lock.acquire()
                    ptz_locked = True
            synthetic = config.adapter == 'cam-c44he' and dual_camera.is_media_request(original_data)
            if synthetic:
                # Camera authenticates every synthetic media response. Preserve
                # WSSE header bytes; replace only the operation and addressing.
                data = dual_camera.authentication_request(data)
                upstream_path = '/onvif/media_service'
            headers = {'Content-Type': self.headers.get('Content-Type', 'application/soap+xml'),
                       'Content-Length': str(len(data)), 'Connection': 'close',
                       'Accept-Encoding': 'identity', 'User-Agent': 'CamS3WBridge/1.0'}
            for name in ('SOAPAction', 'Authorization'):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            if synthetic:
                headers['Content-Type'] = 'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver10/media/wsdl/GetProfiles"'
                headers['SOAPAction'] = '"http://www.onvif.org/ver10/media/wsdl/GetProfiles"'
            timeout = 65 if operation == 'PullMessages' else 10
            connection = http.client.HTTPConnection(config.camera, config.camera_port, timeout=timeout)
            connection.request('POST', upstream_path, body=data, headers=headers)
            response = connection.getresponse()
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('Response too large')
            status = response.status
            if status == 200:
                if is_ptz:
                    content = ptz_control.response(content)
                    root = ET.fromstring(content)
                    if root.find('.//{' + ptz_control.P + '}' + operation + 'Response') is not None:
                        if operation == 'ContinuousMove':
                            self.server.ptz_watchdog.arm(data, headers)
                        elif operation == 'Stop':
                            if self.server.ptz_watchdog.send_stop(data, headers):
                                self.server.ptz_watchdog.cancel()
                            else:
                                status, content = 502, fault('Camera did not acknowledge zero-speed stop')
                elif synthetic:
                    status, content = dual_camera.media_response(content, original_data, config)
                elif config.adapter == 'cam-c44he':
                    content = dual_camera.device_response(content, operation, config)
                elif config.adapter == 'cam-s3w' and operation in {'GetSnapshotUri', 'GetNTP', 'GetNetworkDefaultGateway'} and len(ET.fromstring(content).find('.//{' + S + '}Body')[0]) == 0:
                    status, content = 400, fault()
                else:
                    content = fix_response(content, operation, config)
            if status == 200 and self.server.event_camera is not None:
                content=events.advertise(content,operation,self.server.events)
            if status == 200 and self.server.snapshots and operation in ('GetServices', 'GetCapabilities', 'GetServiceCapabilities'):
                content = snapshots.advertise(content)
            if operation == 'GetStreamUri' and status == 200:
                uris = [e.text for e in ET.fromstring(content).iter() if localname(e.tag) == 'Uri']
                if uris:
                    uri = urllib.parse.urlsplit(uris[0])
                    diagnostics['stream_path'] = uri.path if uri.path in {'/av_stream/ch0', '/av_stream/ch1', '/av_stream0/main', '/av_stream0/sec', '/av_stream1/main', '/av_stream1/sec'} else 'other'
                    diagnostics['stream_destination'] = 'bridge' if uri.hostname == config.public_host else ('camera' if uri.hostname == config.camera else 'other')
                    diagnostics['stream_port'] = uri.port or 554
            self.send_body(status, content)
        except (ValueError, ET.ParseError):
            status = 400
            self.send_body(status, fault('Invalid or unsupported SOAP data'))
        except (OSError, http.client.HTTPException):
            status = 502
            try:
                self.send_body(status, fault('Camera connection failed'))
            except OSError:
                pass
        finally:
            if ptz_locked:
                self.server.ptz_watchdog.lock.release()
            if connection:
                connection.close()
            self.server.slots.release()
            # Log method names only, never request bodies or arbitrary text.
            safe_operation = operation if re.fullmatch(r'(?:Get|Set|Create|Delete|Add|Remove|Start|Stop|Renew|Unsubscribe|Pull|Subscribe|Continuous|Relative|Absolute|Goto)[A-Za-z0-9]{0,72}', operation) else 'unsupported'
            print(json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              'operation': safe_operation, 'status': status, **diagnostics}), flush=True)


if __name__ == '__main__':
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    settings = Settings()
    server = Server((settings.listen, settings.port), settings)
    relay = RTSPRelay((settings.listen, settings.rtsp_listen_port), settings.camera,
                      settings.camera_rtsp_port, settings.allowed, settings.lq_target, settings.lq_source_address)
    server.rtsp_relay = relay
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    print(json.dumps({'bridge': settings.public, 'camera': settings.camera,
                      'rtsp': f'{settings.public_host}:{settings.rtsp_port} via TCP relay',
                      'credentials': 'forwarded; not stored'}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.events:server.events.close()
        server.ptz_watchdog.stop_now()
        server.server_close()
        relay.shutdown()
        relay.server_close()
