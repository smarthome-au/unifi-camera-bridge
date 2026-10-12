"""Discovered pan/tilt mapping, constrained commands and fresh-auth stop watchdog."""
import copy
import math
import re
import threading
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as E
from xml.sax.saxutils import escape
from dual_camera import S, TT, node, envelope, error, local
from ptz_control import P, VELOCITY, clean


def discover(client, service, profile, configuration):
    from discovery import child, value
    response = client.call(service, P, 'GetConfiguration', '<t:PTZConfigurationToken>'+escape(configuration)+'</t:PTZConfigurationToken>')
    config = child(response, 'PTZConfiguration')
    if config is None or config.get('token') != configuration: raise ValueError('Missing PTZ configuration')
    node_token = value(config, 'NodeToken')
    response = client.call(service, P, 'GetNode', '<t:NodeToken>'+escape(node_token)+'</t:NodeToken>')
    ptz_node = child(response, 'PTZNode')
    if ptz_node is None or ptz_node.get('token') != node_token: raise ValueError('Missing PTZ node')
    spaces = child(ptz_node, 'SupportedPTZSpaces')
    space = next((s for s in spaces if local(s.tag)=='ContinuousPanTiltVelocitySpace' and value(s,'URI')==VELOCITY), None)
    if space is None: raise ValueError('Generic pan/tilt velocity unavailable')
    ranges = {}
    for axis in ('X','Y'):
        r=child(space,axis+'Range');lo=float(value(r,'Min'));hi=float(value(r,'Max'))
        if not math.isfinite(lo+hi) or not lo<=0<=hi or lo==hi: raise ValueError('Invalid PTZ velocity range')
        ranges[axis.lower()]=[max(-1,lo),min(1,hi)]
    response = client.call(service,P,'GetConfigurationOptions','<t:ConfigurationToken>'+escape(configuration)+'</t:ConfigurationToken>')
    options = child(response,'PTZConfigurationOptions')
    if options is None: raise ValueError('Missing PTZ options')
    # Keep the standard directional spaces; unsupported optical zoom, absolute
    # movement, presets and home are not advertised through this bridge.
    wrapped=[]
    for element in (config,ptz_node,options):
        wrapper=E.Element('wrapper');wrapper.append(copy.deepcopy(element));clean(wrapper)
        wrapped.append(E.tostring(wrapper[0],encoding='unicode'))
    return dict(service=client.pin(service,('http',)), profile=profile, configuration=configuration,
                node=node_token, ranges=ranges, config_xml=wrapped[0],node_xml=wrapped[1],options_xml=wrapped[2])


def capabilities(parent):
    node(parent,P,'Capabilities',EFlip='false',Reverse='false',GetCompatibleConfigurations='true',MoveStatus='true',StatusPosition='false')


def profile_config(plan, namespace, tag, use_count):
    root=E.fromstring(plan['config_xml']);root.tag='{'+namespace+'}'+tag
    count=root.find('{'+TT+'}UseCount')
    if count is not None:count.text=str(use_count)
    return root


