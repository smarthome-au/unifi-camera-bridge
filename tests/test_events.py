import copy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import events as e
from discovery import D, TT, S


class Client:
    def __init__(self):self.device='http://192.168.1.10/onvif/device_service';self.calls=[]
    def clock(self):pass
    def pin(self,uri,schemes):
        if not uri.startswith('http://192.168.1.10/'):raise ValueError('unexpected target')
        return uri
    def call(self,uri,ns,operation,inner='',**kwargs):
        self.calls.append((uri,operation,inner))
        if operation=='GetServices':
            return ET.fromstring(f'<r xmlns:t="{D}"><t:Service><t:Namespace>{e.E}</t:Namespace><t:XAddr>http://192.168.1.10/events</t:XAddr></t:Service></r>')
        if operation=='GetEventProperties':return ET.fromstring(f'<r xmlns:w="{e.T}"><w:TopicSet><VideoSource><MotionAlarm w:topic="true"/></VideoSource></w:TopicSet></r>')
        raise ValueError('unexpected call')


def service(**kwargs):return e.Service(dict(address='192.168.1.10',model='auto'), 'http://192.168.1.27:8102',client=Client(),start=False,**kwargs)
def op(name,body='',ns=e.E):return ET.fromstring(f'<t:{name} xmlns:t="{ns}" xmlns:w="{e.W}">{body}</t:{name}>')
def response(s,name,body='',path=e.PATH,ns=e.E):return s.response(op(name,body,ns),path)
def subscribe(s):
    status,raw=response(s,'CreatePullPointSubscription');assert status==200
    return ET.fromstring(raw).find('.//{'+e.A+'}Address').text.replace(s.public,'')
def notification(state,source='native',topic='tns1:VideoSource/MotionAlarm'):
    return ET.fromstring(f'<r xmlns:w="{e.W}" xmlns:tt="{TT}"><w:NotificationMessage><w:Topic>{topic}</w:Topic><w:Message><tt:Message UtcTime="2026-10-06T00:00:00Z" PropertyOperation="Changed"><tt:Source><tt:SimpleItem Name="Source" Value="{source}"/></tt:Source><tt:Data><tt:SimpleItem Name="State" Value="{state}"/></tt:Data></tt:Message></w:Message></w:NotificationMessage></r>')
def pull(s,path):return response(s,'PullMessages','<t:Timeout>PT0S</t:Timeout><t:MessageLimit>64</t:MessageLimit>',path)


