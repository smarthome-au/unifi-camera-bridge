import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery
import rtsp_camera as r
from generic_media import configuration
from manager import Store, Supervisor, probe
import test_manager


def camera():
    return dict(model='rtsp', name='RTSP test', ip='192.168.1.20', address='camera.lan',
                rtsp_port=8554, onvif_port=80, username='camera', password='test-private', events=True, ptz=True)


def measure(c, records, progress):
    for i, record in enumerate(records):
        record['video'] = dict(codec='H265' if i == 0 else 'H264', width=1920 if i == 0 else 640,
                               height=1080 if i == 0 else 360, fps=15, profile='Main', bitrate=0)
        record['audio'] = dict(codec='AAC', rate=16000, channels=1, bitrate=0)


def inspected(two=True, anonymous=False):
    c = camera()
    r.inputs(dict(hq_path='/main?token=private-query', lq_path='/sub' if two else '', rtsp_anonymous=anonymous), c)
    return c, r.inspect(c, measure_fn=measure)


class RTSPTests(unittest.TestCase):
    def test_addresses_are_pinned_and_invalid_or_embedded_credentials_rejected(self):
        c = camera()
        self.assertEqual(r.stream_uri('/main?token=private', c), 'rtsp://192.168.1.20:8554/main?token=private')
        self.assertEqual(r.stream_uri('rtsp://camera.lan:9000/a', c), 'rtsp://192.168.1.20:9000/a')
        for value in ('rtsp://other.lan/a', 'http://camera.lan/a', '//other/a', 'rtsp://u:secret@camera.lan/a', '/a\nsecret', 'rtsp://camera.lan:99999/a', 'rtsp://camera.lan/a#secret', '', '/a b'):
            with self.assertRaises(ValueError) as err: r.stream_uri(value, c)
            self.assertNotIn('secret', str(err.exception))

    def test_optional_native_lq_and_only_mismatched_lq_converted(self):
        for two in (False, True):
            c, plan = inspected(two)
            c['discovery'] = plan
            self.assertFalse(c['ptz']); self.assertFalse(c['events'])
            self.assertIsNone(plan['events']); self.assertNotIn('ptz', plan['views'][0])
            streams = plan['views'][0]['streams']
            self.assertEqual(len(streams), 2 if two else 1)
            cfg, commands = configuration(c, 19000)
            self.assertEqual(len(commands), 1 if two else 0)
            self.assertFalse(streams[0]['convert_video'])
            self.assertNotIn('test-private', json.dumps(commands))
            public = json.dumps(discovery.public_report(plan))
            self.assertNotIn('private-query', public)
            self.assertNotIn('rtsp://', public)

    def test_anonymous_upstream_keeps_bridge_authenticated(self):
        c, plan = inspected(anonymous=True); c['discovery'] = plan
        cfg, _ = configuration(c, 19000)
        self.assertNotIn('@', cfg['paths']['view0_hq']['source'])
        self.assertEqual(cfg['authInternalUsers'][0]['user'], 'camera')
        self.assertEqual(cfg['authInternalUsers'][0]['pass'], 'test-private')
        self.assertEqual(cfg['authInternalUsers'][1]['ips'], ['127.0.0.1'])

    def test_invalid_unavailable_or_not_lower_lq_fails_without_silent_downgrade(self):
        c = camera()
        with self.assertRaises(ValueError): r.inputs(dict(hq_path='/same', lq_path='/same'), c)
        r.inputs(dict(hq_path='/main', lq_path='/low'), c)
        with self.assertRaises(ValueError): r.inspect(c, measure_fn=lambda *args: None)
        def equal(c, records, progress):
            measure(c, records, progress); records[1]['video'] = records[0]['video'].copy()
        with self.assertRaises(ValueError): r.inspect(c, measure_fn=equal)

    def test_store_secrets_identity_and_distinct_paths_on_same_camera(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            c, plan = inspected()
            payload = dict(c, revision=0, _discovery=plan)
            store.mutate('camera', payload)
            saved = copy.deepcopy(store.data['cameras'][0])
            self.assertFalse(saved['events']); self.assertFalse(saved['ptz'])
            public = json.dumps(store.public())
            for secret in ('test-private', 'private-query', '_rtsp_records', 'rtsp://'): self.assertNotIn(secret, public)
            payload.update(id=saved['id'], revision=1, name='Renamed', password='')
            payload.pop('_discovery'); store.mutate('camera', payload)
            self.assertEqual(store.data['cameras'][0]['endpoints'], saved['endpoints'])
            payload.pop('id'); payload.update(revision=2, password='test-private', _discovery=plan)
            with self.assertRaises(ValueError): store.mutate('camera', payload)
            plan = copy.deepcopy(plan); plan['views'][0]['streams'][0]['uri'] += '-other'
            payload['_discovery'] = plan; store.mutate('camera', payload)
            self.assertEqual(len(store.data['cameras']), 2)
            supervisor = Supervisor(store, Path(temp)/'runtime')
            with patch('manager.resolved', return_value='192.168.1.30'): specs = supervisor.desired()
            self.assertEqual(len(specs), 4)
            runtime = json.loads((Path(temp)/'runtime'/saved['id']/'camera.json').read_text())
            self.assertIn('192.168.1.30', runtime['discovery']['views'][0]['streams'][0]['uri'])

    def test_connection_test_reads_rtsp_and_detects_changed_properties(self):
        c, plan = inspected(); c['discovery'] = plan
        with patch('manager.resolved', return_value='192.168.1.20'), patch('discovery.measure', side_effect=measure):
            self.assertTrue(probe(c)['ok'])
            c['discovery']['views'][0]['streams'][0]['video']['width'] = 3840
            self.assertFalse(probe(c)['ok'])
        with patch('manager.resolved', return_value='192.168.1.20'), patch('discovery.measure', side_effect=RuntimeError('private-query')):
            result = probe(c); self.assertFalse(result['ok']); self.assertNotIn('private-query', json.dumps(result))


class RTSPHTTPTests(unittest.TestCase):
    setUp = test_manager.HTTPTests.setUp
    tearDown = test_manager.HTTPTests.tearDown
    request = test_manager.HTTPTests.request
    setup_login = test_manager.HTTPTests.setup_login
    def test_inspect_save_and_redaction_over_authenticated_api(self):
        self.setup_login()
        payload = dict(camera(), hq_path='/main?token=private-query', lq_path='/low', rtsp_anonymous=True)
        with patch('manager.resolved', return_value='192.168.1.20'), patch('discovery.measure', side_effect=measure), patch('discovery.Client', side_effect=AssertionError('RTSP must not call ONVIF')):
            status, job = self.request('/api/inspect', payload); self.assertEqual(status, 202)
            for _ in range(100):
                status, result = self.request('/api/inspection/' + job['id'])
                if result['status'] != 'running': break
                time.sleep(.01)
            self.assertEqual(result['status'], 'ready')
            self.assertNotIn('private-query', json.dumps(result))
            self.assertNotIn('test-private', json.dumps(result))
            self.assertEqual(self.request('/api/add-inspected', dict(inspection=job['id'], revision=1))[0], 200)
        saved = self.server.store.data['cameras'][0]
        self.assertEqual(saved['model'], 'rtsp'); self.assertTrue(saved['rtsp_anonymous'])
        self.assertFalse(saved['events']); self.assertFalse(saved['ptz'])
        self.assertNotIn('private-query', json.dumps(self.request('/api/state')[1]))
