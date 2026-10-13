"""Model-specific ONVIF response corrections; add new adapters explicitly.

CAM-S3W values are measured on firmware 1.2.1. Passthrough applies only the
shared HTTP/service-address/identity handling and does not repair video data.
"""
import re
import html
import ipaddress
import urllib.parse
import xml.etree.ElementTree as ET

NS = {'tt': 'http://www.onvif.org/ver10/schema', 'tds': 'http://www.onvif.org/ver10/device/wsdl'}

def localname(tag):
    return tag.rsplit('}', 1)[-1]

def replace_camera_http_urls(text, config):
    pattern = r'http://' + re.escape(config.camera) + r'(?::' + str(config.camera_port) + r')?(?=[/\s<"\']|$)'
    return re.sub(pattern, config.public, text)

def cam_s3w_response(data, operation, config):
    """Keep original XML prefixes/declarations (including QName-valued text).

    Parsing and serializing with ElementTree can lose declarations referenced
    only in event topics or fault text, so edits are targeted on the raw XML.
    """
    text = data.decode('utf-8')
    root = ET.fromstring(data)
    if any(localname(e.tag) == 'Fault' for e in root.iter()):
        return replace_camera_http_urls(text, config).encode()
    text = replace_camera_http_urls(text, config)
    if operation == 'GetStreamUri':
        # Protect substitutes the adopted host in RTSP URIs. Serve video there.
        pattern = r'rtsp://' + re.escape(config.camera) + r'(?::\d+)?(?=/)'
        text = re.sub(pattern, f'rtsp://{config.public_host}:{config.rtsp_port}', text)

    def value(tag, replacement):
        nonlocal text
        # Preserve element names and attributes in the original XML.
        pattern = r'(<(?:\w+:)?' + tag + r'(?:\s[^>]*)?>)[^<]*(</(?:\w+:)?' + tag + r'>)'
        text = re.sub(pattern, lambda m: m[1] + str(replacement) + m[2], text)

    def fill_empty(response, inner):
        nonlocal text
        pattern = r'<(?P<p>\w+:)?' + response + r'\s*/>'
        text = re.sub(pattern, lambda m: '<' + (m['p'] or '') + response + '>' + inner + '</' + (m['p'] or '') + response + '>', text)

    tt = NS['tt']
    tds = NS['tds']
    if config.adapter == 'cam-s3w' and operation in {'GetServices', 'GetCapabilities', 'GetServiceCapabilities'}:
        text = text.replace('UsernameToken="false"', 'UsernameToken="true"')
        text = text.replace('SnapshotUri="true"', 'SnapshotUri="false"')
    if operation == 'GetDeviceInformation':
        # Protect builds the adoption name from Manufacturer + Model.
        value('Manufacturer', html.escape(config.name))
        fill_empty('Manufacturer', html.escape(config.name))
        if config.adapter == 'cam-s3w':
            value('Model', 'CAM-S3W')
        # Distinct software identity avoids merging with native discovery.
        for e in root.iter():
            if localname(e.tag) == 'SerialNumber':
                value('SerialNumber', html.escape((e.text or 'CAM-S3W') + '-bridge'))
    if operation == 'GetNetworkInterfaces':
        value('HwAddress', config.mac)
        value('Name', 'bridge0')
        try:
            ipaddress.ip_address(config.public_host)
            value('Address', config.public_host)
            value('PrefixLength', config.advertised_netmask)
        except ValueError:
            pass
    if config.adapter == 'cam-s3w' and operation == 'GetHostname':
        fill_empty('GetHostnameResponse', f'<tds:HostnameInformation xmlns:tds="{tds}" xmlns:tt="{tt}"><tt:FromDHCP>false</tt:FromDHCP><tt:Name>cam-s3w-bridge</tt:Name></tds:HostnameInformation>')
    if config.adapter == 'cam-s3w' and operation == 'GetDNS':
        fill_empty('GetDNSResponse', f'<tds:DNSInformation xmlns:tds="{tds}" xmlns:tt="{tt}"><tt:FromDHCP>false</tt:FromDHCP></tds:DNSInformation>')
    if config.adapter == 'cam-s3w' and operation == 'GetNetworkProtocols':
        fill_empty('GetNetworkProtocolsResponse', f'<tds:NetworkProtocols xmlns:tds="{tds}" xmlns:tt="{tt}"><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{config.public_port}</tt:Port></tds:NetworkProtocols>')
    if config.adapter == 'cam-s3w' and operation == 'GetScopes':
        scope_name = urllib.parse.quote(config.name, safe='')
        text = re.sub(r'<(?:\w+:)?ScopeItem>scope=onvif://www.onvif.org/name</(?:\w+:)?ScopeItem>', f'<tt:ScopeItem xmlns:tt="{tt}">onvif://www.onvif.org/name/{scope_name}</tt:ScopeItem>', text)
        text = re.sub(r'<(?:\w+:)?ScopeItem>scope=onvif://www.onvif.org/hardware</(?:\w+:)?ScopeItem>', f'<tt:ScopeItem xmlns:tt="{tt}">onvif://www.onvif.org/hardware/CAM-S3W-Bridge</tt:ScopeItem>', text)
    if config.adapter == 'passthrough':
        ET.fromstring(text)
        return text.encode()
    # Verified live stream parameters for this model/firmware. Never mix lenses.
    if operation in {'GetProfiles', 'GetProfile', 'GetVideoEncoderConfiguration',
                     'GetVideoEncoderConfigurations', 'GetCompatibleVideoEncoderConfigurations'}:
        value('FrameRateLimit', 20)
        value('H264Profile', 'High')
    if operation == 'GetVideoEncoderConfigurationOptions':
        value('H264ProfilesSupported', 'High')
        pattern = r'(<(?:\w+:)?FrameRateRange>).*?(</(?:\w+:)?FrameRateRange>)'
        text = re.sub(pattern, lambda m: m[1] + f'<tt:Min xmlns:tt="{tt}">20</tt:Min><tt:Max xmlns:tt="{tt}">20</tt:Max>' + m[2], text, flags=re.S)
    if operation in {'GetProfiles', 'GetProfile', 'GetAudioEncoderConfiguration',
                     'GetAudioEncoderConfigurations', 'GetCompatibleAudioEncoderConfigurations'}:
        value('SampleRate', 8)
        value('Bitrate', 64)  # Not BitrateLimit, which is video.
    # Preserve unrelated extensions and ensure edited output remains XML.
    ET.fromstring(text)
    return text.encode()


def fix_response(data, operation, config):
    return cam_s3w_response(data, operation, config)
