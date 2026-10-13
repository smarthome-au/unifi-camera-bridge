"""Read-only CAM-C44HE lens adapter, measured firmware 1.0.4.

Media2 exposes H.265 HQ and H.264 LQ. Media1 offers H.264 LQ only, because
its VideoEncoding enumeration does not include H.265. Authentication is
forwarded to the physical camera; synthetic responses require its success.
Optional directional PTZ control is exposed only on the PTZ lens; lens-specific events are not advertised.
"""
import copy
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
import ptz_control

S = 'http://www.w3.org/2003/05/soap-envelope'
TT = 'http://www.onvif.org/ver10/schema'
D = 'http://www.onvif.org/ver10/device/wsdl'
M1 = 'http://www.onvif.org/ver10/media/wsdl'
M2 = 'http://www.onvif.org/ver20/media/wsdl'
for prefix, uri in [('s', S), ('tt', TT), ('tds', D), ('trt', M1), ('tr2', M2)]:
    ET.register_namespace(prefix, uri)


def node(parent, ns, tag, text=None, **attrs):
    e = ET.SubElement(parent, '{' + ns + '}' + tag, attrs)
    if text is not None:
        e.text = str(text)
    return e


def local(tag):
    return tag.rsplit('}', 1)[-1]


def request_operation(data):
    return ET.fromstring(data).find('{' + S + '}Body')[0]


def is_media_request(data):
    return request_operation(data).tag.startswith(('{' + M1 + '}', '{' + M2 + '}'))


def authentication_request(data):
    text = data.decode('utf-8')
    pattern = r'(<(?P<p>[\w.-]+:)?Body\b[^>]*>).*?(</(?P=p)Body>)'
    # ONVIF envelopes use a prefix. Also accept a default-namespace Body.
    replacement = f'<trt:GetProfiles xmlns:trt="{M1}"/>'
    text, n = re.subn(pattern, lambda m: m[1] + replacement + m[3], text, flags=re.S)
    if not n:
        text, n = re.subn(r'(<Body\b[^>]*>).*?(</Body>)', lambda m: m[1] + replacement + m[2], text, flags=re.S)
    if n != 1:
        raise ValueError('Unsupported SOAP envelope')
    text = re.sub(r'(<(?:[\w.-]+:)?Action\b[^>]*>)[^<]*(</(?:[\w.-]+:)?Action>)',
                  lambda m: m[1] + M1 + '/GetProfiles' + m[2], text)
    return text.encode()


def envelope(ns, operation):
    root = ET.Element('{' + S + '}Envelope')
    body = node(root, S, 'Body')
    return root, node(body, ns, operation + 'Response')


