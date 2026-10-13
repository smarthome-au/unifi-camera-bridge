"""Authenticated ONVIF motion facade with bounded queues and native PullPoint polling.

Only camera-emitted motion state is forwarded. No video analysis or synthetic
motion triggers. Multi-source cameras are filtered by their native source tokens;
CAM-C44HE's incomplete source model is explicitly treated as camera-wide.
"""
import collections
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from dual_camera import S, TT, D, node, envelope, error, local

E = 'http://www.onvif.org/ver10/events/wsdl'
W = 'http://docs.oasis-open.org/wsn/b-2'
TOPICS = 'http://www.onvif.org/ver10/topics'
T = 'http://docs.oasis-open.org/wsn/t-1'
A = 'http://www.w3.org/2005/08/addressing'
DIALECT = 'http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet'
ITEM_FILTER = 'http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter'
PATH = '/onvif/events_service'
PREFIX = '/onvif/events/sub/'
# CAM-S3W returns empty PT5S polls after ~10 seconds. Leave headroom for
# that firmware behaviour without slowing down other event operations.
PULL_HTTP_TIMEOUT = 15
OPS = {'GetServiceCapabilities', 'GetEventProperties', 'CreatePullPointSubscription',
       'PullMessages', 'Renew', 'Unsubscribe', 'SetSynchronizationPoint'}
for prefix, uri in [('tev', E), ('wsnt', W), ('wstop', T), ('wsa', A)]:
    ET.register_namespace(prefix, uri)


