import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as E
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dual_camera as d
from test_bridge import settings
import test_bridge
from bridge import operation_of

AUTH = f'<s:Envelope xmlns:s="{d.S}" xmlns:trt="{d.M1}"><s:Body><trt:GetProfilesResponse/></s:Body></s:Envelope>'.encode()
HEADER = '<s:Header><wsse:Security xmlns:wsse="urn:test"><wsse:UsernameToken>dummy-auth-marker</wsse:UsernameToken></wsse:Security></s:Header>'

def request(op, inner='', ns=d.M2):
    return f'<s:Envelope xmlns:s="{d.S}" xmlns:m="{ns}">{HEADER}<s:Body><m:{op}>{inner}</m:{op}></s:Body></s:Envelope>'.encode()

class DualTests(unittest.TestCase):
    def setUp(self):
        self.c = settings(); self.c.adapter = 'cam-c44he'; self.c.lens = 'fixed'
    def call(self, op, inner='', ns=d.M2):
        status, data = d.media_response(AUTH, request(op, inner, ns), self.c)
        self.assertEqual(status, 200)
        return E.fromstring(data)
    def test_media2_profiles_pair_same_lens_and_actual_formats(self):
        root = self.call('GetProfiles', '<m:Type>All</m:Type>')
        p = root.findall('.//{' + d.M2 + '}Profiles')
        self.assertEqual([x.get('token') for x in p], ['Profile_0', 'Profile_1'])
        self.assertEqual([x.findtext('.//{' + d.M2 + '}VideoEncoder/{' + d.TT + '}Encoding') for x in p], ['H265', 'H264'])
        self.assertEqual([x.findtext('.//{' + d.TT + '}Width') for x in p], ['2560', '640'])
        self.assertEqual([x.findtext('.//{' + d.TT + '}SourceToken') for x in p], ['fixed_VideoSource'] * 2)
        self.assertEqual([x.findtext('.//{' + d.M2 + '}AudioEncoder/{' + d.TT + '}Encoding') for x in p], ['PCMA'] * 2)
        self.assertNotIn('PTZ', E.tostring(root).decode())
    def test_media1_does_not_claim_h265_support(self):
        root = self.call('GetProfiles', ns=d.M1)
        self.assertEqual([x.get('token') for x in root.findall('.//{' + d.M1 + '}Profiles')], ['Profile_1'])
        self.assertNotIn('H265', E.tostring(root).decode())
    def test_profiles_filter_by_type_and_token(self):
        root = self.call('GetProfiles', '<m:Token>Profile_0</m:Token><m:Type>VideoEncoder</m:Type>')
        self.assertEqual(len(root.findall('.//{' + d.M2 + '}Profiles')), 1)
        self.assertIsNone(root.find('.//{' + d.M2 + '}AudioSource'))
    def test_four_distinct_stream_mappings(self):
        for lens, index in [('fixed', 1), ('ptz', 0)]:
            self.c.lens = lens
            for i, suffix in [(0, 'main'), (1, 'sec')]:
                root = self.call('GetStreamUri', f'<m:Protocol>RTSP</m:Protocol><m:ProfileToken>Profile_{i}</m:ProfileToken>')
                self.assertEqual(root.findtext('.//{' + d.M2 + '}Uri'), f'rtsp://192.0.2.10:554/av_stream{index}/{suffix}')
    def test_optional_h265_lq_metadata_and_routing_with_native_media1(self):
        self.c.lq_h265_port = 8557
        root = self.call('GetProfiles')
        self.assertEqual([x.text for x in root.findall('.//{' + d.M2 + '}VideoEncoder/{' + d.TT + '}Encoding')], ['H265', 'H265'])
        for op in ['GetVideoEncoderConfigurations', 'GetVideoEncoderConfigurationOptions']:
            root = self.call(op, '<m:ConfigurationToken>VideoEncoder_1</m:ConfigurationToken>')
            self.assertEqual(root.findtext('.//{' + d.TT + '}Encoding'), 'H265')
        for lens, index in [('fixed', 1), ('ptz', 0)]:
            self.c.lens = lens
            for token, port, suffix in [(0, 554, 'main'), (1, 8557, 'sec')]:
                root = self.call('GetStreamUri', f'<m:Protocol>RTSP</m:Protocol><m:ProfileToken>Profile_{token}</m:ProfileToken>')
                self.assertEqual(root.findtext('.//{' + d.M2 + '}Uri'), f'rtsp://192.0.2.10:{port}/av_stream{index}/{suffix}')
        self.assertNotIn('H265', E.tostring(self.call('GetProfiles', ns=d.M1)).decode())
        root = self.call('GetStreamUri', '<m:ProfileToken>Profile_1</m:ProfileToken>', ns=d.M1)
        self.assertIn(':554/av_stream0/sec', E.tostring(root).decode())

    def test_routed_h265_mode_does_not_advertise_legacy_h264_profiles(self):
        self.c.lq_h265_port = 8555
        self.c.lq_target = ('converter', 8554)
        self.assertEqual(len(self.call('GetProfiles', ns=d.M1).findall('.//{' + d.M1 + '}Profiles')), 0)
        fixtures = json.loads(Path(__file__).with_name('dual-fixtures.json').read_text())
        root = E.fromstring(d.device_response(fixtures['GetServices'].encode(), 'GetServices', self.c))
        namespaces = [n.text for n in root.findall('.//{' + d.D + '}Namespace')]
        self.assertIn(d.M2, namespaces)
        self.assertNotIn(d.M1, namespaces)

    def test_bad_tokens_and_unsupported_transport_are_faults(self):
        for inner in ['<m:ProfileToken>wrong</m:ProfileToken>', '<m:ProfileToken>Profile_0</m:ProfileToken><m:Protocol>UDP</m:Protocol>']:
            self.assertEqual(d.media_response(AUTH, request('GetStreamUri', inner), self.c)[0], 400)
    def test_camera_auth_fault_cannot_become_success(self):
        fault = d.error('NotAuthorized')[1]
        self.assertEqual(d.media_response(fault, request('GetProfiles'), self.c), (400, fault))
        self.assertEqual(d.media_response(b'<wrong/>', request('GetProfiles'), self.c)[0], 400)
    def test_security_header_preserved_when_authenticating(self):
        data = d.authentication_request(request('GetStreamUri'))
        self.assertIn(HEADER.encode(), data)
        self.assertEqual(operation_of(data), 'GetProfiles')
        self.assertTrue(d.request_operation(data).tag.startswith('{' + d.M1 + '}'))
    def test_services_identity_and_capabilities(self):
        fixtures = json.loads(Path(__file__).with_name('dual-fixtures.json').read_text())
        data = d.device_response(fixtures['GetServices'].encode(), 'GetServices', self.c)
        root = E.fromstring(data)
        self.assertEqual(root.findall('.//{' + d.D + '}Namespace')[-1].text, d.M2)
        self.assertNotIn('ptz_service', data.decode())
        self.assertNotIn('events_service', data.decode())
        serials = []
        for lens in ['fixed', 'ptz']:
            self.c.lens = lens
            r = E.fromstring(d.device_response(fixtures['GetDeviceInformation'].encode(), 'GetDeviceInformation', self.c))
            serials.append(r.findtext('.//{' + d.D + '}SerialNumber'))
        self.assertNotEqual(*serials)
    def test_encoder_filter_matches_stream(self):
        root = self.call('GetVideoEncoderConfigurations', '<m:ConfigurationToken>VideoEncoder_0</m:ConfigurationToken>')
        self.assertEqual(root.findtext('.//{' + d.TT + '}Encoding'), 'H265')
        self.assertEqual(len(root.findall('.//{' + d.M2 + '}Configurations')), 1)

class DualProxyTests(test_bridge.TransportTests):
    def test_media2_auth_forwarding_integration(self):
        self.bridge.config.adapter = 'cam-c44he'
        self.bridge.config.lens = 'ptz'
        self.camera.response_data = AUTH
        status, data = self.call(request('GetProfiles', '<m:Type>All</m:Type>'))
        self.assertEqual(status, 200)
        self.assertIn(b'H265', data)
        headers, forwarded = self.camera.seen[0]
        self.assertIn(HEADER.encode(), forwarded)
        self.assertEqual(operation_of(forwarded), 'GetProfiles')
        self.assertIn(('Content-Length', str(len(forwarded))), headers)

if __name__ == '__main__': unittest.main()