class Controller:
    def __init__(self,camera,plan,client=None):
        from discovery import Client
        self.plan=plan
        if client is None:
            camera=dict(camera,ip=urlsplit(plan['service']).hostname)
            client=Client(camera);client.timeout=2;client.clock()
        self.client=client
        self.lock=threading.RLock();self.timer=None;self.generation=0;self.pending=False

    def call(self,name,inner=''):
        self.client.deadline=time.monotonic()+10
        return self.client.call(self.plan['service'],P,name,inner)

    def token(self):return '<t:ProfileToken>'+escape(self.plan['profile'])+'</t:ProfileToken>'

    def cancel(self):
        self.generation+=1
        if self.timer:self.timer.cancel();self.timer=None
        self.pending=False

    def stop(self):
        ok=False
        # Some SONOFF firmware acknowledges Stop but needs zero velocity too.
        for name,inner in [('ContinuousMove',self.token()+'<t:Velocity><tt:PanTilt x="0" y="0"/></t:Velocity><t:Timeout>PT1S</t:Timeout>'),('Stop',self.token()+'<t:PanTilt>true</t:PanTilt><t:Zoom>false</t:Zoom>')]:
            try:self.call(name,inner);ok=True
            except Exception:pass
        return ok

    def arm(self,seconds):
        self.cancel();self.pending=True;generation=self.generation
        def fire(attempt=0):
            with self.lock:
                if generation!=self.generation:return
                ok=self.stop()
                print('PTZ watchdog stop acknowledged.' if ok else 'PTZ watchdog stop failed.',flush=True)
                if ok:self.cancel()
                elif attempt<2:
                    self.timer=threading.Timer(.5,fire,args=(attempt+1,));self.timer.daemon=True;self.timer.start()
        self.timer=threading.Timer(seconds,fire);self.timer.daemon=True;self.timer.start()

    def close(self):
        with self.lock:
            if self.pending:self.stop()
            self.cancel()

    def response(self,op,profile_count):
        from discovery import child,value
        name=local(op.tag)
        reads={'GetServiceCapabilities','GetConfigurations','GetConfiguration','GetCompatibleConfigurations','GetConfigurationOptions','GetNodes','GetNode','GetStatus','GetPresets'}
        if name not in reads|{'ContinuousMove','Stop'}:return error('ActionNotSupported','Directional pan and tilt only')
        token=value(op,'ProfileToken')
        if name in {'GetCompatibleConfigurations','GetStatus','GetPresets','ContinuousMove','Stop'} and token not in [f'profile{i}' for i in range(profile_count)]:return error()
        for key,expected in [('PTZConfigurationToken',self.plan['configuration']),('ConfigurationToken',self.plan['configuration']),('NodeToken',self.plan['node'])]:
            if child(op,key) is not None and value(op,key)!=expected:return error()
        root,r=envelope(P,name)
        if name=='GetServiceCapabilities':capabilities(r)
        elif name in ('GetConfigurations','GetConfiguration','GetCompatibleConfigurations'):
            r.append(profile_config(self.plan,P,'PTZConfiguration',profile_count))
        elif name in ('GetNodes','GetNode'):r.append(E.fromstring(self.plan['node_xml']))
        elif name=='GetConfigurationOptions':r.append(E.fromstring(self.plan['options_xml']))
        elif name=='GetPresets':pass
        elif name=='GetStatus':
            with self.lock:result=self.call(name,self.token())
            wrapper=E.Element('wrapper');wrapper.append(result);clean(wrapper)
            for element in result:r.append(element)
        else:
            with self.lock:
                if name=='Stop':
                    if not self.stop():return error('Action','Camera did not acknowledge Stop')
                    self.cancel()
                else:
                    velocity=child(op,'Velocity');pan=child(velocity,'PanTilt');zoom=child(velocity,'Zoom')
                    if pan is None or pan.get('space',VELOCITY)!=VELOCITY:return error('InvalidArgVal','Pan/tilt velocity required')
                    if zoom is not None and float(zoom.get('x','nan'))!=0:return error('InvalidArgVal','Optical zoom unsupported')
                    coords={a:float(pan.get(a,'nan')) for a in ('x','y')}
                    for a,v in coords.items():
                        if not math.isfinite(v) or not self.plan['ranges'][a][0]<=v<=self.plan['ranges'][a][1]:return error('InvalidArgVal','Velocity outside supported range')
                    duration=value(op,'Timeout','PT1S')
                    match=re.fullmatch(r'PT(\d+(?:\.\d+)?)S',duration)
                    if not match:return error('InvalidArgVal','Invalid move timeout')
                    seconds=max(.1,min(2,float(match[1])))
                    inner=self.token()+f'<t:Velocity><tt:PanTilt x="{coords["x"]}" y="{coords["y"]}" space="{VELOCITY}"/></t:Velocity><t:Timeout>PT{seconds:g}S</t:Timeout>'
                    # Arm before the request: a lost reply must still trigger Stop.
                    self.arm(seconds)
                    self.call(name,inner)
        return 200,E.tostring(root,encoding='utf-8',xml_declaration=True)
