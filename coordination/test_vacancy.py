from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3,tempfile,threading,unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from coordination.errors import ApiError
from coordination.vacancy import VacancyLedger,EVENT,MAP_VERSION,ASSIGNMENT_MS
from coordination.server import Ledger,Server


class VacancyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.db';self.now=1000000;self.seq=0
        self.ledger=VacancyLedger(self.path,lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def payload(self,**kw):
        self.seq+=1;return {'eventId':EVENT,'mapVersion':MAP_VERSION,'requestId':f'req-{self.seq}',**kw}
    def state(self):return self.ledger.state(EVENT)
    def space(self,sid='A-01'):return next(s for s in self.state()['spaces'] if s['id']==sid)
    def source(self,sid):return next(s for s in self.state()['sources'] if s['id']==sid)
    def share(self,sid='CAR-1',enabled=True):return self.ledger.consent(sid,self.payload(revision=self.source(sid)['revision'],enabled=enabled))
    def observation(self,sid='A-01',source='STAFF-1',state='vacant',**kw):
        return self.payload(spaceId=sid,sourceId=source,sequence=self.source(source)['last_sequence']+1,
            observedAtMillis=self.now,ttlMillis=60000,confidence=.95,state=state,view='field')|kw
    def observe(self,*args,**kw):return self.ledger.observe(self.observation(*args,**kw))
    def assign(self,space='A-01',arrival='IN-1',**kw):return self.ledger.assign(self.payload(spaceId=space,arrivalId=arrival,**kw))
    def action(self,aid,action):return self.ledger.transition(aid,action,self.payload())

    def test_initial_map_is_all_unknown_not_false_empty(self):
        s=self.state();self.assertEqual(s['counts']['total'],18);self.assertEqual(s['counts']['unknown'],18);self.assertEqual(s['counts']['available'],0)
        self.assertEqual(len(s['zones']),3);self.assertTrue(all(s['reason']=='unobserved' for s in s['spaces']))

    def test_one_fresh_report_makes_one_space_available(self):
        s=self.observe();self.assertEqual(s['counts']['available'],1);self.assertTrue(self.space()['available'])
        self.assertEqual(self.space()['reports'][0]['source_id'],'STAFF-1')

    def test_multiple_vehicle_views_of_same_canonical_space_do_not_inflate_count(self):
        self.share();self.share('CAR-2');self.observe(source='CAR-1',view='left');self.observe(source='CAR-2',view='right')
        self.assertEqual(self.state()['counts']['available'],1);self.assertEqual(self.space()['freshSourceCount'],2)

    def test_conflicting_vacant_and_occupied_is_unknown_not_majority_vote(self):
        self.share();self.share('CAR-2');self.observe();self.observe(source='CAR-1');self.observe(source='CAR-2',state='occupied')
        self.assertEqual(self.space()['reason'],'conflicting_reports');self.assertFalse(self.space()['available'])
        with self.assertRaises(ApiError):self.assign()

    def test_blocked_report_dominates_empty_and_occupied(self):
        self.share();self.observe();self.observe(source='CAR-1',state='blocked')
        self.assertEqual(self.space()['occupancy'],'blocked');self.assertFalse(self.space()['available'])

    def test_unknown_latest_report_does_not_fall_back_to_same_sources_old_empty(self):
        self.observe();self.observe(state='unknown');self.assertEqual(self.space()['reason'],'occluded');self.assertFalse(self.space()['available'])

    def test_low_confidence_never_authorizes_dispatch(self):
        self.observe(confidence=.79);self.assertEqual(self.space()['reason'],'low_confidence')
        with self.assertRaises(ApiError):self.assign()
        self.observe(confidence=.8);self.assertTrue(self.space()['available'])

    def test_expiry_is_based_on_observed_time_not_receipt_time(self):
        self.observe(observedAtMillis=self.now-2000,ttlMillis=1000)
        self.assertEqual(self.space()['reason'],'stale');self.assertEqual(self.state()['counts']['available'],0)

    def test_exact_observation_expiry_changes_counts_and_prevents_assignment(self):
        self.observe(ttlMillis=1000);self.now+=999;self.assertTrue(self.space()['available'])
        self.now+=1;self.assertFalse(self.space()['available']);self.assertEqual(self.space()['reason'],'stale')
        with self.assertRaises(ApiError):self.assign()

    def test_one_conflicting_report_expiring_can_restore_other_fresh_evidence(self):
        self.share();self.observe(ttlMillis=10000);self.observe(source='CAR-1',state='occupied',ttlMillis=1000)
        self.assertFalse(self.space()['available']);self.now+=1000;self.assertTrue(self.space()['available'])

    def test_clock_rollback_is_unknown_instead_of_fresh(self):
        self.observe();self.now-=1;self.assertEqual(self.space()['reason'],'clock_mismatch');self.assertFalse(self.space()['available'])

    def test_sources_require_explicit_information_sharing(self):
        with self.assertRaises(ApiError) as e:self.observe(source='CAR-1')
        self.assertEqual(e.exception.code,'vacancy_sharing_disabled');self.share();self.observe(source='CAR-1');self.assertTrue(self.space()['available'])

    def test_revocation_immediately_excludes_old_reports_and_reenable_requires_new_ones(self):
        self.share();self.observe(source='CAR-1');self.share(enabled=False)
        self.assertFalse(self.space()['available']);self.share();self.assertFalse(self.space()['available'])
        self.observe(source='CAR-1');self.assertTrue(self.space()['available'])

    def test_old_sequence_and_delayed_older_timestamp_cannot_replace_new_evidence(self):
        self.observe(state='occupied');before=self.source('STAFF-1')
        for kw in ({'sequence':before['last_sequence']},{'observedAtMillis':self.now-1}):
            with self.assertRaises(ApiError):self.ledger.observe(self.observation(**kw))
        self.assertEqual(self.space()['occupancy'],'occupied')

    def test_same_observation_uuid_replays_without_extending_its_ttl(self):
        p=self.observation(ttlMillis=1000);first=self.ledger.observe(p);self.now+=1000;again=self.ledger.observe(p)
        self.assertEqual(first['actionResult']['id'],again['actionResult']['id']);self.assertFalse(self.space()['available'])
        self.assertEqual(len(self.state()['events']),1)

    def test_invalid_geometry_map_source_state_confidence_and_times_are_rejected(self):
        for changes in ({'mapVersion':2},{'mapVersion':True},{'mapVersion':1.0},{'eventId':'another'},
            {'confidence':float('nan')},{'confidence':True},{'confidence':1.1},{'ttlMillis':0},{'ttlMillis':120001},
            {'sequence':True},{'sequence':0},{'observedAtMillis':self.now+1},{'state':'free'},{'view':'anywhere'},
            {'spaceId':'unknown'},{'sourceId':'UNKNOWN'}):
            with self.subTest(changes=changes),self.assertRaises(ApiError):self.ledger.observe(self.observation()|changes)
        self.assertEqual(self.state()['counts']['available'],0)

    def test_dispatch_uses_one_space_without_changing_the_observed_occupancy(self):
        self.observe();s=self.assign();a=s['actionResult'];target=self.space()
        self.assertEqual(target['occupancy'],'vacant');self.assertFalse(target['available']);self.assertIsNotNone(target['assignment'])
        self.assertEqual(s['counts']['assigned'],1);self.assertEqual(a['expires'],self.now+ASSIGNMENT_MS)

    def test_unknown_occupied_or_closed_space_cannot_be_assigned(self):
        with self.assertRaises(ApiError):self.assign()
        self.observe(state='occupied')
        with self.assertRaises(ApiError):self.assign()
        self.observe();self.ledger.zone('A',self.payload(open=False,revision=1))
        with self.assertRaises(ApiError):self.assign()

    def test_operator_closure_survives_new_vacancy_and_restart(self):
        self.ledger.zone('A',self.payload(open=False,revision=1));self.observe()
        self.ledger=VacancyLedger(self.path,lambda:self.now);self.assertFalse(self.space()['zoneOpen']);self.assertFalse(self.space()['available'])
        with self.assertRaises(ApiError):self.ledger.zone('A',self.payload(open=True,revision=1))
        self.ledger.zone('A',self.payload(open=True,revision=2));self.assertTrue(self.space()['available'])

    def test_one_arrival_cannot_have_two_active_dispatches(self):
        self.observe();self.observe('B-01');self.assign()
        with self.assertRaises(ApiError):self.assign('B-01')

    def test_simultaneous_dispatches_to_one_space_have_one_winner(self):
        self.observe();barrier=threading.Barrier(12)
        payloads=[self.payload(spaceId='A-01',arrivalId=f'IN-{i}') for i in range(12)]
        def run(p):
            barrier.wait()
            try:self.ledger.assign(p);return 200
            except ApiError as e:return e.status
        with ThreadPoolExecutor(12) as pool:results=list(pool.map(run,payloads))
        self.assertEqual(results.count(200),1);self.assertEqual(results.count(409),11)

    def test_parallel_replays_create_only_one_assignment(self):
        self.observe();p=self.payload(spaceId='A-01',arrivalId='IN-1')
        with ThreadPoolExecutor(6) as pool:results=list(pool.map(lambda _:self.ledger.assign(p),range(6)))
        self.assertEqual(len({s['actionResult']['id'] for s in results}),1)

    def test_old_request_id_cannot_be_reused_for_other_data_or_another_operation(self):
        p=self.observation();self.ledger.observe(p)
        with self.assertRaises(ApiError):self.ledger.observe(p|{'state':'occupied'})
        with self.assertRaises(ApiError):self.ledger.assign(self.payload(requestId=p['requestId'],spaceId='A-01',arrivalId='IN-1'))

    def test_dispatch_replay_after_expiry_never_renews_or_creates_another(self):
        self.observe(ttlMillis=120000);p=self.payload(spaceId='A-01',arrivalId='IN-1');a=self.ledger.assign(p)['actionResult']
        self.now+=ASSIGNMENT_MS;s=self.ledger.assign(p);self.assertEqual(s['actionResult']['status'],'expired');self.assertEqual(s['actionResult']['id'],a['id'])
        self.assertEqual(s['counts']['assigned'],0);self.assertTrue(self.space()['available'])

    def test_cancellation_is_idempotent_and_releases_only_the_lease(self):
        self.observe();a=self.assign()['actionResult'];self.action(a['id'],'cancel');self.action(a['id'],'cancel')
        self.assertTrue(self.space()['available']);self.assertEqual(self.state()['counts']['assigned'],0)

    def test_arrival_requires_fresh_occupied_evidence(self):
        self.observe();a=self.assign()['actionResult']
        with self.assertRaises(ApiError):self.action(a['id'],'arrival')
        self.observe(state='occupied');s=self.action(a['id'],'arrival');self.assertEqual(s['counts']['assigned'],0)
        self.assertEqual(self.space()['occupancy'],'occupied');self.assertFalse(self.space()['available'])

    def test_new_conflicting_or_expired_observations_flag_dispatch_for_review(self):
        self.observe(ttlMillis=1000);self.assign();self.now+=1000;s=self.state();self.assertEqual(s['assignments'][0]['attentionReason'],'stale')
        self.assertEqual(s['counts']['assigned'],1);self.assertFalse(self.space()['available'])

    def test_active_assignment_is_not_lost_outside_recent_history_limit(self):
        self.observe(ttlMillis=120000);self.observe('B-01',ttlMillis=120000);self.assign()
        for i in range(105):
            a=self.assign('B-01',f'OTHER-{i}')['actionResult'];self.action(a['id'],'cancel')
        self.assertIsNotNone(self.space()['assignment']);self.assertEqual(self.state()['counts']['assigned'],1)
        with self.assertRaises(ApiError):self.assign(arrival='DUPLICATE')

    def test_restart_preserves_sources_reports_assignments_and_absolute_deadline(self):
        self.share();self.observe(source='CAR-1');a=self.assign()['actionResult'];self.now+=1000
        self.ledger=VacancyLedger(self.path,lambda:self.now);s=self.state()
        self.assertTrue(self.source('CAR-1')['sharing']);self.assertEqual(s['assignments'][0]['expires'],a['expires']);self.assertEqual(s['counts']['assigned'],1)

    def test_new_ledger_does_not_change_existing_reservations(self):
        existing=Ledger(self.path,lambda:self.now);before=existing.reserve('V-07','A-14','legacy')['reservation']
        VacancyLedger(self.path,lambda:self.now);self.assertEqual(existing.state('V-07')['reservation'],before)


class VacancyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.server=Server(('127.0.0.1',0),Ledger(Path(self.temp.name)/'test.db'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def get(self,path):return urlopen(self.base+path)
    def test_dashboard_and_assets_are_served_with_browser_safety_headers(self):
        for path,content in [('/admin/','text/html'),('/admin/app.js','text/javascript'),('/admin/style.css','text/css'),('/admin/demo-map.png','image/png')]:
            with self.get(path) as r:self.assertIn(content,r.headers['Content-Type']);self.assertIn("default-src 'self'",r.headers['Content-Security-Policy']);self.assertEqual(r.headers['Cache-Control'],'no-store')
    def test_state_and_observation_are_real_http_and_persisted(self):
        with self.get('/v1/vacancy/state?eventId='+EVENT) as r:s=json.load(r)
        p={'eventId':EVENT,'mapVersion':1,'requestId':'http-observe','spaceId':'A-01','sourceId':'STAFF-1','sequence':1,'observedAtMillis':s['serverTimeMillis'],'ttlMillis':60000,'confidence':.95,'state':'vacant','view':'field'}
        request=Request(self.base+'/v1/vacancy/observations',json.dumps(p).encode(),{'Content-Type':'application/json'})
        with urlopen(request) as r:self.assertEqual(json.load(r)['counts']['available'],1)
    def test_foreign_origin_and_host_cannot_mutate_or_read_dashboard(self):
        for headers in ({'Origin':'https://other.example'},{'Host':'other.example'}):
            with self.assertRaises(HTTPError) as e:urlopen(Request(self.base+'/v1/vacancy/state?eventId='+EVENT,headers=headers))
            self.assertEqual(e.exception.code,403)
    def test_unknown_file_traversal_and_event_are_rejected(self):
        for path in ['/admin/../server.py','/admin/not-here','/v1/vacancy/state?eventId=unknown']:
            with self.assertRaises(HTTPError) as e:self.get(path)
            self.assertEqual(e.exception.code,404)
    def test_nonfinite_json_is_rejected_instead_of_crashing(self):
        r=Request(self.base+'/v1/vacancy/observations',b'{"confidence":NaN}',{'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as e:urlopen(r)
        self.assertEqual(e.exception.code,400)


if __name__=='__main__':unittest.main()
