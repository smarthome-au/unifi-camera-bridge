"""Verified policies applied to live inspection results, never device credentials."""
import copy
import re

DISCOVERED_MODELS = ('auto', 'cam-pt2', 'rtsp')


def apply(model, plan):
    if model != 'cam-pt2':
        return plan
    device = plan.get('device', {})
    if re.sub(r'[^A-Z0-9]', '', device.get('Model', '').upper()) != 'CAMPT2':
        raise ValueError('CAM-PT2 preset: the camera reports a different model. Use Automatic setup.')
    views = plan.get('views', [])
    if len(views) != 1:
        raise ValueError('CAM-PT2 preset: expected one lens. Use Automatic setup to review this camera.')
    hq = next((s for s in views[0]['streams'] if s['quality'] == 'hq'), None)
    if not hq or (hq['video']['codec'], hq['video']['width'], hq['video']['height']) != ('H264', 1920, 1080):
        raise ValueError('CAM-PT2 preset: expected native H.264 1080p. Use Automatic setup to review this firmware.')
    result = copy.deepcopy(plan)
    result['views'][0]['label'] = 'Camera view'
    result['views'][0]['streams'] = [copy.deepcopy(hq)]
    result['warnings'] = [w for w in result['warnings'] if not w.startswith('Verified CAM-PT2 preset:') and w != 'Pan/tilt could not be verified on this camera and will not be advertised.']
    result['warnings'].append('Verified CAM-PT2 preset: native 1080p only. Firmware 1.1.0 advertises SD but does not provide a verified separate RTSP stream. No SD stream is generated.')
    if not views[0].get('ptz'):
        result['warnings'].append('Pan/tilt could not be verified on this camera and will not be advertised.')
    return result