def error(code='NoProfile', reason='Unknown profile or configuration'):
    return 400, (f'<s:Envelope xmlns:s="{S}" xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault>'
                 f'<s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:{code}</s:Value></s:Subcode></s:Code>'
                 f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>').encode()


def video_source(parent, ns, tag, config):
    e = node(parent, ns, tag, token=config.lens + '_VideoSourceConfig')
    node(e, TT, 'Name', config.lens + ' source')
    node(e, TT, 'UseCount', 2)
    node(e, TT, 'SourceToken', config.lens + '_VideoSource')
    node(e, TT, 'Bounds', x='0', y='0', width='2560', height='1440')


def audio_source(parent, ns, tag):
    e = node(parent, ns, tag, token='AudioSourceConfig')
    node(e, TT, 'Name', 'Camera microphone')
    node(e, TT, 'UseCount', 2)
    node(e, TT, 'SourceToken', 'AudioSource')


def multicast(parent):
    e = node(parent, TT, 'Multicast')
    addr = node(e, TT, 'Address')
    node(addr, TT, 'Type', 'IPv4')
    node(addr, TT, 'IPv4Address', '0.0.0.0')
    for tag, value in [('Port', 0), ('TTL', 0), ('AutoStart', 'false')]:
        node(e, TT, tag, value)


def video_encoder(parent, ns, tag, index, v2, h265_lq=False):
    e = node(parent, ns, tag, token=f'VideoEncoder_{index}')
    if v2:
        e.set('Profile', 'Main')
    node(e, TT, 'Name', 'HQ' if index == 0 else 'LQ')
    node(e, TT, 'UseCount', 1)
    node(e, TT, 'Encoding', 'H265' if index == 0 or (v2 and h265_lq) else 'H264')
    r = node(e, TT, 'Resolution')
    node(r, TT, 'Width', 2560 if index == 0 else 640)
    node(r, TT, 'Height', 1440 if index == 0 else 360)
    if not v2:
        node(e, TT, 'Quality', 100)
    rate = node(e, TT, 'RateControl')
    node(rate, TT, 'FrameRateLimit', 15)
    if not v2:
        node(rate, TT, 'EncodingInterval', 1)
    # Camera's advertised upper bound, not a measured stream bitrate.
    node(rate, TT, 'BitrateLimit', 5000)
    if v2:
        node(e, TT, 'Quality', 100)
    else:
        h = node(e, TT, 'H264')
        node(h, TT, 'GovLength', 40)
        node(h, TT, 'H264Profile', 'Main')
        multicast(e)
        node(e, TT, 'SessionTimeout', 'PT60S')


def audio_encoder(parent, ns, tag, v2):
    e = node(parent, ns, tag, token='AudioEncoder')
    node(e, TT, 'Name', 'Camera microphone')
    node(e, TT, 'UseCount', 2)
    node(e, TT, 'Encoding', 'PCMA' if v2 else 'G711')
    node(e, TT, 'Bitrate', 64)
    node(e, TT, 'SampleRate', 8)
    if not v2:
        multicast(e)
        node(e, TT, 'SessionTimeout', 'PT60S')


def media_capabilities(parent, ns, ptz=False):
    e = node(parent, ns, 'Capabilities', SnapshotUri='false')
    if ns == M2:
        node(e, ns, 'ProfileCapabilities', MaximumNumberOfProfiles='2',
             ConfigurationsSupported='VideoSource VideoEncoder AudioSource AudioEncoder' + (' PTZ' if ptz else ''))
        node(e, ns, 'StreamingCapabilities', RTSPStreaming='true', RTPMulticast='false', RTP_RTSP_TCP='true')
    else:
        node(e, ns, 'ProfileCapabilities', MaximumNumberOfProfiles='1')
        node(e, ns, 'StreamingCapabilities', RTPMulticast='false', RTP_TCP='true', RTP_RTSP_TCP='true')


def media_response(auth_response, request, config):
    auth = ET.fromstring(auth_response)
    if any(local(e.tag) == 'Fault' for e in auth.iter()):
        return 400, auth_response
    if auth.find('.//{' + M1 + '}GetProfilesResponse') is None:
        return error('NotAuthorized', 'Camera did not validate the media request')
    op = request_operation(request)
    name = local(op.tag)
    ns = M2 if op.tag.startswith('{' + M2 + '}') else M1
    v2 = ns == M2
    h265_lq = v2 and bool(getattr(config, 'lq_h265_port', 0))
    if not v2 and getattr(config, 'lq_target', None):
        # Both published qualities now use H.265, which Media1 cannot describe.
        if name == 'GetProfiles':
            root, _ = envelope(ns, name)
            return 200, ET.tostring(root, encoding='utf-8', xml_declaration=True)
        return error('ActionNotSupported', 'Use the Media2 service for H.265 profiles')
    values = {local(e.tag): e.text for e in op}
    indices = [0, 1] if v2 else [1]
    token = values.get('ProfileToken') or (values.get('Token') if name == 'GetProfiles' else None)
    if token:
        if token not in [f'Profile_{i}' for i in indices]:
            return error()
        indices = [int(token[-1])]
    ct = values.get('ConfigurationToken')
    if ct:
        if 'VideoEncoder' in name:
            if ct not in [f'VideoEncoder_{i}' for i in indices]:
                return error()
            indices = [int(ct[-1])]
        elif 'AudioEncoder' in name and ct != 'AudioEncoder':
            return error()
        elif 'VideoSource' in name and ct != config.lens + '_VideoSourceConfig':
            return error()
        elif 'AudioSource' in name and ct != 'AudioSourceConfig':
            return error()
    root, response = envelope(ns, name)
    if name == 'GetServiceCapabilities':
        media_capabilities(response, ns, ptz_control.enabled(config))
    elif name in {'GetProfiles', 'GetProfile'}:
        if name == 'GetProfile' and not token:
            return error()
        types = {e.text for e in op if local(e.tag) == 'Type'}
        for i in indices:
            p = node(response, ns, 'Profile' if name == 'GetProfile' else 'Profiles', token=f'Profile_{i}', fixed='true')
            node(p, ns if v2 else TT, 'Name', config.lens.title() + (' HQ' if i == 0 else ' LQ'))
            c = node(p, ns, 'Configurations') if v2 else p
            for kind, fn in [('VideoSource', video_source), ('AudioSource', audio_source), ('VideoEncoder', video_encoder), ('AudioEncoder', audio_encoder)]:
                if v2 and types and 'All' not in types and kind not in types:
                    continue
                args = (config,) if kind == 'VideoSource' else ((i, v2, h265_lq) if kind == 'VideoEncoder' else ((v2,) if kind == 'AudioEncoder' else ()))
                fn(c, ns if v2 else TT, kind if v2 else kind + 'Configuration', *args)
            if ptz_control.enabled(config) and (not v2 or not types or 'All' in types or 'PTZ' in types):
                ptz = ptz_control.profile_configuration(auth, ns if v2 else TT, 'PTZ' if v2 else 'PTZConfiguration')
                if ptz is not None:
                    c.append(ptz)
    elif name == 'GetStreamUri':
        if not token or (v2 and values.get('Protocol') not in {'RTSP', 'RtspUnicast'}):
            return error('InvalidArgVal', 'Profile token and RTSP transport required')
        lens_index = 1 if config.lens == 'fixed' else 0
        path = f'/av_stream{lens_index}/' + ('main' if indices[0] == 0 else 'sec')
        port = config.lq_h265_port if h265_lq and indices[0] == 1 else config.rtsp_port
        uri = f'rtsp://{config.public_host}:{port}{path}'
        if v2:
            node(response, ns, 'Uri', uri)
        else:
            m = node(response, ns, 'MediaUri')
            for tag, value in [('Uri', uri), ('InvalidAfterConnect', 'false'), ('InvalidAfterReboot', 'false'), ('Timeout', 'PT0S')]:
                node(m, TT, tag, value)
    elif name == 'GetVideoSources':
        e = node(response, ns, 'VideoSources', token=config.lens + '_VideoSource')
        node(e, TT, 'Framerate', 15)
        r = node(e, TT, 'Resolution')
        node(r, TT, 'Width', 2560)
        node(r, TT, 'Height', 1440)
    elif name == 'GetAudioSources':
        e = node(response, ns, 'AudioSources', token='AudioSource')
        node(e, TT, 'Channels', 1)
    elif name == 'GetVideoEncoderConfigurationOptions':
        if v2:
            for i in indices:
                e = node(response, ns, 'Options', FrameRatesSupported='15', ProfilesSupported='Main')
                node(e, TT, 'Encoding', 'H265' if i == 0 or h265_lq else 'H264')
                q = node(e, TT, 'QualityRange')
                node(q, TT, 'Min', 100); node(q, TT, 'Max', 100)
                r = node(e, TT, 'ResolutionsAvailable')
                node(r, TT, 'Width', 2560 if i == 0 else 640); node(r, TT, 'Height', 1440 if i == 0 else 360)
                b = node(e, TT, 'BitrateRange')
                node(b, TT, 'Min', 5000); node(b, TT, 'Max', 5000)
        else:
            e = node(response, ns, 'Options')
            q = node(e, TT, 'QualityRange'); node(q, TT, 'Min', 100); node(q, TT, 'Max', 100)
            h = node(e, TT, 'H264'); r = node(h, TT, 'ResolutionsAvailable')
            node(r, TT, 'Width', 640); node(r, TT, 'Height', 360)
            for tag, val in [('GovLengthRange', 40), ('FrameRateRange', 15), ('EncodingIntervalRange', 1)]:
                r = node(h, TT, tag); node(r, TT, 'Min', val); node(r, TT, 'Max', val)
            node(h, TT, 'H264ProfilesSupported', 'Main')
    elif name == 'GetAudioEncoderConfigurationOptions':
        e = node(response, ns, 'Options')
        if not v2:
            e = node(e, TT, 'Options')
        node(e, TT, 'Encoding', 'PCMA' if v2 else 'G711')
        for tag, value in [('BitrateList', 64), ('SampleRateList', 8)]:
            node(node(e, TT, tag), TT, 'Items', value)
    elif any(name in {f'Get{k}Configuration', f'Get{k}Configurations', f'GetCompatible{k}Configurations'}
             for k in ['VideoSource', 'VideoEncoder', 'AudioSource', 'AudioEncoder']):
        kind = next(k for k in ['VideoSource', 'VideoEncoder', 'AudioSource', 'AudioEncoder'] if k in name)
        tag = 'Configuration' if name.endswith('Configuration') else 'Configurations'
        if kind == 'VideoEncoder':
            for i in indices: video_encoder(response, ns, tag, i, v2, h265_lq)
        elif kind == 'AudioEncoder': audio_encoder(response, ns, tag, v2)
        elif kind == 'VideoSource': video_source(response, ns, tag, config)
        else: audio_source(response, ns, tag)
    else:
        return error('ActionNotSupported', 'Operation not supported by lens bridge')
    return 200, ET.tostring(root, encoding='utf-8', xml_declaration=True)


def device_response(data, operation, config):
    # Reuse the existing device-address and empty-response fixes, then remove
    # camera-wide capabilities that cannot truthfully be assigned to a lens.
    from adapters import cam_s3w_response
    shared = copy.copy(config)
    shared.adapter = 'cam-s3w'
    root = ET.fromstring(cam_s3w_response(data, operation, shared))
    if any(local(e.tag) == 'Fault' for e in root.iter()):
        return data
    if operation == 'GetServices':
        response = root.find('.//{' + D + '}GetServicesResponse')
        for service in list(response):
            namespace = service.findtext('{' + D + '}Namespace')
            keep = ({D} if getattr(config, 'lq_target', None) else {D, M1}) | ({ptz_control.P} if ptz_control.enabled(config) else set())
            if namespace not in keep:
                response.remove(service)
            elif namespace == M1:
                cap = service.find('{' + D + '}Capabilities')
                if cap is not None:
                    service.remove(cap)
                    cap = ET.Element('{' + D + '}Capabilities')
                    service.insert(2, cap)
                    media_capabilities(cap, M1)
        service = node(response, D, 'Service')
        node(service, D, 'Namespace', M2)
        node(service, D, 'XAddr', config.public + '/onvif/media2_service')
        include = any(local(e.tag) == 'Capabilities' for e in response.iter())
        if include:
            media_capabilities(node(service, D, 'Capabilities'), M2, ptz_control.enabled(config))
        v = node(service, D, 'Version'); node(v, TT, 'Major', 20); node(v, TT, 'Minor', 6)
    elif operation == 'GetCapabilities':
        for parent in list(root.iter()):
            for child in list(parent):
                if local(child.tag) in ({'Events', 'DeviceIO', 'Imaging', 'Analytics'} | (set() if ptz_control.enabled(config) else {'PTZ'})):
                    parent.remove(child)
                elif local(child.tag) == 'MaximumNumberOfProfiles':
                    child.text = '1'
    elif operation == 'GetScopes':
        response = root.find('.//{' + D + '}GetScopesResponse')
        response.clear()
        for scope in (['onvif://www.onvif.org/type/ptz'] if ptz_control.enabled(config) else []) + ['onvif://www.onvif.org/type/video_encoder',
                      'onvif://www.onvif.org/name/' + urllib.parse.quote(config.name, safe=''),
                      'onvif://www.onvif.org/hardware/CAM-C44HE-Bridge']:
            e = node(response, D, 'Scopes'); node(e, TT, 'ScopeDef', 'Fixed'); node(e, TT, 'ScopeItem', scope)
    elif operation == 'GetDeviceInformation':
        for e in root.iter():
            if local(e.tag) == 'SerialNumber': e.text = (e.text or '') + '-' + config.lens
            elif local(e.tag) == 'Model': e.text = 'CAM-C44HE'
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
