import contextlib
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Settings, Server, NS, operation_of, fix_response

FIXTURES = json.loads(Path(__file__).with_name('fixtures.json').read_text())
BODY = b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Header><unchanged-test-marker/></s:Header><s:Body><tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities></s:Body></s:Envelope>'


def settings():
    with patch.dict(os.environ, {'BRIDGE_URL': 'http://192.0.2.10:8096',
                                'CAMERA_IP': '10.11.11.196',
                                'CAMERA_ADAPTER': 'cam-s3w', 'ALLOWED_CLIENTS': ''}):
        return Settings()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()

    def result(self, name):
        return fix_response(FIXTURES[name].encode(), name.split(':')[0], self.config)

    def test_both_profiles_preserve_dimensions_and_correct_metadata(self):
        root = ET.fromstring(self.result('GetProfiles'))
        profiles = root.findall('.//trt:Profiles', NS)
        self.assertEqual(len(profiles), 2)
        self.assertEqual([p.findtext('.//tt:Width', namespaces=NS) for p in profiles], ['2304', '640'])
        self.assertEqual([p.findtext('.//tt:Height', namespaces=NS) for p in profiles], ['1296', '360'])
        for p in profiles:
            self.assertEqual(p.findtext('.//tt:FrameRateLimit', namespaces=NS), '20')
            self.assertEqual(p.findtext('.//tt:H264Profile', namespaces=NS), 'High')
            self.assertEqual(p.findtext('.//tt:SampleRate', namespaces=NS), '8')
            self.assertEqual(p.findtext('.//tt:Bitrate', namespaces=NS), '64')
            self.assertEqual(p.findtext('.//tt:BitrateLimit', namespaces=NS), '5000')

    def test_streams_use_bridge_rtsp_port_and_remain_distinct(self):
        for i in range(2):
            root = ET.fromstring(self.result(f'GetStreamUri:Profile_{i}'))
            self.assertEqual(root.findtext('.//tt:Uri', namespaces=NS), f'rtsp://192.0.2.10:554/av_stream/ch{i}')

    def test_all_service_addresses_go_through_bridge(self):
        for op in ['GetServices', 'GetCapabilities']:
            root = ET.fromstring(self.result(op))
            for e in root.iter():
                if e.tag.endswith('}XAddr'):
                    self.assertTrue(e.text.startswith(self.config.public + '/onvif/'))

    def test_capabilities_and_empty_responses(self):
        self.assertIn(b'UsernameToken="true"', self.result('GetServices'))
        self.assertIn(b'SnapshotUri="false"', self.result('GetServices'))
        self.assertIn(b'HostnameInformation', self.result('GetHostname'))
        self.assertIn(b'DNSInformation', self.result('GetDNS'))
        self.assertIn(b'>8096<', self.result('GetNetworkProtocols'))

    def test_virtual_identity(self):
        root = ET.fromstring(self.result('GetNetworkInterfaces'))
        self.assertEqual(root.findtext('.//tt:HwAddress', namespaces=NS), self.config.mac)
        self.assertEqual(root.findtext('.//tt:Address', namespaces=NS), '192.0.2.10')

    def test_passthrough_does_not_change_media(self):
        self.config.adapter = 'passthrough'
        self.assertIn(b'<tt:FrameRateLimit>30</tt:FrameRateLimit>', self.result('GetProfiles'))
        self.assertIn(b'UsernameToken="false"', self.result('GetServices'))

    def test_event_namespaces_are_preserved(self):
        before = FIXTURES['GetEventProperties'].encode()
        after = self.result('GetEventProperties')
        self.assertEqual(before, after)

    def test_reject_entities(self):
        with self.assertRaises(ValueError):
            operation_of(b'<!DOCTYPE x [<!ENTITY a "bad">]>' + BODY)


class FakeCamera(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        self.server.seen.append((list(self.headers.raw_items()), data))
        self.send_response(self.server.response_status)
        self.send_header('Content-Length', str(len(self.server.response_data)))
        self.end_headers()
        self.wfile.write(self.server.response_data)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.camera = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeCamera)
        self.camera.seen = []
        self.camera.response_status = 200
        self.camera.response_data = FIXTURES['GetCapabilities'].encode()
        c = settings()
        c.camera = '127.0.0.1'
        c.camera_port = self.camera.server_port
        self.bridge = Server(('127.0.0.1', 0), c)
        for server in [self.camera, self.bridge]:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in [self.bridge, self.camera]:
            server.shutdown()
            server.server_close()

    def call(self, data=BODY, chunked=False):
        h = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port)
        headers = {'Content-Type': 'application/soap+xml'}
        if chunked:
            headers['Transfer-Encoding'] = 'chunked'
            data = iter([data[:55], data[55:]])
        else:
            headers['content-length'] = str(len(data))
        h.request('POST', '/onvif/device_service', data, headers, encode_chunked=chunked)
        r = h.getresponse()
        result = r.status, r.read()
        h.close()
        return result

    def test_lowercase_client_header_is_normalized_upstream(self):
        self.assertEqual(self.call()[0], 200)
        headers, data = self.camera.seen[0]
        self.assertIn(('Content-Length', str(len(BODY))), headers)
        self.assertNotIn(('content-length', str(len(BODY))), headers)
        self.assertEqual(data, BODY)  # Authentication header bytes stay intact.

    def test_chunked_client_is_buffered_to_known_length(self):
        self.assertEqual(self.call(chunked=True)[0], 200)
        headers, data = self.camera.seen[0]
        self.assertEqual(data, BODY)
        self.assertFalse(any(k.lower() == 'transfer-encoding' for k, _ in headers))

    def test_camera_fault_is_preserved(self):
        self.camera.response_status = 400
        self.camera.response_data = b'<error>NotAuthorized</error>'
        self.assertEqual(self.call(), (400, self.camera.response_data))

    def test_settings_writes_are_not_forwarded(self):
        self.assertEqual(self.call(BODY.replace(b'GetCapabilities', b'SetHostname'))[0], 400)
        self.assertEqual(self.camera.seen, [])

    def test_logs_do_not_contain_request_body(self):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.call()
        self.assertNotIn('unchanged-test-marker', capture.getvalue())
        self.assertNotIn('<s:', capture.getvalue())


if __name__ == '__main__':
    unittest.main()
