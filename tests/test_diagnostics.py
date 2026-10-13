import io,json,tempfile,threading,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from diagnostics import Journal

class DiagnosticsTests(unittest.TestCase):
 def test_raw_secrets_urls_and_unknown_fields_never_retained(self):
  j=Journal()
  for record in [dict(operation='GetProfiles',status=401,password='SUPERSECRET',url='rtsp://user:SUPERSECRET@camera/feed'),dict(event_connection='retry',error='SUPERSECRET'),dict(snapshot_status=503,body='SUPERSECRET')]:j.ingest('camera/fixed',json.dumps(record))
  for raw in ['Traceback SUPERSECRET','rtsp://user:SUPERSECRET@camera/path',json.dumps({'operation':'SUPERSECRET','status':500}),json.dumps({'snapshot_status':'SUPERSECRET'}),json.dumps({'worker_event':['SUPERSECRET']}),'["SUPERSECRET"]']:
   j.ingest('camera/fixed',raw)
  self.assertEqual(len(j.rows),3)
  self.assertNotIn('SUPERSECRET',json.dumps(j.read()));self.assertNotIn('rtsp://',json.dumps(j.read()))
 def test_buffer_filters_repeat_count_and_persistence(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'logs.json';j=Journal(p,3)
   for i in range(4):j.add('info',str(i),'a/media')
   j.add('warning','Video unavailable.','b/fixed');j.add('warning','Video unavailable.','b/fixed')
   self.assertEqual(len(j.rows),3);self.assertEqual(j.read('b','issues')['entries'][0]['count'],2)
   self.assertEqual(j.read('b','error')['entries'],[])
   j.flush();self.assertEqual(p.stat().st_mode&0o777,0o600)
   restored=Journal(p,3);self.assertEqual(restored.read(),j.read())
   restored.add('error','Failure.');self.assertGreater(restored.rows[-1]['id'],j.rows[-1]['id'])
 def test_oversized_lines_discarded_and_reader_closes(self):
  j=Journal();pipe=io.BytesIO(b'x'*20000+b'\n'+b'{"snapshot_status":503}\n')
  j.consume('a/view0',pipe);self.assertTrue(pipe.closed);self.assertEqual(len(j.rows),1)
 def test_routine_polling_and_motion_do_not_fill_buffer(self):
  j=Journal()
  for r in [dict(operation='PullMessages',status=200),dict(event='motion',active=True),dict(snapshot_status=200),dict(rtsp='open')]:j.ingest('a/fixed',json.dumps(r))
  self.assertEqual(len(j.rows),0)
 def test_event_failure_details_are_whitelisted_and_repeat_failures_collapse(self):
  j=Journal()
  for _ in range(3):j.ingest('a/fixed',json.dumps(dict(event_connection='retry',error='TimeoutError',operation='PullMessages',message='SUPERSECRET')))
  row=j.read()['entries'][0]
  self.assertEqual(len(j.rows),1);self.assertEqual(row['count'],3)
  self.assertIn('PullMessages: Camera response timed out.',row['message'])
  j.ingest('a/fixed',json.dumps(dict(event_connection='retry',error='HTTPError',status=401,operation='Renew')))
  self.assertIn('Renew: Camera returned HTTP 401.',j.read()['entries'][0]['message'])
  for error in ['SUPERSECRET', ['SUPERSECRET']]:
   j.ingest('a/fixed',json.dumps(dict(event_connection='retry',error=error,status='SUPERSECRET',operation='SUPERSECRET')))
  self.assertNotIn('SUPERSECRET',json.dumps(j.read()))
 def test_worker_diagnostics_and_concurrent_writes(self):
  j=Journal();j.ingest('a/media','{"worker_event":"encoder_exit"}')
  self.assertEqual(j.rows[0]['level'],'warning')
  threads=[threading.Thread(target=lambda:j.add('error','Failure.','b/media')) for _ in range(20)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertEqual(j.rows[-1]['count'],20)

if __name__=='__main__':unittest.main()
