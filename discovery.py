"""Read-only ONVIF inventory and measured, source-specific stream selection.

All returned URLs remain private. Neither SOAP nor ffprobe diagnostics are logged.
"""
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.parse as url
import urllib.request as http
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from dual_camera import S, TT, D, M1, M2, local
from ptz_control import P
import generic_ptz

WSSE = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd'
WSU = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd'


def child(e, name):
    return next((x for x in e if local(x.tag) == name), None) if e is not None else None


def value(e, name, default=''):
    x = child(e, name)
    return x.text or default if x is not None else default


def safe_xml(raw):
    if len(raw) > 1048576 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Invalid ONVIF response.')
    return ET.fromstring(raw)


class NoRedirect(http.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('Camera redirected its service to another address.')


class Client:
    def __init__(self, camera):
        self.c = camera
        self.ip = camera['ip']
        self.offset = 0
        self.timeout = 6
        self.device = f"http://{self.ip}:{camera['onvif_port']}/onvif/device_service"
        passwords = http.HTTPPasswordMgrWithDefaultRealm()
        # Entries are registered only after the destination has been pinned.
        self.passwords = passwords
        self.opener = http.build_opener(http.ProxyHandler({}), NoRedirect(), http.HTTPDigestAuthHandler(passwords), http.HTTPBasicAuthHandler(passwords))
        self.deadline = time.monotonic() + 120

    def pin(self, uri, scheme):
        p = url.urlsplit(uri)
        if p.scheme not in scheme or not p.hostname or len(uri) > 4096:
            raise ValueError('Camera returned an unsupported stream or service address.')
        if p.hostname not in (self.ip, self.c['address'], '0.0.0.0', '127.0.0.1', 'localhost'):
            raise ValueError('Camera advertised a different host; inspect its network configuration.')
        if p.fragment:
            raise ValueError('Unsupported address fragment.')
        return url.urlunsplit((p.scheme, f'{self.ip}:{p.port or (554 if p.scheme == "rtsp" else 80)}', p.path or '/', p.query, ''))

    def call(self, uri, ns, operation, inner='', auth=True, header_xml=''):
        if time.monotonic() > self.deadline:
            raise ValueError('Camera inspection timed out.')
        uri = self.pin(uri, ('http',))
        header = ''
        if auth:
            created = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.offset)).strftime('%Y-%m-%dT%H:%M:%SZ')
            nonce = secrets.token_bytes(20)
            digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.c['password'].encode()).digest()).decode()
            header = f'<w:Security xmlns:w="{WSSE}" xmlns:u="{WSU}"><w:UsernameToken><w:Username>{escape(self.c["username"])}</w:Username><w:Password Type="{WSSE.replace("wss-wssecurity-secext-1.0.xsd", "wss-username-token-profile-1.0#PasswordDigest")}">{digest}</w:Password><w:Nonce>{base64.b64encode(nonce).decode()}</w:Nonce><u:Created>{created}</u:Created></w:UsernameToken></w:Security>'
            self.passwords.add_password(None, uri, self.c['username'], self.c['password'])
        body = f'<s:Envelope xmlns:s="{S}" xmlns:t="{ns}" xmlns:tt="{TT}"><s:Header>{header}{header_xml}</s:Header><s:Body><t:{operation}>{inner}</t:{operation}></s:Body></s:Envelope>'.encode()
        request = http.Request(uri, data=body, headers={'Content-Type': f'application/soap+xml; charset=utf-8; action="{ns}/{operation}"'})
        with self.opener.open(request, timeout=self.timeout) as r:
            root = safe_xml(r.read(1048577))
        if any(local(e.tag) == 'Fault' for e in root.iter()):
            raise ValueError('Camera rejected an ONVIF request.')
        response = root.find(f'.//{{{ns}}}{operation}Response')
        if response is None:
            raise ValueError('Camera did not return the expected ONVIF response.')
        return response

    def clock(self):
        try:
            r = self.call(self.device, D, 'GetSystemDateAndTime', auth=False)
            utc = next(x for x in r.iter() if local(x.tag) == 'UTCDateTime')
            fields = {local(x.tag): int(x.text) for x in utc.iter() if local(x.tag) in ('Year', 'Month', 'Day', 'Hour', 'Minute', 'Second')}
            stamp = dt.datetime(*(fields[k] for k in ('Year', 'Month', 'Day', 'Hour', 'Minute', 'Second')), tzinfo=dt.timezone.utc)
            self.offset = (stamp - dt.datetime.now(dt.timezone.utc)).total_seconds()
        except Exception:
            pass


