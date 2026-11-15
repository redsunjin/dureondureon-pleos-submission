from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen

from coordination.curb_sessions import CurbSessions,build_template,APPROVAL_MS
from coordination.errors import ApiError
from coordination.server import Ledger,Server


class ReplanSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.template=build_template()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db';self.now=100000;self.seq=0
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
    def tearDown(self):self.temp.cleanup()
    def uid(self):self.seq+=1;return f'request-{self.seq}'
    def state(self):return self.ledger.state('V-07')['session']
    def action(self,s,action,car=None,request=None):
        return self.ledger.act(s['sessionId'],'V-07',request or self.uid(),s['revision'],action,car,
            f"{s['sessionId']}_{car}" if car else None)['session']
    def approve(self,s):
        for c in s['participants']:s=self.action(s,'approve',c['vehicleId'])
        return s
    def stopped(self,model_seconds=2):
        s=self.ledger.create('V-07',self.uid())['session'];s=self.action(self.approve(s),'start')
        remaining=model_seconds/4*1000
        while remaining>0:
            step=min(500,remaining);self.now+=int(step);remaining-=step;s=self.state()
        return self.action(s,'stop')
    def replan(self,s,request=None):return self.ledger.replan(s['sessionId'],'V-07',request or self.uid(),s['revision'])['session']
    def data(self,s):
        with sqlite3.connect(self.path) as db:return json.loads(db.execute('SELECT data FROM curb_sessions WHERE id=?',(s['sessionId'],)).fetchone()[0])
    def write_data(self,s,data):
        with sqlite3.connect(self.path) as db:db.execute('UPDATE curb_sessions SET data=? WHERE id=?',(json.dumps(data),s['sessionId']))
    def poses(self,s):return {c['vehicleId']:c['pose'] for c in s['participants']}
    def check_new(self,source,child):
        self.assertNotEqual(source['sessionId'],child['sessionId']);self.assertEqual(child['parentSessionId'],source['sessionId'])
        self.assertEqual(child['parentRevision'],source['revision']);self.assertEqual(child['status'],'awaiting')
        self.assertEqual(child['elapsedSeconds'],0);self.assertEqual(self.poses(source),self.poses(child))
        self.assertTrue(all(c['decision']=='pending' and c['progressPercent']==0 for c in child['participants']))
        self.assertTrue(all(c['moveRequestId'].startswith(child['sessionId']) for c in child['participants']))
    def finish(self,s):
        s=self.action(self.approve(s),'start')
        for _ in range(70):
            self.now+=500;s=self.state()
            if s['status']=='completed':break
        self.assertEqual(s['status'],'completed');self.assertTrue(all(c['progressPercent']==100 for c in s['participants']))
        return s

    def test_replan_preserves_stopped_space_creation_positions_and_changes_requests(self):
        old=self.stopped();new=self.replan(old);self.check_new(old,new)
        self.assertEqual(new['replanMode'],'current_pose_search');self.assertEqual(new['replanGeneration'],1)
        self.assertNotEqual(old['participants'][0]['pose'],self.template['participants'][0]['initialPose'])
        self.assertLess(new['durationSeconds'],old['durationSeconds'])

    def test_mid_reverse_pose_is_retained_and_remaining_path_finishes(self):
        old=self.stopped(48);new=self.replan(old);self.check_new(old,new)
        self.assertGreater(next(c for c in new['participants'] if c['vehicleId']=='NEW')['pose']['yaw'],0.01)
        self.assertEqual(new['replanMode'],'remaining_path_revalidated')
        self.finish(new)

    def test_four_reapprovals_cannot_start_and_previous_consent_is_not_reused(self):
        new=self.replan(self.stopped())
        for c in new['participants'][:-1]:new=self.action(new,'approve',c['vehicleId'])
        with self.assertRaises(ApiError):self.action(new,'start')
        self.assertEqual(self.state()['elapsedSeconds'],0)
        new=self.action(new,'approve','NEW');self.assertEqual(new['status'],'ready')
        self.assertEqual(self.action(new,'start')['status'],'running')

    def test_rejection_freezes_current_arrangement_and_replanning_again_reasks_everyone(self):
        old=self.stopped(48);new=self.replan(old);new=self.action(new,'reject','P-2')
        self.assertEqual(self.poses(old),self.poses(new))
        again=self.replan(new);self.check_new(new,again);self.assertEqual(again['replanGeneration'],2)

    def test_old_move_request_does_not_approve_new_plan(self):
        old=self.stopped();new=self.replan(old)
        with self.assertRaises(ApiError) as error:
            self.ledger.act(new['sessionId'],'V-07',self.uid(),new['revision'],'approve','P-1',old['participants'][0]['moveRequestId'])
        self.assertEqual(error.exception.code,'invalid_move_request')
        self.assertTrue(all(c['decision']=='pending' for c in self.state()['participants']))
        with self.assertRaises(ApiError):self.action(old,'start')

    def test_idempotent_replan_returns_same_child_and_keeps_new_votes(self):
        old=self.stopped();new=self.replan(old,'same-replan');self.action(new,'approve','P-1')
        replay=self.replan(old,'same-replan')
        self.assertEqual(new['sessionId'],replay['sessionId']);self.assertEqual(replay['participants'][0]['decision'],'approved')
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM curb_sessions').fetchone()[0],2)
        with self.assertRaises(ApiError):self.ledger.replan(old['sessionId'],'V-07','same-replan',old['revision']+1)

    def test_old_receipt_cannot_redraw_an_ancestor_after_a_second_replan(self):
        old=self.stopped(48);first=self.replan(old,'first-replan')
        rejected=self.action(first,'reject','P-2');second=self.replan(rejected)
        second=self.action(second,'approve','P-1')
        replay=self.replan(old,'first-replan')
        self.assertEqual(replay['sessionId'],second['sessionId'])
        self.assertEqual(self.poses(replay),self.poses(second))
        self.assertEqual(replay['participants'][0]['decision'],'approved')

    def test_stale_active_completed_and_superseded_sources_are_refused(self):
        old=self.stopped()
        with self.assertRaises(ApiError):self.ledger.replan(old['sessionId'],'V-07',self.uid(),old['revision']-1)
        new=self.replan(old)
        with self.assertRaises(ApiError):self.replan(new)
        with self.assertRaises(ApiError):self.replan(old)
        completed=self.finish(new)
        with self.assertRaises(ApiError):self.replan(completed)

    def test_concurrent_replans_cannot_fork_the_frozen_arrangement(self):
        old=self.stopped();barrier=threading.Barrier(2)
        def run(i):
            barrier.wait()
            try:self.replan(old,f'parallel-{i}');return 200
            except ApiError as error:return error.status
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(run,[1,2])),[200,409])

    def test_successive_replans_keep_original_absolute_bounds(self):
        first=self.replan(self.stopped(2));s=self.action(self.approve(first),'start')
        for _ in range(3):self.now+=500;s=self.state()
        stopped=self.action(s,'stop');second=self.replan(stopped);self.check_new(stopped,second)
        original={c['id']:c for c in self.template['scene']['parked']}
        for c in self.data(second)['scene']['parked']:
            o=original[c['id']]
            self.assertAlmostEqual(c['pose']['x']-c['max_backward'],o['pose']['x']-o['max_backward'])
            self.assertAlmostEqual(c['pose']['x']+c['max_forward'],o['pose']['x']+o['max_forward'])
        self.finish(second)

    def test_restart_retains_child_pending_votes_and_then_stops_running_child_in_place(self):
        child=self.replan(self.stopped(48));child=self.action(child,'approve','P-1')
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
        restored=self.state();self.assertEqual(restored['sessionId'],child['sessionId']);self.assertEqual(restored['participants'][0]['decision'],'approved')
        for c in restored['participants'][1:]:restored=self.action(restored,'approve',c['vehicleId'])
        restored=self.action(restored,'start');self.now+=500;before=self.state()
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
        stopped=self.state();self.assertEqual(stopped['reason'],'server_restarted');self.assertEqual(self.poses(stopped),self.poses(before))
        again=self.replan(stopped);self.check_new(stopped,again)

    def test_legacy_known_checkpoint_is_recovered_without_reset(self):
        old=self.stopped();data=self.data(old)
        for key in ('scene','plan','schemaVersion','continuation'):data.pop(key,None)
        self.write_data(old,data);self.check_new(old,self.replan(old))

    def test_invalid_checkpoint_cannot_silently_reset_or_create_a_child(self):
        old=self.stopped(48);data=self.data(old)
        i=max(i for i,f in enumerate(data['frames']) if f['time']<=old['elapsedSeconds']+1e-6)
        data['frames'][i]['poses']['NEW']['x']+=0.2;self.write_data(old,data)
        frozen=self.state()
        with self.assertRaises(ApiError) as error:self.replan(frozen)
        self.assertEqual(error.exception.status,422);self.assertEqual(self.poses(self.state()),self.poses(frozen))
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM curb_sessions').fetchone()[0],1)

    def test_expired_waiting_plan_can_replan_with_fresh_approval_period(self):
        old=self.ledger.create('V-07',self.uid())['session'];self.now+=APPROVAL_MS;old=self.state()
        self.assertEqual(old['status'],'expired');new=self.replan(old);self.check_new(old,new)
        self.assertGreater(new['expiresAtMillis'],old['expiresAtMillis'])

    def test_zero_remaining_vehicle_still_requires_new_consent_to_hold_position(self):
        old=self.stopped(76);child=self.replan(old);self.check_new(old,child)
        stationary=[c for c in child['participants'] if c['totalTravel']==0]
        self.assertTrue(any(c['vehicleId']=='NEW' for c in stationary))
        self.assertTrue(all(c['decision']=='pending' and not c['steps'] for c in stationary))
        for c in child['participants']:
            if c['vehicleId']!='NEW':child=self.action(child,'approve',c['vehicleId'])
        with self.assertRaises(ApiError):self.action(child,'start')


