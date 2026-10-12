"""CAM-C44HE directional pan/tilt adapter; native camera authenticates requests.

Only the PTZ virtual lens exposes this service. Native Profile_0 controls its
motor; both virtual quality profiles must map to that token. Position/zoom,
presets and home operations are withheld until their behavior is validated.
"""
import copy
import math
import re
import xml.etree.ElementTree as E

S = 'http://www.w3.org/2003/05/soap-envelope'
TT = 'http://www.onvif.org/ver10/schema'
P = 'http://www.onvif.org/ver20/ptz/wsdl'
M1 = 'http://www.onvif.org/ver10/media/wsdl'
VELOCITY = 'http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace'
READ = {'GetServiceCapabilities', 'GetConfigurations', 'GetConfiguration',
        'GetConfigurationOptions', 'GetNodes', 'GetNode', 'GetStatus', 'GetPresets'}
ALLOWED = READ | {'ContinuousMove', 'Stop'}
E.register_namespace('tptz', P)


def enabled(config):
    return config.adapter == 'cam-c44he' and config.lens == 'ptz' and getattr(config, 'ptz', False)


def operation(data):
    return E.fromstring(data).find('{' + S + '}Body')[0]


def is_ptz(data):
    return operation(data).tag.startswith('{' + P + '}')