def now(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def motion_topic(text):
    parts = [p.split(':')[-1] for p in (text or '').split('/')]
    return ('MotionAlarm' in parts or 'CellMotionDetector' in parts or 'MotionRegionDetector' in parts)


def discover(client):
    from discovery import value
    uri = None
    try:
        services = client.call(client.device, D, 'GetServices', '<t:IncludeCapability>false</t:IncludeCapability>')
        uri = next((value(s, 'XAddr') for s in services if value(s, 'Namespace') == E), None)
    except Exception: pass
    if not uri:
        try:
            caps = client.call(client.device,D,'GetCapabilities','<t:Category>Events</t:Category>')
            uri = next((value(x,'XAddr') for x in caps.iter() if local(x.tag)=='Events'),None)
        except Exception: return None
    if not uri: return None
    uri = client.pin(uri, ('http',))
    response = client.call(uri, E, 'GetEventProperties')
    topics = next((x for x in response.iter() if local(x.tag) == 'TopicSet'), None)
    paths = []
    def visit(element, path=''):
        name = local(element.tag)
        path = path+'/'+name if path else name
        if element.get('{'+T+'}topic') in ('true', '1') and motion_topic(path): paths.append(path)
        for child in element: visit(child, path)
    if topics is not None:
        for child in topics: visit(child)
    return {'service': uri, 'motion_topics': paths} if paths else None


def duration(text, default, maximum):
    if not text: return default
    match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?', text)
    if not match or not any(match.groups()): raise ValueError('Invalid duration')
    seconds = sum(float(v or 0)*m for v,m in zip(match.groups(),(3600,60,1)))
    return max(0, min(maximum, seconds))


def capabilities(parent):
    return node(parent, E, 'Capabilities', WSSubscriptionPolicySupport='false',
                WSPullPointSupport='true', WSPausableSubscriptionManagerInterfaceSupport='false',
                MaxPullPoints='4', PersistentNotificationStorage='false')


def advertise(data, operation, service):
    """Replace upstream event advertisements with the local verified facade."""
    if operation not in ('GetServices', 'GetCapabilities'): return data
    root = ET.fromstring(data)
    for parent in root.iter():
        for c in list(parent):
            if local(c.tag) == 'Events' or (local(c.tag) == 'Service' and any(local(x.tag)=='Namespace' and x.text==E for x in c)):
                parent.remove(c)
    if service and service.available:
        if operation == 'GetServices':
            r = root.find('.//{'+D+'}GetServicesResponse')
            if r is not None:
                s = node(r,D,'Service');node(s,D,'Namespace',E);node(s,D,'XAddr',service.public+PATH)
                cap=node(s,D,'Capabilities');capabilities(cap)
                ver=node(s,D,'Version');node(ver,TT,'Major',2);node(ver,TT,'Minor',6)
        else:
            r=root.find('.//{'+D+'}Capabilities')
            if r is not None:
                e=node(r,TT,'Events');node(e,TT,'XAddr',service.public+PATH)
                for key,val in [('WSSubscriptionPolicySupport','false'),('WSPullPointSupport','true'),('WSPausableSubscriptionManagerInterfaceSupport','false')]:node(e,TT,key,val)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


class Service:
    def __init__(self, camera, public, view=None, multi=False, source_token='source', config_token='source_config', client=None, start=True, status_file=None):
        from discovery import Client
        self.camera=camera;self.public=public;self.source_token=source_token;self.config_token=config_token
        self.client=client or Client(dict(camera,ip=camera.get('ip') or os.environ.get('CAMERA_IP') or camera['address']))
        self.client.timeout=9
        self.condition=threading.Condition();self.done=threading.Event();self.subs={};self.states={};self.active=None
        self.plan=None;self.available=False;self.renewals=0;self.replacements=0;self.renew_supported=True;self.reconnects=0;self.received=0;self.transitions=0;self.last_event=None;self.last_motion=None
        self.status_file=status_file;self.link='checking';self.thread=None;self.operation=None
        model=camera.get('discovery',{}).get('device',{}).get('Model',camera.get('model','')).lower()
        self.camera_wide=model in ('cam-c44he','camc44he')
        self.multi=multi and not self.camera_wide
        view=view or {}
        self.source_ids={view.get('source')} | {s.get(k) for s in view.get('streams',[]) for k in ('source','source_config')}
        self.source_ids.discard(None);self.source_ids.discard('')
        try:
            self.client.clock();self.plan=discover(self.client);self.available=bool(self.plan)
            self.link='connecting' if self.available else 'unsupported'
        except Exception:self.link='unavailable'
        self.write_status()
        if start:
            self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def call(self,uri,ns,name,inner='',header=''):
        self.operation=name
        self.client.deadline=time.monotonic()+20
        previous_timeout=self.client.timeout
        if name=='PullMessages':self.client.timeout=PULL_HTTP_TIMEOUT
        try:return self.client.call(uri,ns,name,inner,header_xml=header)
        finally:self.client.timeout=previous_timeout

    def write_status(self):
        if not self.status_file:return
        data={'supported':self.available,'connection':self.link,'scope':'camera-wide' if self.camera_wide else ('lens' if self.multi else 'camera'),
              'renewals':self.renewals,'subscription_replacements':self.replacements,'reconnects':self.reconnects,'notifications':self.received,'transitions':self.transitions,'last_event':self.last_event,'last_motion':self.last_motion}
        path=Path(self.status_file);tmp=path.with_suffix('.tmp')
        try:
            fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(data,f)
            os.replace(tmp,path)
        except OSError:pass

    def close(self):
        self.done.set()
        with self.condition:self.condition.notify_all()
        if self.thread:self.thread.join(timeout=12)

    def consume(self,response):
        from discovery import child
        for n in response.iter():
            if local(n.tag)!='NotificationMessage':continue
            topic=next((x.text for x in n if local(x.tag)=='Topic'),None)
            if not motion_topic(topic):continue
            message=next((x for x in n.iter() if x.tag=='{'+TT+'}Message'),None)
            if message is None:continue
            source=child(message,'Source');data=child(message,'Data')
            ids={x.get('Value') for x in source or [] if local(x.tag)=='SimpleItem' and x.get('Name') in ('Source','VideoSourceToken','VideoSourceConfigurationToken')}
            if self.multi and not ids.intersection(self.source_ids):continue
            state=next((x.get('Value','').lower() for x in data or [] if local(x.tag)=='SimpleItem' and x.get('Name') in ('State','IsMotion','LogicalState')),None)
            if state not in ('true','false','1','0'):continue
            operation=message.get('PropertyOperation','Changed')
            if operation=='Deleted':continue
            key=('/'.join(p.split(':')[-1] for p in topic.split('/')),tuple(sorted(ids)))
            with self.condition:
                self.received+=1;self.states[key]=state in ('true','1')
                combined=any(self.states.values())
                # Multiple ONVIF topics can describe the same motion detector.
                if self.active is None or combined!=self.active:
                    self.active=combined;self.transitions+=1;stamp=now();self.last_event=stamp
                    if combined:self.last_motion=stamp
                    self.publish(combined,stamp,'Initialized' if operation=='Initialized' else 'Changed')
                    print(json.dumps({'event':'motion','active':combined,'scope':'camera-wide' if self.camera_wide else 'camera'}),flush=True)
            self.write_status()

    def publish(self,state,stamp,operation):
        self.expire()
        for sub in self.subs.values():
            if sub.get('state_filter') is None or sub['state_filter']==state:sub['queue'].append((state,stamp,operation))
        self.condition.notify_all()

    def expire(self):
        current=time.monotonic()
        self.subs={k:s for k,s in self.subs.items() if s['expires']>current}

    def subscribe_native(self):
        from discovery import child,value
        r=self.call(self.plan['service'],E,'CreatePullPointSubscription','<t:InitialTerminationTime>PT60S</t:InitialTerminationTime>')
        ref=child(r,'SubscriptionReference')
        uri=self.client.pin(value(ref,'Address'),('http',));header=''
        params=child(ref,'ReferenceParameters')
        if params is not None:
            for p in params:
                p=copy.deepcopy(p);p.set('{'+A+'}IsReferenceParameter','true');header+=ET.tostring(p,encoding='unicode')
        return uri,header

    def renew_native(self,uri,header):
        if self.renew_supported:
            try:
                self.call(uri,W,'Renew','<t:TerminationTime>PT60S</t:TerminationTime>',header)
                self.renewals+=1;return uri,header
            except urllib.error.HTTPError as exc:
                # CAM-C44HE returns ActionFailed (HTTP 400) for Renew. Keep the
                # old subscription until its replacement exists; auth/transport
                # errors instead go through the ordinary reconnect path.
                if exc.code not in (400,405,501):raise
                self.renew_supported=False
        replacement=self.subscribe_native();self.replacements+=1
        if replacement!=(uri,header):
            try:self.call(uri,W,'Unsubscribe',header=header)
            except Exception:pass
        return replacement

    def run(self):
        from discovery import child,value
        while not self.done.is_set():
            uri=None;header=''
            try:
                if not self.plan:
                    self.client.deadline=time.monotonic()+30;self.client.clock();self.plan=discover(self.client)
                    self.available=bool(self.plan)
                    if not self.plan:
                        self.link='unsupported';self.write_status();self.done.wait(30);continue
                uri,header=self.subscribe_native()
                with self.condition:self.states.clear()
                self.link='connecting';self.write_status();renew=time.monotonic()+25
                while not self.done.is_set():
                    if time.monotonic()>=renew:
                        uri,header=self.renew_native(uri,header);self.write_status();renew=time.monotonic()+25
                    started=time.monotonic()
                    r=self.call(uri,E,'PullMessages','<t:Timeout>PT5S</t:Timeout><t:MessageLimit>64</t:MessageLimit>',header)
                    self.consume(r)
                    if self.link!='connected':self.link='connected';self.write_status()
                    self.done.wait(max(0,.2-(time.monotonic()-started)))
            except Exception as exc:
                self.reconnects+=1
                record={'event_connection':'retry','error':type(exc).__name__,'operation':self.operation}
                if isinstance(exc,urllib.error.HTTPError):record['status']=exc.code
                print(json.dumps(record),flush=True)
                self.link='reconnecting';self.write_status()
            finally:
                if uri:
                    try:self.call(uri,W,'Unsubscribe',header=header)
                    except Exception:pass
            self.done.wait(3)

    def response(self,op,path):
        from discovery import value
        name=local(op.tag);ns=op.tag[1:].split('}')[0]
        if name not in OPS or ns not in (E,W):return error('ActionNotSupported','Unsupported event operation')
        if not self.available:return error('ActionNotSupported','Camera motion events unavailable')
        root,r=envelope(W if name in ('Renew','Unsubscribe') else E,name)
        # QName-valued topic text and Type attributes need explicit declarations.
        root.set('xmlns:tns1',TOPICS);root.set('xmlns:xsd','http://www.w3.org/2001/XMLSchema')
        if name=='GetServiceCapabilities':capabilities(r)
        elif name=='GetEventProperties':
            node(r,E,'TopicNamespaceLocation','http://www.onvif.org/onvif/ver10/topics/topicns.xml')
            node(r,W,'FixedTopicSet','true')
            topics=node(r,T,'TopicSet');video=node(topics,TOPICS,'VideoSource');motion=node(video,TOPICS,'MotionAlarm',**{'{'+T+'}topic':'true'})
            desc=node(motion,TT,'MessageDescription',IsProperty='true');source=node(desc,TT,'Source')
            node(source,TT,'SimpleItemDescription',Name='Source',Type='tt:ReferenceToken')
            data=node(desc,TT,'Data');node(data,TT,'SimpleItemDescription',Name='State',Type='xsd:boolean')
            node(r,W,'TopicExpressionDialect',DIALECT)
            node(r,W,'TopicExpressionDialect','http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete')
            node(r,E,'MessageContentFilterDialect',ITEM_FILTER)
            node(r,E,'MessageContentSchemaLocation','http://www.onvif.org/onvif/ver10/schema/onvif.xsd')
        elif name=='CreatePullPointSubscription':
            state_filter=None
            for filt in op:
                if local(filt.tag) != 'Filter': continue
                for expression in filt:
                    text=(expression.text or '').strip()
                    if local(expression.tag)=='TopicExpression':
                        if text.split(':')[-1] != 'VideoSource/MotionAlarm' or expression.get('Dialect') not in (DIALECT,'http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete'):
                            return error('InvalidArgVal','Unsupported topic filter')
                    elif local(expression.tag)=='MessageContent' and expression.get('Dialect')==ITEM_FILTER:
                        # A bounded ItemFilter subset for the sole exposed boolean
                        # State property. Never evaluate supplied code or XPath.
                        normalized=text.replace('"', "'").replace(' ', '')
                        match=re.fullmatch(r"boolean\(//(?:[A-Za-z_][\w.-]*:)?SimpleItem\[@Name='State'and@Value='(true|false)'\]\)",normalized)
                        if not match:return error('InvalidArgVal','Unsupported state filter')
                        state_filter=match[1]=='true'
                    else:return error('InvalidArgVal','Unsupported event filter')
            seconds=max(10,duration(value(op,'InitialTerminationTime'),60,300))
            with self.condition:
                self.expire()
                if len(self.subs)>=4:return error('Action','Subscription limit reached')
                ident=secrets.token_urlsafe(24);sub={'expires':time.monotonic()+seconds,'queue':collections.deque(maxlen=256),'pulling':False,'state_filter':state_filter}
                if self.active is not None and (sub.get('state_filter') is None or sub['state_filter']==self.active):sub['queue'].append((self.active,now(),'Initialized'))
                self.subs[ident]=sub
            ref=node(r,E,'SubscriptionReference');node(ref,A,'Address',self.public+PREFIX+ident)
            node(r,W,'CurrentTime',now());node(r,W,'TerminationTime',(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=seconds)).isoformat().replace('+00:00','Z'))
        else:
            ident=path[len(PREFIX):] if path.startswith(PREFIX) else ''
            with self.condition:
                self.expire();sub=self.subs.get(ident)
                if sub is None:return error('InvalidArgVal','Unknown or expired subscription')
                if name=='Unsubscribe':del self.subs[ident];self.condition.notify_all()
                elif name=='Renew':
                    seconds=max(10,duration(value(op,'TerminationTime'),60,300));sub['expires']=time.monotonic()+seconds
                    node(r,W,'TerminationTime',(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=seconds)).isoformat().replace('+00:00','Z'));node(r,W,'CurrentTime',now())
                elif name=='SetSynchronizationPoint':
                    if self.active is not None and (sub.get('state_filter') is None or sub['state_filter']==self.active):sub['queue'].append((self.active,now(),'Initialized'));self.condition.notify_all()
                elif name=='PullMessages':
                    seconds=duration(value(op,'Timeout'),5,30);limit=int(value(op,'MessageLimit','64'))
                    if not 1<=limit<=256:return error('InvalidArgVal','Invalid message limit')
                    if sub['pulling']:return error('Action','Only one concurrent pull per subscription')
                    sub['pulling']=True
                    try:
                        end=min(time.monotonic()+seconds,sub['expires'])
                        while not sub['queue'] and ident in self.subs and not self.done.is_set() and time.monotonic()<end:self.condition.wait(end-time.monotonic())
                        if ident not in self.subs or sub['expires']<=time.monotonic():return error('InvalidArgVal','Unknown or expired subscription')
                        node(r,E,'CurrentTime',now());node(r,E,'TerminationTime',(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=max(0,sub['expires']-time.monotonic()))).isoformat().replace('+00:00','Z'))
                        for _ in range(min(limit,len(sub['queue']))):
                            state,stamp,operation=sub['queue'].popleft()
                            n=node(r,W,'NotificationMessage');node(n,W,'Topic','tns1:VideoSource/MotionAlarm',Dialect=DIALECT)
                            m=node(node(n,W,'Message'),TT,'Message',UtcTime=stamp,PropertyOperation=operation)
                            source=node(m,TT,'Source');node(source,TT,'SimpleItem',Name='Source',Value=self.source_token)
                            data=node(m,TT,'Data');node(data,TT,'SimpleItem',Name='State',Value=str(state).lower())
                    finally:sub['pulling']=False
        return 200,ET.tostring(root,encoding='utf-8',xml_declaration=True)
