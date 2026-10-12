"""Bounded local diagnostics built from known fields, never raw worker output."""
from collections import deque
import datetime
import json
import os
from pathlib import Path
import threading

OPERATIONS = set('GetServices GetCapabilities GetServiceCapabilities GetSystemDateAndTime GetProfiles GetProfile GetStreamUri GetSnapshotUri GetDeviceInformation GetNetworkInterfaces GetScopes GetHostname GetDNS GetNTP GetNetworkProtocols GetNetworkDefaultGateway GetEventProperties CreatePullPointSubscription PullMessages Renew Unsubscribe SetSynchronizationPoint GetStatus GetNodes GetNode GetConfigurations GetConfiguration GetConfigurationOptions GetPresets ContinuousMove RelativeMove AbsoluteMove Stop GotoPreset GotoHomePosition SetHomePosition GetVideoSources GetVideoSourceConfigurations GetVideoSourceConfiguration GetVideoEncoderConfigurations GetVideoEncoderConfiguration GetVideoEncoderConfigurationOptions GetAudioSources GetAudioOutputs GetAudioSourceConfigurations GetAudioSourceConfiguration GetAudioEncoderConfigurations GetAudioEncoderConfiguration GetAudioEncoderConfigurationOptions GetCompatibleVideoEncoderConfigurations GetCompatibleAudioEncoderConfigurations GetCompatibleVideoSourceConfigurations GetCompatibleAudioSourceConfigurations'.split())
WORKER_MESSAGES = {
 'media_exit': ('error','Video relay stopped; restarting.'),
 'encoder_exit': ('warning','Stream converter stopped; restarting.'),
 'encoder_started': ('info','Stream converter started.'),
 'media_start_failed': ('error','Video relay could not start. Check camera settings and connection.'),
}

class Journal:
    def __init__(self, path=None, capacity=1000):
        self.path=Path(path) if path else None
        self.rows=deque(maxlen=capacity);self.lock=threading.RLock();self.next_id=1;self.dirty=False
        if self.path:
            try:
                if self.path.stat().st_size > 1024*1024: raise ValueError()
                rows=json.loads(self.path.read_text())
                for r in rows[-capacity:]:
                    if set(r)!=set(('id','time','level','camera','source','message','count')):continue
                    if r['level'] not in ('info','warning','error') or type(r['id']) is not int or type(r['count']) is not int:continue
                    if not all(isinstance(r[k],str) and len(r[k])<=240 for k in ('time','camera','source','message')):continue
                    self.rows.append(r);self.next_id=max(self.next_id,r['id']+1)
            except (OSError,ValueError,TypeError,KeyError):pass

    def add(self, level, message, key='bridge'):
        camera, _, source=key.partition('/')
        if camera=='bridge':camera=''
        source=source or 'manager'
        with self.lock:
            stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
            # Consecutive identical failures retain a count instead of flooding the view.
            if self.rows and all(self.rows[-1][k]==v for k,v in [('level',level),('camera',camera),('source',source),('message',message)]):
                self.rows[-1]['count']+=1;self.rows[-1]['time']=stamp
            else:
                self.rows.append(dict(id=self.next_id,time=stamp,level=level,camera=camera,source=source,message=message,count=1));self.next_id+=1
            self.dirty=True

    def read(self, camera='', level='all', limit=250):
        with self.lock:
            rows=[dict(r) for r in reversed(self.rows) if (not camera or r['camera']==camera) and (level=='all' or (level=='issues' and r['level'] in ('warning','error')) or level==r['level'])]
            return {'entries':rows[:limit],'matching':len(rows),'retained':len(self.rows),'capacity':self.rows.maxlen}

    def flush(self):
        with self.lock:
            if not self.path or not self.dirty:return
            tmp=self.path.with_suffix('.tmp')
            try:
                fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as f:json.dump(list(self.rows),f)
                os.replace(tmp,self.path);self.dirty=False
            except OSError:pass  # Diagnostics must not interrupt camera workers.

    def ingest(self, key, line):
        try:d=json.loads(line)
        except (ValueError,TypeError):
            if line.startswith('LQ RTSP server exited;'):self.add('error',WORKER_MESSAGES['media_exit'][1],key)
            elif line.startswith('LQ transcoder startup failed;') or line.startswith('Automatic media bridge could not start;'):self.add('error',WORKER_MESSAGES['media_start_failed'][1],key)
            elif line.startswith('LQ encoder ') and ' exited (' in line:self.add('warning',WORKER_MESSAGES['encoder_exit'][1],key)
            return
        if not isinstance(d,dict):return
        event=d.get('worker_event')
        if isinstance(event,str) and event in WORKER_MESSAGES:
            level,msg=WORKER_MESSAGES[event];self.add(level,msg,key);return
        if d.get('event_connection')=='retry':self.add('warning','Motion event connection lost; retrying.',key);return
        if d.get('rtsp')=='connection_error':self.add('warning','Video connection interrupted. The camera or viewer closed the connection unexpectedly.',key);return
        if 'snapshot_status' in d:
            status=d['snapshot_status']
            if type(status) is int and 400<=status<=599:self.add('warning','Snapshot unavailable. Check that the camera video is reachable.',key)
            return
        op=d.get('operation',d.get('ptz_operation'));status=d.get('status')
        if isinstance(op,str) and op in OPERATIONS and type(status) is int and 400<=status<=599:
            detail=' Camera authentication was rejected.' if status==401 else ''
            self.add('error' if status>=500 else 'warning',f'{op} returned HTTP {status}.{detail}',key)

    def consume(self, key, pipe):
        try:
            while True:
                data=pipe.readline(8193)
                if not data:break
                if len(data)>8192:
                    while data and not data.endswith(b'\n'):data=pipe.readline(8193)
                    continue
                self.ingest(key,data.decode('utf-8',errors='replace').strip())
        finally:pipe.close()