def prepare(data):
    op = operation(data)
    name = op.tag.rsplit('}', 1)[-1]
    if name not in ALLOWED: raise ValueError('Unsupported PTZ operation')
    token = op.find('{' + P + '}ProfileToken')
    if name in {'GetStatus', 'GetPresets', 'ContinuousMove', 'Stop'}:
        if token is None or token.text not in {'Profile_0', 'Profile_1'}:
            raise ValueError('Unknown PTZ profile')
        token.text = 'Profile_0'
    for tag, expected in [('ConfigurationToken','PTZCfgToken'), ('PTZConfigurationToken','PTZCfgToken'), ('NodeToken','PTZNodeToken')]:
        for t in op.findall('{' + P + '}' + tag):
            if t.text != expected: raise ValueError('Unknown PTZ configuration')
    if name == 'ContinuousMove':
        velocity = op.find('{' + P + '}Velocity')
        pan = None if velocity is None else velocity.find('{' + TT + '}PanTilt')
        if pan is None: raise ValueError('Pan/tilt velocity required')
        if pan.get('space', VELOCITY) != VELOCITY: raise ValueError('Unsupported velocity space')
        for axis in ['x', 'y']:
            value = float(pan.get(axis, 'nan'))
            if not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError('Invalid velocity')
        zoom = velocity.find('{' + TT + '}Zoom')
        if zoom is not None:
            if float(zoom.get('x', 'nan')) != 0: raise ValueError('Zoom unsupported')
            velocity.remove(zoom)
        timeout = op.find('{' + P + '}Timeout')
        if timeout is None: timeout = E.SubElement(op, '{' + P + '}Timeout')
        # Bound each accepted movement; the bridge watchdog enforces this.
        seconds = 1.0
        if timeout.text:
            m = re.fullmatch(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?', timeout.text)
            if not m or not any(m.groups()): raise ValueError('Invalid timeout')
            seconds = sum(float(x or 0)*scale for x,scale in zip(m.groups(),[3600,60,1]))
        timeout.text = f'PT{max(1.0,min(2.0,seconds)):g}S'
    text = data.decode()
    new_body = E.tostring(op,encoding='unicode')
    pattern = r'(<(?P<p>[\w.-]+:)?Body\b[^>]*>).*?(</(?P=p)Body>)'
    text,n = re.subn(pattern,lambda m:m[1]+new_body+m[3],text,flags=re.S)
    if not n: text,n=re.subn(r'(<Body\b[^>]*>).*?(</Body>)',lambda m:m[1]+new_body+m[2],text,flags=re.S)
    if n != 1: raise ValueError('Unsupported envelope')
    return text.encode()


def clean(root):
    """Remove unsupported absolute/relative/zoom/preset capabilities."""
    for parent in list(root.iter()):
        for child in list(parent):
            tag = child.tag.rsplit('}',1)[-1]
            if ('Zoom' in tag or 'Absolute' in tag or 'Relative' in tag
                    or tag in {'PanTiltLimits','Position','Extension','PTControlDirection'}):
                parent.remove(child)
            elif tag == 'MaximumNumberOfPresets': child.text = '0'
            elif tag == 'HomeSupported': child.text = 'false'
            elif tag == 'UseCount': child.text = '2'
            elif tag == 'DefaultPTZTimeout': child.text = 'PT1S'
            elif tag == 'PTZTimeout':
                for sub in child:
                    sub.text = 'PT1S' if sub.tag.endswith('}Min') else 'PT2S'
            elif tag == 'Capabilities':
                child.set('StatusPosition','false')
    return root


def profile_configuration(auth_root, ns, tag):
    profile = auth_root.find('.//{' + M1 + '}Profiles[@token="Profile_0"]')
    raw = None if profile is None else profile.find('{' + TT + '}PTZConfiguration')
    if raw is None: return None
    config = clean(copy.deepcopy(raw))
    config.tag = '{' + ns + '}' + tag
    return config


def response(data):
    root = E.fromstring(data)
    if root.find('.//{' + S + '}Fault') is not None: return data
    return E.tostring(clean(root),encoding='utf-8',xml_declaration=True)


def stop_request(data, zero=False):
    text=data.decode()
    body=f'<tptz:Stop xmlns:tptz="{P}"><tptz:ProfileToken>Profile_0</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>'
    if zero:
        body=f'<tptz:ContinuousMove xmlns:tptz="{P}" xmlns:tt="{TT}"><tptz:ProfileToken>Profile_0</tptz:ProfileToken><tptz:Velocity><tt:PanTilt x="0" y="0"/><tt:Zoom x="0"/></tptz:Velocity><tptz:Timeout>PT1S</tptz:Timeout></tptz:ContinuousMove>'
    pattern=r'(<(?P<p>[\w.-]+:)?Body\b[^>]*>).*?(</(?P=p)Body>)'
    text,n=re.subn(pattern,lambda m:m[1]+body+m[3],text,flags=re.S)
    if not n:text,n=re.subn(r'(<Body\b[^>]*>).*?(</Body>)',lambda m:m[1]+body+m[2],text,flags=re.S)
    if n!=1:raise ValueError('Unsupported envelope')
    action = 'ContinuousMove' if zero else 'Stop'
    text=re.sub(r'(<(?:[\w.-]+:)?Action\b[^>]*>)[^<]*(</(?:[\w.-]+:)?Action>)',lambda m:m[1]+P+'/'+action+m[2],text)
    return text.encode()


class Watchdog:
    """Bound motion even on firmware ignoring ContinuousMove.Timeout.

    All movement/Stop requests and timer callbacks share this lock, so an old
    timer cannot stop a newer movement. The incoming authentication header is
    reused only for its associated Stop, then discarded. CAM-C44HE accepts
    this short-lived WSSE reuse; other firmware must be revalidated.
    """
    def __init__(self, camera, port):
        import threading
        self.lock=threading.RLock();self.timer=None;self.generation=0;self.pending=None
        self.camera=camera;self.port=port

    def cancel(self):
        self.generation+=1
        if self.timer:self.timer.cancel();self.timer=None
        self.pending=None

    def stop_now(self):
        with self.lock:
            if self.pending:self.send_stop(*self.pending)
            self.cancel()

    def arm(self, data, headers, send=None):
        import threading
        self.cancel()
        seconds=float(operation(data).findtext('{'+P+'}Timeout')[2:-1])
        generation=self.generation
        body=stop_request(data)
        stop_headers=dict(headers)
        stop_headers.update({'Content-Length':str(len(body)), 'SOAPAction':'"'+P+'/Stop"',
                             'Content-Type':'application/soap+xml; charset=utf-8; action="'+P+'/Stop"'})
        self.pending=(body,stop_headers)
        def fire(attempt=0):
            with self.lock:
                if generation!=self.generation:return
                ok=(send or self.send_stop)(body,stop_headers)
                if ok:self.cancel();return
                if attempt<2:
                    self.timer=threading.Timer(.5,fire,args=(attempt+1,));self.timer.daemon=True;self.timer.start()
        self.timer=threading.Timer(seconds,fire);self.timer.daemon=True;self.timer.start()

    def send_stop(self, body, headers):
        import http.client,json
        success=[]
        # Send zero velocity as an additional stop signal, then standard Stop.
        # Automatic tracking may independently resume movement.
        for zero in [True, False]:
            action='ContinuousMove' if zero else 'Stop'
            payload=stop_request(body,zero=zero)
            h=dict(headers)
            h.update({'Content-Length':str(len(payload)), 'SOAPAction':'"'+P+'/'+action+'"',
                      'Content-Type':'application/soap+xml; charset=utf-8; action="'+P+'/'+action+'"'})
            connection=http.client.HTTPConnection(self.camera,self.port,timeout=2)
            ok=False
            try:
                connection.request('POST','/onvif/ptz_service',payload,h)
                r=connection.getresponse();content=r.read(65536)
                ok=r.status==200 and E.fromstring(content).find('.//{'+P+'}'+action+'Response') is not None
            except Exception:pass
            finally:connection.close()
            success.append(ok)
        print(json.dumps({'operation':'WatchdogStop','zero_velocity_acknowledged':success[0],
                          'stop_acknowledged':success[1]}),flush=True)
        return all(success)