class EventTests(unittest.TestCase):
    def test_native_motion_is_forwarded_and_other_detections_are_not_faked(self):
        s=service();path=subscribe(s)
        s.consume(notification('true',topic='tns1:RuleEngine/MyRuleDetector/PeopleDetect'))
        self.assertNotIn(b'NotificationMessage',pull(s,path)[1])
        s.consume(notification('true'));s.consume(notification('true'));s.consume(notification('false'))
        status,raw=pull(s,path);self.assertEqual(status,200)
        root=ET.fromstring(raw)
        self.assertEqual([x.get('Value') for x in root.iter() if x.get('Name')=='State'],['true','false'])
        self.assertEqual([x.get('Value') for x in root.iter() if x.get('Name')=='Source'],['source','source'])
        self.assertNotIn(b'192.168.1.10',raw)
    def test_multiple_source_isolation_and_camera_wide_duo_scope(self):
        s=service(view={'source':'lens1'},multi=True);path=subscribe(s)
        s.consume(notification('true',source='lens2'));self.assertNotIn(b'NotificationMessage',pull(s,path)[1])
        s.consume(notification('true',source='lens1'));self.assertIn(b'NotificationMessage',pull(s,path)[1])
        s.camera_wide=True;s.multi=False;s.consume(notification('false',source='lens1'))
        self.assertFalse(s.active)
    def test_aggregate_motion_does_not_end_until_all_reported_detectors_clear(self):
        s=service();s.consume(notification('true'));s.consume(notification('true',source='native2'))
        s.consume(notification('false'));self.assertTrue(s.active)
        s.consume(notification('false',source='native2'));self.assertFalse(s.active)
    def test_subscription_expiry_renewal_limit_and_unsubscribe(self):
        s=service();paths=[subscribe(s) for _ in range(4)]
        self.assertEqual(response(s,'CreatePullPointSubscription')[0],400)
        self.assertEqual(response(s,'Renew','<t:TerminationTime>PT90S</t:TerminationTime>',paths[0],e.W)[0],200)
        self.assertEqual(response(s,'Unsubscribe',path=paths[0],ns=e.W)[0],200)
        self.assertEqual(pull(s,paths[0])[0],400)
        s.subs[paths[1].split('/')[-1]]['expires']=time.monotonic()-1
        self.assertEqual(pull(s,paths[1])[0],400)
        subscribe(s)
    def test_queue_bounded_and_initial_state_only_after_real_notification(self):
        s=service();p=subscribe(s);self.assertNotIn(b'NotificationMessage',pull(s,p)[1])
        for i in range(300):s.consume(notification('true' if i%2 else 'false'))
        self.assertEqual(len(s.subs[p.split('/')[-1]]['queue']),256)
        p2=subscribe(s);self.assertIn(b'Initialized',pull(s,p2)[1])
    def test_pull_waits_for_notification_and_sync_uses_last_real_state(self):
        s=service();p=subscribe(s);result=[]
        t=threading.Thread(target=lambda:result.append(response(s,'PullMessages','<t:Timeout>PT2S</t:Timeout><t:MessageLimit>1</t:MessageLimit>',p)))
        t.start();time.sleep(.03);s.consume(notification('true'));t.join(timeout=1)
        self.assertFalse(t.is_alive());self.assertIn(b'Value="true"',result[0][1])
        response(s,'SetSynchronizationPoint',path=p);self.assertIn(b'Initialized',pull(s,p)[1])
    def test_invalid_and_unsupported_requests_fail_closed(self):
        s=service();p=subscribe(s)
        self.assertEqual(response(s,'Subscribe')[0],400)
        self.assertEqual(response(s,'PullMessages','<t:MessageLimit>10000</t:MessageLimit>',p)[0],400)
        self.assertEqual(response(s,'CreatePullPointSubscription','<t:Filter><w:MessageContent>arbitrary</w:MessageContent></t:Filter>')[0],400)
        with self.assertRaises(ValueError):response(s,'Renew','<t:TerminationTime>infinite</t:TerminationTime>',p,e.W)
        s.available=False;self.assertEqual(response(s,'GetEventProperties')[0],400)
    def test_device_advertisement_only_when_event_service_is_verified(self):
        s=service()
        for name,inner in [('GetServices',''),('GetCapabilities','<t:Capabilities/>')]:
            raw=f'<s:Envelope xmlns:s="{S}" xmlns:t="{D}"><s:Body><t:{name}Response>{inner}</t:{name}Response></s:Body></s:Envelope>'.encode()
            self.assertIn(e.PATH.encode(),e.advertise(raw,name,s))
            s.available=False;self.assertNotIn(e.PATH.encode(),e.advertise(raw,name,s));s.available=True
    def test_event_properties_have_schema_namespaces(self):
        status,raw=response(service(),'GetEventProperties');root=ET.fromstring(raw)
        self.assertIsNotNone(root.find('.//{'+e.W+'}FixedTopicSet'))
        self.assertIsNotNone(root.find('.//{'+e.T+'}TopicSet'))
        self.assertIn(b'xmlns:tns1=',raw)
        self.assertNotIn(b'PeopleDetect',raw)

if __name__=='__main__':unittest.main()

