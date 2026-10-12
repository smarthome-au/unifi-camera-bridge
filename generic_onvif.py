"""ONVIF facade driven by a measured discovery plan, with local WSSE auth."""
import base64
import copy
import signal
import generic_ptz
import events
import snapshots
from ptz_control import P
import datetime as dt
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dual_camera import S, TT, D, M1, M2, node, local, envelope, error, multicast
from discovery import WSSE, WSU, safe_xml, child, value
from rtsp_relay import RTSPRelay


def authenticate(root, camera):
    token = root.find(f'.//{{{WSSE}}}UsernameToken')
    if token is None or not hmac.compare_digest(value(token, 'Username').encode(), camera['username'].encode()): return False
    password = child(token, 'Password')
    if password is None: return False
    kind = password.get('Type', '').rsplit('#', 1)[-1]
    if kind == 'PasswordText': return hmac.compare_digest((password.text or '').encode(), camera['password'].encode())
    if kind != 'PasswordDigest': return False
    try:
        created = value(token, 'Created')
        stamp = dt.datetime.fromisoformat(created.replace('Z', '+00:00'))
        if abs((dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()) > 300: return False
        nonce = base64.b64decode(value(token, 'Nonce'), validate=True)
        if not 8 <= len(nonce) <= 128: return False
        expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + camera['password'].encode()).digest()).decode()
        return hmac.compare_digest(password.text or '', expected)
    except (ValueError, TypeError): return False