def profile_records(response, ns):
    records = []
    for p in response:
        if local(p.tag) not in ('Profiles', 'Profile') or not p.get('token'):
            continue
        configs = child(p, 'Configurations') if ns == M2 else p
        source = child(configs, 'VideoSource' if ns == M2 else 'VideoSourceConfiguration')
        encoder = child(configs, 'VideoEncoder' if ns == M2 else 'VideoEncoderConfiguration')
        if encoder is None:
            continue
        records.append({'token': p.get('token'), 'name': value(p, 'Name'),
                        'source': value(source, 'SourceToken'), 'source_config': source.get('token', '') if source is not None else '',
                        'reported_codec': value(encoder, 'Encoding'), 'reported_width': int(value(child(encoder, 'Resolution'), 'Width', '0')), 'reported_height': int(value(child(encoder, 'Resolution'), 'Height', '0')), 'ns': ns,
                        'ptz': child(configs, 'PTZ' if ns == M2 else 'PTZConfiguration') is not None,
                        'ptz_config': (child(configs, 'PTZ' if ns == M2 else 'PTZConfiguration').get('token','') if child(configs, 'PTZ' if ns == M2 else 'PTZConfiguration') is not None else '')})
    return records


def credential_uri(uri, camera):
    p = url.urlsplit(uri)
    if camera.get('rtsp_anonymous'):
        return uri
    auth = url.quote(camera['username'], safe='') + ':' + url.quote(camera['password'], safe='')
    return url.urlunsplit((p.scheme, auth + '@' + p.netloc, p.path, p.query, ''))


def media_config(camera, paths, port):
    return {'logLevel': 'error', 'rtspAddress': f'127.0.0.1:{port}', 'rtspTransports': ['tcp'],
            'rtmp': False, 'hls': False, 'webrtc': False, 'srt': False, 'moq': False,
            'authInternalUsers': [{'user': 'any', 'ips': ['127.0.0.1'], 'permissions': [{'action': 'read'}]}],
            'paths': {key: {'source': credential_uri(uri, camera), 'rtspTransport': 'tcp', 'sourceOnDemand': True} for key, uri in paths.items()}}


