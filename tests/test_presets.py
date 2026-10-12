import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_discovery import plan, stream
import presets
from manager import Store, Supervisor


def pt2_plan():
    p = plan([stream(uri='rtsp://192.168.1.10/av_stream/ch0')])
    p['device'] = dict(Manufacturer='SONOFF', Model='CAM-PT2', FirmwareVersion='1.1.0')
    p['views'][0]['ptz'] = {'profile': 'Profile_0'}
    p['views'][0]['streams'][0]['audio'] = {'codec': 'PCMA', 'sample_rate': 8000, 'channels': 1}
    return p


class PresetTests(unittest.TestCase):
    def test_pt2_preserves_native_audio_and_ptz_without_lq_or_transcoding(self):
        p = pt2_plan()
        result = presets.apply('cam-pt2', p)
        self.assertEqual(result['views'][0]['streams'], p['views'][0]['streams'])
        self.assertEqual(result['views'][0]['ptz'], p['views'][0]['ptz'])
        self.assertEqual(len(result['views'][0]['streams']), 1)
        self.assertFalse(result['views'][0]['streams'][0]['convert_video'])
        self.assertEqual(presets.apply('cam-pt2', result), result)
        self.assertEqual(p['warnings'], [])

    def test_wrong_model_or_unverified_video_rejected(self):
        p = pt2_plan()
        for wrong in ('CAM-C44HE', 'Unknown'):
            q = copy.deepcopy(p); q['device']['Model'] = wrong
            with self.assertRaisesRegex(ValueError, 'different model'): presets.apply('cam-pt2', q)
        q = copy.deepcopy(p); q['views'].append(copy.deepcopy(q['views'][0]))
        with self.assertRaisesRegex(ValueError, 'one lens'): presets.apply('cam-pt2', q)
        q = copy.deepcopy(p); q['views'][0]['streams'][0]['video']['height'] = 360
        with self.assertRaisesRegex(ValueError, '1080p'): presets.apply('cam-pt2', q)
        q = copy.deepcopy(p); q['views'][0].pop('ptz')
        self.assertNotIn('ptz', presets.apply('cam-pt2', q)['views'][0])

    def test_store_routes_preset_to_verified_generic_workers_and_preserves_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            c = dict(revision=0, name='PT2', model='cam-pt2', address='192.168.1.10', username='camera', password='private-secret')
            with self.assertRaisesRegex(ValueError, 'Inspect'): store.mutate('camera', c)
            c['_discovery'] = pt2_plan(); store.mutate('camera', c)
            saved = copy.deepcopy(store.data['cameras'][0])
            self.assertTrue(saved['ptz'])
            self.assertEqual(len(saved['endpoints']), 1)
            supervisor = Supervisor(store, Path(temp)/'runtime')
            with patch('manager.resolved', side_effect=lambda value:value): specs = supervisor.desired()
            self.assertEqual({s[1] for s in specs.values()}, {'generic_onvif.py','generic_media.py'})
            self.assertNotIn('private-secret', json.dumps(specs))
            self.assertNotIn('rtsp://', json.dumps(store.public()))
            c.update(id=saved['id'], revision=1, name='Renamed'); c.pop('_discovery')
            store.mutate('camera', c)
            self.assertEqual(store.data['cameras'][0]['endpoints'], saved['endpoints'])
            c.update(revision=2, address='192.168.1.11')
            with self.assertRaisesRegex(ValueError, 'Inspect again'): store.mutate('camera', c)
