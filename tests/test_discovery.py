import base64
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import discovery as d
import generic_onvif as g
from generic_media import configuration
from manager import Store


def stream(source='lens1', width=1920, height=1080, codec='H264', uri='rtsp://192.168.1.10/main'):
    return dict(source=source, uri=uri, token='native', video=dict(width=width,height=height,codec=codec,fps=20,profile='Main',bitrate=0),audio=None)


def plan(records=None):
    views,warnings=d.select_views(records or [stream(),stream(width=640,height=360,uri='rtsp://192.168.1.10/low')])
    return dict(device=dict(Manufacturer='Test',Model='Unknown',FirmwareVersion='1'),views=views,warnings=warnings,profile_count=2)


class DiscoveryTests(unittest.TestCase):
    def test_group_sources_and_order_by_measured_resolution(self):
        p=plan([stream(width=640,height=360),stream(),stream(source='lens2',width=2304,height=1296)])
        self.assertEqual(len(p['views']),2)
        self.assertEqual(p['views'][0]['streams'][0]['video']['width'],1920)
        self.assertEqual(len(p['views'][1]['streams']),1)
    def test_missing_native_lq_never_fabricates_a_second_quality(self):
        p=plan([stream()])
        self.assertEqual(len(p['views'][0]['streams']),1)
        c=dict(discovery=p,username='camera',password='test-secret')
        cfg,commands=configuration(c,19000)
        self.assertEqual(commands,[])
        self.assertEqual(set(cfg['paths']),{'view0_hq'})
    def test_codec_match_preferred_and_only_lq_converted(self):
        p=plan([stream(codec='H265'),stream(width=640,height=360)])
        self.assertFalse(p['views'][0]['streams'][0]['convert_video'])
        self.assertTrue(p['views'][0]['streams'][1]['convert_video'])
        self.assertEqual(p['views'][0]['streams'][1]['output_codec'],'H265')
        p=plan([stream(codec='H265'),stream(width=640,height=360),stream(width=800,height=450,codec='H265')])
        self.assertEqual(p['views'][0]['streams'][1]['video']['width'],800)
        self.assertFalse(p['views'][0]['streams'][1]['convert_video'])
    def test_missing_source_and_unverified_source_fail_closed(self):
        for records in [[stream(source='')],[dict(source='lens',uri='rtsp://a')],[stream(),dict(source='lens2')]]:
            with self.assertRaises(ValueError):d.select_views(records)
    def test_report_never_exposes_uri_query_token_or_password(self):
        p=plan([stream(uri='rtsp://user:secret@192.168.1.10/a?secret-query')])
        public=json.dumps(d.public_report(p))
        for private in ('secret','rtsp://','native'):self.assertNotIn(private,public)
    def test_service_targets_pinned_to_camera_and_no_redirects(self):
        c=d.Client(dict(ip='192.168.1.10',address='camera.lan',onvif_port=80,username='a',password='b'))
        self.assertEqual(c.pin('rtsp://old:secret@0.0.0.0:8554/track?mode=1',('rtsp',)),'rtsp://192.168.1.10:8554/track?mode=1')
        for uri in ['http://192.168.1.20/','file:///etc/passwd','http://public.example/']:
            with self.assertRaises(ValueError):c.pin(uri,('http',))
    def test_runtime_paths_and_credential_isolation(self):
        c=dict(discovery=plan([stream(codec='H265'),stream(width=640,height=360)]),username='test-user',password='private-secret')
        cfg,cmd=configuration(c,19003)
        self.assertEqual(len(cmd),1)
        self.assertNotIn('private-secret',json.dumps(cmd))
        self.assertIn('private-secret',cfg['paths']['view0_hq']['source'])
        self.assertEqual(cfg['authInternalUsers'][1]['ips'],['127.0.0.1'])
        self.assertNotIn('input_view0_lq',json.dumps(cfg['authInternalUsers'][0]))
    def test_store_auto_requires_plan_preserves_identity_and_hides_streams(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp)
            c=dict(revision=0,name='Auto',model='auto',address='192.168.1.10',username='camera',password='private-secret')
            with self.assertRaises(ValueError):store.mutate('camera',c)
            c['_discovery']=plan();store.mutate('camera',c)
            saved=copy.deepcopy(store.data['cameras'][0]);self.assertEqual(len(saved['endpoints']),1)
            self.assertNotIn('rtsp://',json.dumps(store.public()))
            self.assertNotIn('private-secret',json.dumps(store.public()))
            c.update(id=saved['id'],revision=1,name='Renamed');c.pop('_discovery');store.mutate('camera',c)
            self.assertEqual(saved['endpoints'],store.data['cameras'][0]['endpoints'])
            c.update(revision=2,address='192.168.1.11')
            with self.assertRaises(ValueError):store.mutate('camera',c)


class FacadeTests(unittest.TestCase):
    def request(self,operation,inner='',ns=d.M2):
        return ET.fromstring(f'<s:Envelope xmlns:s="{d.S}" xmlns:m="{ns}"><s:Body><m:{operation}>{inner}</m:{operation}></s:Body></s:Envelope>')
    def test_profiles_and_uris_derived_from_plan(self):
        c=dict(name='Auto',discovery=plan())
        args=(c,0,'http://192.168.1.27:8103',8603,'02:00:00:00:00:03')
        status,raw=g.response(self.request('GetProfiles'),*args)
        self.assertEqual(status,200)
        xml=ET.fromstring(raw)
        self.assertEqual([x.text for x in xml.iter() if d.local(x.tag)=='Width'],['1920','640'])
        self.assertNotIn(b'AudioEncoder',raw)
        status,raw=g.response(self.request('GetStreamUri','<m:ProfileToken>profile1</m:ProfileToken><m:Protocol>RTSP</m:Protocol>'),*args)
        self.assertIn(b'rtsp://192.168.1.27:8603/view0_lq',raw)
        status,raw=g.response(self.request('GetVideoEncoderConfigurations'),*args)
        self.assertEqual(len(ET.fromstring(raw).findall('.//{'+d.M2+'}Configurations')),2)
        self.assertEqual(g.response(self.request('SetVideoEncoderConfiguration'),*args)[0],400)
    def test_wsse_checks_password_and_timestamp(self):
        c=dict(username='camera',password='test-secret')
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ');nonce=b'0123456789abcdefghij'
        digest=base64.b64encode(hashlib.sha1(nonce+stamp.encode()+c['password'].encode()).digest()).decode()
        xml=f'<Envelope xmlns:w="{d.WSSE}" xmlns:u="{d.WSU}"><w:UsernameToken><w:Username>camera</w:Username><w:Password Type="x#PasswordDigest">{digest}</w:Password><w:Nonce>{base64.b64encode(nonce).decode()}</w:Nonce><u:Created>{stamp}</u:Created></w:UsernameToken></Envelope>'
        self.assertTrue(g.authenticate(ET.fromstring(xml),c))
        self.assertFalse(g.authenticate(ET.fromstring(xml.replace(digest,'wrong')),c))
        self.assertFalse(g.authenticate(ET.fromstring(xml.replace(stamp,'2000-01-01T00:00:00Z')),c))

if __name__=='__main__':unittest.main()