class ReplanHttpTests(unittest.TestCase):
    def test_http_checkpoint_new_plan_has_fresh_requests_and_same_positions(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[100000];server=Server(('127.0.0.1',0),Ledger(Path(folder)/'http.db',lambda:now[0]))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();counter=[0]
            def post(path,body):
                with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as response:return json.load(response)['session']
            def action(s,name,car=None):
                counter[0]+=1
                return post(f"/v1/curb/sessions/{s['sessionId']}/actions",{'ownerVehicleId':'V-07','requestId':f'http-{counter[0]}','revision':s['revision'],'action':name,'participantId':car,'moveRequestId':f"{s['sessionId']}_{car}" if car else None})
            try:
                s=post('/v1/curb/sessions',{'ownerVehicleId':'V-07','requestId':'create'})
                for c in s['participants']:s=action(s,'approve',c['vehicleId'])
                s=action(s,'start');now[0]+=500;s=action(s,'stop')
                new=post(f"/v1/curb/sessions/{s['sessionId']}/replan",{'ownerVehicleId':'V-07','requestId':'replan','revision':s['revision']})
                self.assertEqual([c['pose'] for c in s['participants']],[c['pose'] for c in new['participants']])
                self.assertEqual(new['reason'],'awaiting_reconsent');self.assertTrue(all(c['decision']=='pending' for c in new['participants']))
            finally:server.shutdown();server.server_close();thread.join()
