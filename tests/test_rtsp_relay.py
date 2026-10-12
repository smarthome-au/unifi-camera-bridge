import concurrent.futures
import contextlib
import io
import socket
import socketserver
import sys
from pathlib import Path
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtsp_relay import RTSPRelay


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class RelayTests(unittest.TestCase):
    def test_concurrent_streams_preserve_control_and_binary_data(self):
        camera = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        relay = RTSPRelay(('127.0.0.1', 0), '127.0.0.1', camera.server_address[1])
        for server in [camera, relay]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        marker = b'DESCRIBE rtsp://example.test/av_stream/ch0 RTSP/1.0\r\nAuthorization: Dummy TEST_ONLY_SECRET\r\n\r\n'
        payload = marker + b'$\x00\xff\xff' + bytes(range(256)) * 256

        def session(_):
            with socket.create_connection(relay.server_address, timeout=3) as s:
                s.sendall(payload)
                result = b''
                while len(result) < len(payload):
                    part = s.recv(65536)
                    if not part:
                        break
                    result += part
                self.assertEqual(result, payload)

        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(session, range(2)))
                relay.shutdown()
                camera.shutdown()
            self.assertNotIn('TEST_ONLY_SECRET', captured.getvalue())
            self.assertNotIn('Authorization', captured.getvalue())
            self.assertEqual(relay.stats()['sessions'], 2)
            self.assertEqual(relay.stats()['bytes_from_camera'], len(payload) * 2)
        finally:
            relay.server_close()
            camera.server_close()


if __name__ == '__main__':
    unittest.main()
