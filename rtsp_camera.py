"""Explicit RTSP inputs, measured locally and wrapped by the generic ONVIF facade."""
import copy
import urllib.parse as url
import discovery


def stream_uri(text, camera):
    # URLs may contain access tokens in the path/query; never echo these values.
    if not isinstance(text, str) or not text or len(text) > 4096 or any(ord(c) <= 32 for c in text):
        raise ValueError('RTSP setup: enter a stream path or RTSP URL without spaces.')
    try:
        if text.startswith('/') and not text.startswith('//'):
            text = f'rtsp://{camera["ip"]}:{camera["rtsp_port"]}' + text
        p = url.urlsplit(text)
        if p.scheme != 'rtsp' or not p.hostname or p.fragment:
            raise ValueError()
        if p.username is not None or p.password is not None:
            raise ValueError('credentials')
        if p.hostname.lower() not in (camera['address'].lower(), camera['ip']):
            raise ValueError()
        port = p.port or 554
        if not 1 <= port <= 65535:
            raise ValueError()
        return url.urlunsplit(('rtsp', f'{camera["ip"]}:{port}', p.path or '/', p.query, ''))
    except ValueError as exc:
        if str(exc) == 'credentials':
            raise ValueError('RTSP setup: remove the login from the URL and use the username and password fields.') from None
        raise ValueError('RTSP setup: use an rtsp:// URL for the camera address above, or a path beginning with /.') from None


def inputs(payload, camera):
    anonymous = payload.get('rtsp_anonymous', False)
    if not isinstance(anonymous, bool):
        raise ValueError('Invalid RTSP authentication switch.')
    camera['rtsp_anonymous'] = anonymous
    camera['ptz'] = camera['events'] = False
    records = []
    for quality in ('hq', 'lq'):
        text = payload.get(quality + '_path', '')
        if quality == 'lq' and not text:
            continue
        records.append(dict(source='rtsp-view', token=quality, name=quality.upper(),
                            uri=stream_uri(text, camera), ptz=False))
    if len(records) == 2 and records[0]['uri'] == records[1]['uri']:
        raise ValueError('RTSP setup: HQ and LQ must use different stream addresses. Leave LQ blank for one quality.')
    camera['_rtsp_records'] = records


def inspect(camera, progress=lambda _: None, measure_fn=None):
    records = copy.deepcopy(camera['_rtsp_records'])
    (measure_fn or discovery.measure)(camera, records, progress)
    if any(not r.get('video') for r in records):
        raise ValueError('RTSP setup: a supplied stream could not be verified as H.264/H.265 video. Check the login, path and camera settings.')
    if len(records) == 2:
        area = lambda r: r['video']['width'] * r['video']['height']
        if area(records[1]) >= area(records[0]):
            raise ValueError('RTSP setup: LQ must have a lower resolution than HQ. Check the paths or leave LQ blank.')
    views, warnings = discovery.select_views(records)
    warnings.append('RTSP supplies video and audio only. Camera motion events and pan/tilt controls are unavailable through this connection.')
    return dict(device=dict(Manufacturer='RTSP', Model='RTSP camera', FirmwareVersion=''),
                views=views, warnings=warnings, profile_count=len(records), events=None)


def current_records(camera, ip):
    """Refresh a hostname's pinned IP without changing saved paths or credentials."""
    records = copy.deepcopy(camera['discovery']['views'][0]['streams'])
    for r in records:
        p = url.urlsplit(r['uri'])
        r['uri'] = url.urlunsplit((p.scheme, f'{ip}:{p.port or 554}', p.path, p.query, ''))
    return records


def probe(camera, ip):
    try:
        records = current_records(camera, ip)
        for r in records:
            r.pop('video', None)
            r.pop('audio', None)
        plan = inspect(dict(camera, _rtsp_records=records))
        saved = camera['discovery']['views'][0]['streams']
        measured = plan['views'][0]['streams']
        properties = lambda s: (s['video']['codec'], s['video']['width'], s['video']['height'], tuple((s.get('audio') or {}).get(k) for k in ('codec', 'rate', 'channels')))
        if [properties(s) for s in saved] != [properties(s) for s in measured]:
            return dict(ok=False, message='RTSP streams are reachable, but their video or audio settings have changed. Add the camera again to inspect the new settings.')
        return dict(ok=True, message='All configured RTSP streams verified.')
    except Exception:
        return dict(ok=False, message='Could not verify the RTSP streams. Check the camera login, paths and network access.')