def encoder(parent, ns, tag, stream, index):
    video = stream['video']
    e = node(parent, ns, tag, token=f'encoder{index}', Profile='Main' if stream['convert_video'] else video['profile'])
    node(e, TT, 'Name', stream['quality'].upper()); node(e, TT, 'UseCount', 1)
    node(e, TT, 'Encoding', stream['output_codec'])
    r = node(e, TT, 'Resolution'); node(r, TT, 'Width', video['width']); node(r, TT, 'Height', video['height'])
    rate = node(e, TT, 'RateControl'); node(rate, TT, 'FrameRateLimit', video['fps'])
    # Bitrate is measured when available; otherwise a conservative bridge ceiling.
    node(rate, TT, 'BitrateLimit', video['bitrate'] or max(512, video['width'] * video['height'] // 256))
    node(e, TT, 'Quality', 100)


def audio_encoder(parent, ns, tag, stream, index):
    audio = stream['audio']
    e = node(parent, ns, tag, token=f'audio{index}')
    node(e, TT, 'Name', 'Microphone'); node(e, TT, 'UseCount', 1)
    node(e, TT, 'Encoding', audio['codec']); node(e, TT, 'Bitrate', audio['bitrate'] or (64 if audio['codec'] in ('PCMA', 'PCMU') else 128))
    node(e, TT, 'SampleRate', audio['rate']//1000)


def source(parent, ns, tag, view):
    e = node(parent, ns, tag, token='source_config')
    node(e, TT, 'Name', view['label']); node(e, TT, 'UseCount', len(view['streams'])); node(e, TT, 'SourceToken', 'source')
    v = view['streams'][0]['video']
    node(e, TT, 'Bounds', x='0', y='0', width=str(v['width']), height=str(v['height']))


def audio_source(parent, ns, tag, view):
    e = node(parent, ns, tag, token='audio_source_config')
    node(e, TT, 'Name', 'Microphone'); node(e, TT, 'UseCount', len(view['streams'])); node(e, TT, 'SourceToken', 'microphone')


def media_caps(parent, tag, view):
    e = node(parent, M2, tag, SnapshotUri='true')
    kinds = ('PTZ ' if view.get('ptz') else '') + 'VideoSource VideoEncoder' + (' AudioSource AudioEncoder' if any(p['audio'] for p in view['streams']) else '')
    node(e, M2, 'ProfileCapabilities', MaximumNumberOfProfiles=str(len(view['streams'])), ConfigurationsSupported=kinds)
    node(e, M2, 'StreamingCapabilities', RTSPStreaming='true', RTPMulticast='false', RTP_RTSP_TCP='true')


def response(request, camera, index, public, rtsp_port, mac, controller=None, event_service=None):
    body = request.find(f'{{{S}}}Body')
    if body is None or len(body) != 1: return error('InvalidArgVal', 'Invalid request')
    op = body[0]; name = local(op.tag); ns = op.tag[1:].split('}')[0]
    view = copy.copy(camera['discovery']['views'][index]); streams = view['streams']
    if not camera.get('ptz'): view.pop('ptz',None)
    root, r = envelope(ns, name)
    host = urllib.parse.urlsplit(public).hostname
    if ns == M2:
        indices = list(range(len(streams)))
        token = value(op, 'ProfileToken') or (value(op, 'Token') if name == 'GetProfiles' else '')
        if token:
            if token not in [f'profile{i}' for i in indices]: return error()
            indices = [int(token[7:])]
        ct = value(op, 'ConfigurationToken')
        if ct:
            if 'VideoEncoder' in name or 'AudioEncoder' in name:
                prefix = 'encoder' if 'VideoEncoder' in name else 'audio'
                if ct not in [f'{prefix}{i}' for i in indices]: return error()
                indices = [int(ct[len(prefix):])]
            elif ct != ('audio_source_config' if 'AudioSource' in name else 'source_config'): return error()
        if name == 'GetServiceCapabilities': media_caps(r, 'Capabilities', view)
        elif name == 'GetProfiles':
            types = {e.text for e in op if local(e.tag) == 'Type'}
            for i in indices:
                p = node(r, M2, 'Profiles', token=f'profile{i}')
                node(p, M2, 'Name', streams[i]['quality'].upper()); c = node(p, M2, 'Configurations')
                for kind, fn in [('VideoSource', source), ('AudioSource', audio_source), ('VideoEncoder', encoder), ('AudioEncoder', audio_encoder)]:
                    if kind.startswith('Audio') and not streams[i]['audio']: continue
                    if types and 'All' not in types and kind not in types: continue
                    fn(c, M2, kind, *( (view,) if kind.endswith('Source') else (streams[i], i)))
                if view.get('ptz') and (not types or 'All' in types or 'PTZ' in types):
                    c.append(generic_ptz.profile_config(view['ptz'],M2,'PTZ',len(streams)))
        elif name == 'GetSnapshotUri':
            return snapshots.uri_response(op, public, [f'profile{i}' for i in range(len(streams))])
        elif name == 'GetStreamUri':
            if not token or value(op, 'Protocol') not in ('RTSP', 'RtspUnicast'): return error('InvalidArgVal', 'RTSP profile required')
            node(r, M2, 'Uri', f'rtsp://{host}:{rtsp_port}/view{index}_{streams[indices[0]]["quality"]}')
        elif name in ('GetVideoEncoderConfigurations', 'GetAudioEncoderConfigurations', 'GetVideoSourceConfigurations', 'GetAudioSourceConfigurations'):
            kind = name[3:-14]
            if kind == 'VideoEncoder':
                for i in indices: encoder(r, M2, 'Configurations', streams[i], i)
            elif kind == 'AudioEncoder':
                for i in indices:
                    if streams[i]['audio']: audio_encoder(r, M2, 'Configurations', streams[i], i)
            elif kind == 'VideoSource': source(r, M2, 'Configurations', view)
            elif any(p['audio'] for p in streams): audio_source(r, M2, 'Configurations', view)
        elif name == 'GetVideoEncoderConfigurationOptions':
            for i in indices:
                stream = streams[i]; v = stream['video']
                o = node(r, M2, 'Options', FrameRatesSupported=str(v['fps']), ProfilesSupported='Main' if stream['convert_video'] else v['profile'])
                node(o, TT, 'Encoding', stream['output_codec']); q = node(o, TT, 'QualityRange'); node(q, TT, 'Min', 100); node(q, TT, 'Max', 100)
                res = node(o, TT, 'ResolutionsAvailable'); node(res, TT, 'Width', v['width']); node(res, TT, 'Height', v['height'])
                b = node(o, TT, 'BitrateRange'); node(b, TT, 'Min', 1); node(b, TT, 'Max', v['bitrate'] or max(512, v['width']*v['height']//256))
        elif name == 'GetAudioEncoderConfigurationOptions':
            for i in indices:
                a = streams[i]['audio']
                if not a: continue
                o = node(r, M2, 'Options'); node(o, TT, 'Encoding', a['codec'])
                for key, number in [('BitrateList', a['bitrate'] or 64), ('SampleRateList', a['rate']//1000)]: node(node(o, TT, key), TT, 'Items', number)
        else: return error('ActionNotSupported', 'Read-only media service')
    elif ns in (events.E, events.W):
        return error('ActionNotSupported', 'Use the events service endpoint')
    elif ns == P:
        if not view.get('ptz') or controller is None: return error('ActionNotSupported','Pan/tilt disabled')
        return controller.response(op,len(streams))
    elif ns == D:
        if name == 'GetDeviceInformation':
            info = camera['discovery']['device']
            for key, val in [('Manufacturer', info['Manufacturer']), ('Model', info['Model']+' Bridge'), ('FirmwareVersion', '1.4.0'), ('SerialNumber', mac.replace(':', '')), ('HardwareId', 'Sonoff-Protect-Bridge')]: node(r, D, key, val)
        elif name == 'GetServices':
            for service, path in [(D, '/onvif/device_service'), (M2, '/onvif/media2_service')] + ([(P,'/onvif/ptz_service')] if view.get('ptz') else []):
                s = node(r, D, 'Service'); node(s, D, 'Namespace', service); node(s, D, 'XAddr', public+path)
                if value(op, 'IncludeCapability') in ('true', '1'):
                    cap = node(s, D, 'Capabilities')
                    if service == M2: media_caps(cap, 'Capabilities', view)
                    elif service == P: generic_ptz.capabilities(cap)
                    else: device_caps(cap)
                v = node(s, D, 'Version'); node(v, TT, 'Major', 20 if service == M2 else 2); node(v, TT, 'Minor', 6)
        elif name == 'GetServiceCapabilities': device_caps(r)
        elif name == 'GetCapabilities':
            cap = node(r, D, 'Capabilities'); dev = node(cap, TT, 'Device'); node(dev, TT, 'XAddr', public+'/onvif/device_service')
            # Media2 is advertised through GetServices, as required for H.265.
            sec = node(dev, TT, 'Security')
            for key in ('TLS1.1', 'TLS1.2', 'OnboardKeyGeneration', 'AccessPolicyConfig', 'X.509Token', 'SAMLToken', 'KerberosToken', 'RELToken'): node(sec, TT, key, 'false')
            if view.get('ptz'): node(node(cap,TT,'PTZ'),TT,'XAddr',public+'/onvif/ptz_service')
        elif name == 'GetSystemDateAndTime':
            now = dt.datetime.now(dt.timezone.utc); date = node(r, D, 'SystemDateAndTime')
            node(date, TT, 'DateTimeType', 'Manual'); node(date, TT, 'DaylightSavings', 'false')
            utc = node(date, TT, 'UTCDateTime'); t = node(utc, TT, 'Time'); d = node(utc, TT, 'Date')
            for key, val in [('Hour', now.hour), ('Minute', now.minute), ('Second', now.second)]: node(t, TT, key, val)
            for key, val in [('Year', now.year), ('Month', now.month), ('Day', now.day)]: node(d, TT, key, val)
        elif name == 'GetScopes':
            for scope in (['type/ptz'] if view.get('ptz') else []) + ['type/video_encoder', 'name/'+urllib.parse.quote(camera['name'], safe=''), 'hardware/Automatic-Bridge']:
                s = node(r, D, 'Scopes'); node(s, TT, 'ScopeDef', 'Fixed'); node(s, TT, 'ScopeItem', 'onvif://www.onvif.org/'+scope)
        elif name == 'GetHostname':
            h = node(r, D, 'HostnameInformation'); node(h, TT, 'FromDHCP', 'false'); node(h, TT, 'Name', 'sonoff-protect-bridge')
        elif name == 'GetNetworkInterfaces':
            e = node(r, D, 'NetworkInterfaces', token='eth0'); node(e, TT, 'Enabled', 'true')
            info = node(e, TT, 'Info'); node(info, TT, 'Name', 'eth0'); node(info, TT, 'HwAddress', mac); node(info, TT, 'MTU', 1500)
        elif name == 'GetNetworkProtocols':
            for protocol, port in [('HTTP', urllib.parse.urlsplit(public).port), ('RTSP', rtsp_port)]:
                e = node(r, D, 'NetworkProtocols'); node(e, TT, 'Name', protocol); node(e, TT, 'Enabled', 'true'); node(e, TT, 'Port', port)
        elif name == 'GetDNS':
            node(node(r, D, 'DNSInformation'), TT, 'FromDHCP', 'false')
        elif name == 'GetNTP': node(node(r, D, 'NTPInformation'), TT, 'FromDHCP', 'false')
        else: return error('ActionNotSupported', 'Read-only device service')
    else: return error('ActionNotSupported', 'Use the Media2 service')
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if ns == D: data = events.advertise(data, name, event_service)
    return 200, data


def device_caps(parent):
    c = node(parent, D, 'Capabilities')
    node(c, D, 'Network', IPFilter='false', ZeroConfiguration='false', IPVersion6='false', DynDNS='false')
    node(c, D, 'Security', UsernameToken='true', HttpDigest='false')
    node(c, D, 'System', DiscoveryResolve='false', DiscoveryBye='false', RemoteDiscovery='false', SystemBackup='false', SystemLogging='false', FirmwareUpgrade='false')


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def setup(self): super().setup(); self.connection.settimeout(10)
    def do_GET(self):
        if self.path == snapshots.PATH:
            self.server.snapshots.serve(self)
        else:
            self.send_error(404)

    def do_POST(self):
        op = None
        try:
            if self.path not in ('/onvif/device_service', '/onvif/media2_service', '/onvif/ptz_service', events.PATH) and not self.path.startswith(events.PREFIX): raise ValueError()
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1: raise ValueError()
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1048576: raise ValueError()
            root = safe_xml(self.rfile.read(size))
            op = root.find(f'{{{S}}}Body')
            public_op = op is not None and len(op) == 1 and op[0].tag == f'{{{D}}}GetSystemDateAndTime'
            if not public_op and not authenticate(root, self.server.camera):
                status, data = error('NotAuthorized', 'Authentication required'); status = 401
            else:
                if op[0].tag.startswith(('{'+events.E+'}', '{'+events.W+'}')):
                    if not self.server.events: status, data = error('ActionNotSupported', 'Events disabled')
                    else: status, data = self.server.events.response(op[0], self.path)
                else: status, data = response(root, self.server.camera, self.server.index, os.environ['BRIDGE_URL'], int(os.environ['RTSP_PORT']), os.environ['BRIDGE_MAC'], self.server.controller, self.server.events)
                if op[0].tag.startswith('{'+P+'}'): print(json.dumps({'ptz_operation':local(op[0].tag),'status':status}),flush=True)
        except Exception:
            status, data = error('InvalidArgVal', 'Invalid request')
        if status >= 400 and op is not None and len(op): print(json.dumps({'operation':local(op[0].tag),'status':status}),flush=True)
        self.send_response(status); self.send_header('Content-Type', 'application/soap+xml; charset=utf-8'); self.send_header('Content-Length', str(len(data))); self.send_header('Connection', 'close'); self.end_headers()
        self.wfile.write(data); self.close_connection = True


def main():
    camera = json.loads(Path(os.environ['GENERIC_CAMERA_FILE']).read_text())
    port = int(os.environ['GENERIC_MEDIA_PORT'])
    relay = RTSPRelay(('0.0.0.0', int(os.environ['RTSP_PORT'])), '127.0.0.1', port, lq_target=('127.0.0.1', port), lq_source_address='127.0.0.2')
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    server = http.server.ThreadingHTTPServer(('0.0.0.0', int(os.environ['LISTEN_PORT'])), Handler)
    server.camera = camera; server.index = int(os.environ['GENERIC_VIEW_INDEX'])
    server.snapshots = snapshots.Service(camera, f'rtsp://127.0.0.1:{port}/view{server.index}_hq', local=True)
    plan=camera['discovery']['views'][server.index].get('ptz')
    server.controller=generic_ptz.Controller(camera,plan) if camera.get('ptz') and plan else None
    view=camera['discovery']['views'][server.index]
    server.events=events.Service(camera,os.environ['BRIDGE_URL'],view,len(camera['discovery']['views'])>1,status_file=os.environ.get('EVENT_STATUS_FILE')) if camera.get('events',True) else None
    def stop(*_): threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:server.serve_forever()
    finally:
        if server.events:server.events.close()
        if server.controller:server.controller.close()
        server.server_close();relay.shutdown();relay.server_close()

if __name__ == '__main__': main()