class EventHTTPTests(unittest.TestCase):
    def test_generic_endpoint_authentication_subscription_and_source_redaction(self):
        import http.client,http.server,os
        from unittest.mock import patch
        import generic_onvif as g
        from test_discovery import plan
        camera=dict(name='Test',username='camera',password='test-secret',ptz=False,discovery=plan())
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),g.Handler)
        server.camera=camera;server.index=0;server.controller=None;server.events=service()
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,name,body='',auth=True,ns=e.E):
            header='<w:UsernameToken xmlns:w="'+g.WSSE+'"><w:Username>camera</w:Username><w:Password Type="x#PasswordText">test-secret</w:Password></w:UsernameToken>' if auth else ''
            xml=f'<s:Envelope xmlns:s="{S}" xmlns:t="{ns}"><s:Header>{header}</s:Header><s:Body><t:{name}>{body}</t:{name}></s:Body></s:Envelope>'
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            conn.request('POST',path,xml,{'Content-Type':'application/soap+xml'})
            r=conn.getresponse();result=r.status,r.read();conn.close();return result
        try:
            with patch.dict(os.environ,{'BRIDGE_URL':server.events.public,'RTSP_PORT':'8602','BRIDGE_MAC':'02:00:00:00:00:01'}):
                self.assertEqual(request(e.PATH,'GetEventProperties',auth=False)[0],401)
                self.assertEqual(request(e.PATH,'GetEventProperties')[0],200)
                status,raw=request('/onvif/device_service','GetServices',ns=D)
                self.assertEqual(status,200);self.assertIn(e.PATH.encode(),raw)
                status,raw=request(e.PATH,'CreatePullPointSubscription');self.assertEqual(status,200)
                path=ET.fromstring(raw).find('.//{'+e.A+'}Address').text.replace(server.events.public,'')
                server.events.consume(notification('true'))
                self.assertEqual(request(path,'PullMessages',auth=False)[0],401)
                status,raw=request(path,'PullMessages','<t:Timeout>PT0S</t:Timeout><t:MessageLimit>1</t:MessageLimit>')
                self.assertEqual(status,200);self.assertIn(b'Value="true"',raw)
                self.assertNotIn(b'test-secret',raw);self.assertNotIn(b'192.168.1.10',raw)
                request(path,'Unsubscribe',ns=e.W)
        finally:server.shutdown();server.server_close();thread.join();server.events.close()

class RenewalTests(unittest.TestCase):
    def test_rejected_renew_opens_replacement_before_unsubscribing_old(self):
        from unittest.mock import patch
        import urllib.error
        s=service();s.plan={'service':'http://192.168.1.10/events'};calls=[]
        def call(uri,ns,name,inner='',header=''):
            calls.append(name)
            if name=='Renew':raise urllib.error.HTTPError(uri,400,'Bad request',{},None)
            if name=='CreatePullPointSubscription':return ET.fromstring(f'<r xmlns:w="{e.A}"><SubscriptionReference><w:Address>http://192.168.1.10/sub/new</w:Address></SubscriptionReference></r>')
            return ET.Element('r')
        with patch.object(s,'call',side_effect=call):
            uri,header=s.renew_native('http://192.168.1.10/sub/old','')
            self.assertEqual(calls,['Renew','CreatePullPointSubscription','Unsubscribe'])
            self.assertEqual(uri,'http://192.168.1.10/sub/new');self.assertFalse(s.renew_supported)
            calls.clear();s.renew_native(uri,header)
            self.assertEqual(calls,['CreatePullPointSubscription'])
    def test_auth_failure_does_not_disable_native_renewal(self):
        from unittest.mock import patch
        import urllib.error
        s=service()
        with patch.object(s,'call',side_effect=urllib.error.HTTPError('http://192.168.1.10/',401,'Auth',{},None)):
            with self.assertRaises(urllib.error.HTTPError):s.renew_native('http://192.168.1.10/sub','')
        self.assertTrue(s.renew_supported)

class FilterTests(unittest.TestCase):
    def test_state_filter_is_applied_without_evaluating_supplied_xpath(self):
        s=service()
        body=f'''<t:Filter><w:MessageContent Dialect="{e.ITEM_FILTER}">boolean(//tt:SimpleItem[@Name='State' and @Value='true'])</w:MessageContent></t:Filter>'''
        status,raw=response(s,'CreatePullPointSubscription',body);self.assertEqual(status,200)
        p=ET.fromstring(raw).find('.//{'+e.A+'}Address').text.replace(s.public,'')
        s.consume(notification('false'));s.consume(notification('true'));s.consume(notification('false'))
        _,raw=pull(s,p)
        self.assertEqual([x.get('Value') for x in ET.fromstring(raw).iter() if x.get('Name')=='State'],['true'])
        self.assertEqual(response(s,'CreatePullPointSubscription',body.replace('boolean(', 'document('))[0],400)
