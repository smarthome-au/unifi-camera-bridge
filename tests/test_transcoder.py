import json
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lq_transcoder import configuration, command, OUTPUTS

class TranscoderTests(unittest.TestCase):
    def test_external_login_only_reads_converted_outputs(self):
        config = configuration({'username': 'camera', 'password': 'p@ss:# /'}, '192.0.2.9')
        users = config['authInternalUsers']
        self.assertEqual(users[0]['permissions'], [{'action':'read', 'path': p} for p in OUTPUTS])
        self.assertEqual(users[1]['ips'], ['127.0.0.1', '::1'])
        self.assertEqual(config['apiAddress'], '127.0.0.1:9997')
        self.assertEqual(config['paths']['input0']['source'], 'rtsp://camera:p%40ss%3A%23%20%2F@192.0.2.9:554/av_stream1/sec')
        self.assertEqual(config['paths']['input1']['source'].rsplit('/',2)[-2:], ['av_stream0','sec'])
        self.assertNotIn('/main', json.dumps(config))
    def test_ffmpeg_has_no_camera_credentials_and_copies_audio(self):
        for i in range(2):
            cmd = command(i)
            self.assertIn('libx265', cmd)
            self.assertEqual(cmd[cmd.index('-c:a')+1], 'copy')
            self.assertNotIn('@', ' '.join(cmd))
            self.assertTrue(cmd[-1].endswith(OUTPUTS[i]))
    def test_reserved_any_login_fails_closed(self):
        with self.assertRaises(ValueError):
            configuration({'username':'any','password':'x'}, '192.0.2.9')

class RoutingTests(unittest.TestCase):
    def test_native_hq_and_converted_lq_route_without_altering_auth(self):
        import socket
        from rtsp_relay import select_destination
        for path, target in [('/av_stream1/main',('camera',554)),('/av_stream0/sec',('converter',8554)),('/av_stream1/sec/trackID=0',('converter',8554))]:
            a,b=socket.socketpair()
            try:
                request=f'DESCRIBE rtsp://192.0.2.2:8555{path} RTSP/1.0\r\nCSeq: 2\r\nAuthorization: Digest private-test-value\r\n\r\n'.encode()
                b.sendall(request)
                actual, data=select_destination(a,('camera',554),('converter',8554))
                self.assertEqual(actual,target);self.assertEqual(data,request)
            finally:a.close();b.close()
    def test_root_options_then_lq_describe(self):
        import socket
        from rtsp_relay import select_destination
        a,b=socket.socketpair()
        try:
            req=b'DESCRIBE rtsp://192.0.2.2:8555/av_stream1/sec RTSP/1.0\r\nCSeq: 2\r\n\r\n'
            b.sendall(b'OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n'+req)
            target, data=select_destination(a,('camera',554),('converter',8554))
            self.assertEqual(target,('converter',8554));self.assertEqual(data,req)
            self.assertIn(b'CSeq: 1',b.recv(4096))
        finally:a.close();b.close()
