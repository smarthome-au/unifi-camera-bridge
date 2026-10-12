import hashlib
import http.server
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import snapshots as s
import generic_onvif as g
from discovery import safe_xml
from test_discovery import plan
from dual_camera import S, M1, M2

CAMERA = dict(username='snapshot-user', password='private-password')
JPEG = b'\xff\xd8snapshot-test\xff\xd9'

class SnapshotTests(unittest.TestCase):
    def request(self, ns=M2, token='profile0'):
        return ET.fromstring(f'<m:GetSnapshotUri xmlns:m="{ns}"><m:ProfileToken>{token}</m:ProfileToken></m:GetSnapshotUri>')

    def test_media_versions_and_invalid_profile(self):
        for ns in (M1,M2):
            code,data=s.uri_response(self.request(ns), 'http://192.0.2.1:8100', ['profile0'])
            self.assertEqual(code,200)
            root=ET.fromstring(data)
            self.assertEqual(next(e.text for e in root.iter() if e.tag.endswith('}Uri')), 'http://192.0.2.1:8100/snapshot.jpg')
            self.assertNotIn(CAMERA['password'].encode(),data)
            self.assertEqual('MediaUri' in data.decode(), ns==M1)
        self.assertNotEqual(s.uri_response(self.request(token='wrong'), 'http://a', ['profile0'])[0],200)

    def test_generic_advertisement_and_profile_response(self):
        camera=dict(CAMERA,discovery=plan(),ptz=False)
        op=self.request(); root=ET.Element('{'+S+'}Envelope');ET.SubElement(root,'{'+S+'}Body').append(op)
        code,data=g.response(root,camera,0,'http://192.0.2.1:8102',8602,'02:00:00:00:00:02')
        self.assertEqual(code,200);self.assertIn(b':8102/snapshot.jpg',data)
        cap=ET.Element('root');g.media_caps(cap,'Capabilities',camera['discovery']['views'][0])
        self.assertEqual(cap[0].get('SnapshotUri'),'true')

    def header(self, service, path=s.PATH, nc='00000001'):
        fields=urllib.request.parse_keqv_list(urllib.request.parse_http_list(service.challenge()[7:]))
        nonce=fields['nonce'];md5=lambda x:hashlib.md5(x.encode()).hexdigest()
        a=md5(f"{CAMERA['username']}:{s.REALM}:{CAMERA['password']}")
        response=md5(f'{a}:{nonce}:{nc}:client:auth:{md5("GET:"+path)}')
        return f'Digest username="{CAMERA["username"]}", realm="{s.REALM}", nonce="{nonce}", uri="{path}", qop=auth, nc={nc}, cnonce="client", response="{response}"'

    def test_digest_replay_wrong_path_and_cross_lens_nonce_rejected(self):
        service=s.Service(CAMERA,'rtsp://127.0.0.1:1234/test',True)
        header=self.header(service)
        self.assertFalse(service.authenticated(header,'/wrong'))
        self.assertFalse(s.Service(CAMERA,'rtsp://127.0.0.1:1234/other',True).authenticated(header,s.PATH))
        self.assertTrue(service.authenticated(header,s.PATH))
        self.assertFalse(service.authenticated(header,s.PATH))
        self.assertTrue(service.authenticated(self.header(service,nc='00000002'),s.PATH))
        self.assertFalse(service.authenticated(None,s.PATH))
        self.assertFalse(service.authenticated('Digest broken',s.PATH))

    def test_expired_nonce_rejected(self):
        service=s.Service(CAMERA,'rtsp://127.0.0.1:1234/test',True)
        with patch('snapshots.time.time',return_value=1000):header=self.header(service)
        with patch('snapshots.time.time',return_value=1121):self.assertFalse(service.authenticated(header,s.PATH))

    def test_http_digest_challenge_and_image(self):
        service=s.Service(CAMERA,'rtsp://127.0.0.1:1234/test',True)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):service.serve(self)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}{s.PATH}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(url)
            self.assertEqual(ctx.exception.code,401)
            passwords=urllib.request.HTTPPasswordMgrWithDefaultRealm();passwords.add_password(None,url,**{'user':CAMERA['username'],'passwd':CAMERA['password']})
            opener=urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(passwords))
            with patch.object(service,'capture',return_value=JPEG):
                with opener.open(url) as r:
                    self.assertEqual(r.headers['Content-Type'],'image/jpeg');self.assertEqual(r.read(),JPEG)
            with patch.object(service,'capture',side_effect=TimeoutError()):
                with self.assertRaises(urllib.error.HTTPError) as ctx:opener.open(url)
                self.assertEqual(ctx.exception.code,503)
        finally:server.shutdown();server.server_close();thread.join()

    def test_one_second_cache_and_no_stale_fallback(self):
        service=s.Service(CAMERA,'rtsp://127.0.0.1:1234/test',True)
        with patch.object(service,'frame',return_value=JPEG) as frame:
            self.assertEqual(service.capture(),JPEG);self.assertEqual(service.capture(),JPEG)
            self.assertEqual(frame.call_count,1)
        service.cached_at-=2
        with patch.object(service,'frame',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):service.capture()

    def test_capture_argv_has_no_secrets_and_image_validation(self):
        from subprocess import CompletedProcess
        with patch('snapshots.subprocess.run',return_value=CompletedProcess([],0,JPEG)) as run:
            self.assertEqual(s.Service.frame('rtsp://127.0.0.1:1234/test'),JPEG)
            self.assertNotIn(CAMERA['password'],str(run.call_args))
        with patch('snapshots.subprocess.run',return_value=CompletedProcess([],0,b'not a jpeg')):
            with self.assertRaises(ValueError):s.Service.frame('rtsp://127.0.0.1:1234/test')

if __name__=='__main__':unittest.main()