def measure(camera, records, progress=lambda _: None):
    # A private RTSP proxy keeps camera credentials out of ffprobe's arguments.
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='inspect-', dir=os.environ.get('RUNTIME_DIR', '/run/bridge')) as folder:
        path = Path(folder)/'media.json'
        path.write_text(json.dumps(media_config(camera, {f'p{i}': p['uri'] for i, p in enumerate(records)}, port)))
        path.chmod(0o600)
        process = subprocess.Popen(['/mediamtx', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(.4)
            for i, record in enumerate(records):
                progress(f'Checking video stream {i+1} of {len(records)}…')
                try:
                    result = subprocess.run(['ffprobe', '-v', 'quiet', '-rtsp_transport', 'tcp', '-analyzeduration', '2000000', '-probesize', '2000000', '-show_streams', '-of', 'json', f'rtsp://127.0.0.1:{port}/p{i}'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=16)
                    streams = json.loads(result.stdout)['streams']
                    video = next(x for x in streams if x.get('codec_type') == 'video')
                    codec = {'hevc': 'H265', 'h264': 'H264'}.get(video.get('codec_name'))
                    if not codec or not video.get('width') or not video.get('height'):
                        continue
                    rate = video.get('avg_frame_rate', '0/1')
                    n, d = map(float, rate.split('/'))
                    if not d or not n:
                        n, d = map(float, video.get('r_frame_rate', '0/1').split('/'))
                    fps = round(n/d, 3) if d else 0
                    if not 0 < fps <= 240:
                        continue
                    audio = next((x for x in streams if x.get('codec_type') == 'audio'), None)
                    record['video'] = {'codec': codec, 'width': video['width'], 'height': video['height'], 'fps': fps, 'profile': video.get('profile', 'Main'), 'bitrate': int(video.get('bit_rate', 0))//1000}
                    record['audio'] = ({'codec': {'pcm_alaw': 'PCMA', 'pcm_mulaw': 'PCMU', 'aac': 'AAC'}.get(audio.get('codec_name'), 'unsupported'), 'rate': int(audio.get('sample_rate', 0)), 'channels': audio.get('channels', 1), 'bitrate': int(audio.get('bit_rate', 0))//1000} if audio else None)
                except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired):
                    continue
        finally:
            process.terminate()
            try: process.wait(timeout=4)
            except subprocess.TimeoutExpired: process.kill(); process.wait()


def select_views(records):
    groups = {}
    warnings = []
    if any(not r['source'] for r in records):
        # Do not guess lens relationships from profile names or URL spelling.
        raise ValueError('Camera omits video source identities. Automatic lens grouping is unsafe; use a verified model preset.')
    for r in records:
        groups.setdefault(r['source'], []).append(r)
    views = []
    for source, profiles in groups.items():
        good = [p for p in profiles if p.get('video')]
        if not good:
            raise ValueError('A video source has no verified H.264/H.265 stream. Check RTSP access and camera settings.')
        area = lambda p: p['video']['width'] * p['video']['height']
        good.sort(key=lambda p: (area(p), p['video']['fps']), reverse=True)
        hq = good[0]
        lower = [p for p in good if area(p) < area(hq)]
        same = [p for p in lower if p['video']['codec'] == hq['video']['codec']]
        lq = min(same or lower, key=area) if lower else None
        streams = []
        for quality, p in [('hq', hq)] + ([('lq', lq)] if lq else []):
            p = json.loads(json.dumps(p))
            p['quality'] = quality
            p['convert_video'] = p['video']['codec'] != hq['video']['codec']
            p['output_codec'] = hq['video']['codec']
            if p.get('audio') and p['audio']['codec'] == 'unsupported':
                raise ValueError('An audio codec is not yet supported by automatic setup. Disable camera audio or use a verified preset.')
            streams.append(p)
        label = 'Camera view' if len(groups) == 1 else f'View {len(views)+1}'
        views.append({'source': source, 'label': label, 'streams': streams})
    if any(not p.get('video') for p in records): warnings.append('Some advertised profiles could not be verified and were excluded.')
    
    return views, warnings


def inspect(camera, progress=lambda _: None, measure_fn=measure):
    client = Client(camera)
    client.clock()
    progress('Reading camera identity and ONVIF services…')
    info = client.call(client.device, D, 'GetDeviceInformation')
    device = {k: value(info, k)[:120] for k in ('Manufacturer', 'Model', 'FirmwareVersion')}
    services = {}
    try:
        response = client.call(client.device, D, 'GetServices', '<t:IncludeCapability>false</t:IncludeCapability>')
        for service in response:
            ns, uri = value(service, 'Namespace'), value(service, 'XAddr')
            if ns in (M1, M2, P) and uri: services[ns] = client.pin(uri, ('http',))
    except Exception:
        pass
    if M1 not in services:
        try:
            caps = client.call(client.device, D, 'GetCapabilities', '<t:Category>All</t:Category>')
            media = next(x for x in caps.iter() if local(x.tag) == 'Media')
            services[M1] = client.pin(value(media, 'XAddr'), ('http',))
        except Exception:
            pass
    if not services: raise ValueError('No usable ONVIF media service was advertised.')
    records, seen, advertised = [], set(), []
    for ns in (M2, M1):
        if ns not in services: continue
        progress('Reading video sources and media profiles…')
        try:
            profiles = profile_records(client.call(services[ns], ns, 'GetProfiles', '<t:Type>All</t:Type>' if ns == M2 else ''), ns)
        except Exception:
            continue
        # Some devices provide only a source-config token inside each profile.
        configs = {}
        try:
            for config in client.call(services[ns], ns, 'GetVideoSourceConfigurations'):
                configs[config.get('token')] = value(config, 'SourceToken')
        except Exception:
            pass
        for p in profiles[:16]:
            p['source'] = p['source'] or configs.get(p['source_config'], '')
            advertised.append(p)
            token = escape(p['token'])
            inner = f'<t:Protocol>RTSP</t:Protocol><t:ProfileToken>{token}</t:ProfileToken>' if ns == M2 else f'<t:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></t:StreamSetup><t:ProfileToken>{token}</t:ProfileToken>'
            try:
                response = client.call(services[ns], ns, 'GetStreamUri', inner)
                uri = next(x.text for x in response.iter() if local(x.tag) == 'Uri')
                p['uri'] = client.pin(uri, ('rtsp',))
                if p['uri'] not in seen:
                    seen.add(p['uri']); records.append(p)
            except Exception:
                continue
    family_fallback = False
    if device['Manufacturer'].upper() == 'SONOFF':
        main = next((p for p in records if url.urlsplit(p['uri']).path == '/av_stream/ch0'), None)
        if main:
            # Some SONOFF firmware returns an invalid hostname and repeats ch0
            # for its lower-resolution profile. Probe the sibling path only
            # when ONVIF explicitly assigns that profile to the same source.
            lower = next((p for p in advertised if p['source'] and p['source'] == main['source'] and 0 < p['reported_width']*p['reported_height'] < main['reported_width']*main['reported_height']), None)
            if lower and not any(url.urlsplit(p['uri']).path == '/av_stream/ch1' for p in records):
                candidate = dict(lower)
                u = url.urlsplit(main['uri'])
                candidate['uri'] = url.urlunsplit((u.scheme, u.netloc, '/av_stream/ch1', '', ''))
                candidate['verify_reported_size'] = True
                records.append(candidate)
                family_fallback = True
    fallback = device['Manufacturer'].upper() == 'SONOFF' and device['Model'].upper() == 'CAM-C44HE'
    if fallback:
        # This model omits a lens and mislabels encoders in its ONVIF inventory.
        # Paths are a verified model quirk; properties are still measured live.
        records = [dict(token='', name='', source=lens, source_config='', reported_codec='', ns=M1, ptz=lens=='ptz', uri=f'rtsp://{camera["ip"]}:{camera["rtsp_port"]}/av_stream{number}/{quality}') for lens, number in [('fixed',1),('ptz',0)] for quality in ('main','sec')]
    if not records: raise ValueError('No usable RTSP streams were returned. Check the ONVIF account and camera settings.')
    measure_fn(camera, records, progress)
    for p in records:
        if p.get('verify_reported_size') and p.get('video') and (p['video']['width'],p['video']['height']) != (p['reported_width'],p['reported_height']):
            p.pop('video',None)
    views, warnings = select_views(records)
    if family_fallback:
        warnings.insert(0, 'Camera returned an invalid or duplicate SD address. A SONOFF sibling stream path was checked against the advertised source and resolution.')
    if len(advertised) > len(seen) and not fallback and not family_fallback:
        warnings.append('Some advertised profiles returned duplicate or unusable stream addresses.')
    if fallback:
        warnings.insert(0, 'CAM-C44HE reports incomplete ONVIF profiles. Known lens paths were used and their video properties measured live.')
        for view in views: view['label'] = 'Fixed view' if view['source']=='fixed' else 'PTZ view'
    attach_ptz(client, services.get(P), advertised, views, warnings, fallback)
    import events
    try: event_plan = events.discover(client)
    except Exception: event_plan = None
    return {'device': device, 'views': views, 'warnings': warnings, 'profile_count': len(records), 'events': event_plan}


def public_report(plan):
    return {'device': plan['device'], 'events': bool(plan.get('events')), 'profile_count': plan['profile_count'], 'warnings': plan['warnings'],
            'views': [{'label': v['label'], 'ptz': bool(v.get('ptz')), 'streams': [{'quality': p['quality'], 'video': p['video'], 'audio': p['audio'], 'output_codec': p['output_codec'], 'convert_video': p['convert_video']} for p in v['streams']]} for v in plan['views']]}


def attach_ptz(client, service, profiles, views, warnings, dual_fallback=False):
    if not service:
        if any(p.get('ptz') for p in profiles): warnings.append('PTZ was detected, but no usable PTZ service was advertised.')
        return
    for view in views:
        if dual_fallback:
            profile=next((p for p in profiles if p.get('ptz_config') and p['token']=='Profile_0'),None) if view['source']=='ptz' else None
        else:
            candidates=[p for p in profiles if p['source']==view['source'] and p.get('ptz_config')]
            profile=next((p for p in candidates if p['token']==view['streams'][0]['token']), candidates[0] if candidates else None)
        if not profile:continue
        try:view['ptz']=generic_ptz.discover(client,service,profile['token'],profile['ptz_config'])
        except Exception:warnings.append('Pan/tilt configuration could not be verified for '+view['label']+'.')
