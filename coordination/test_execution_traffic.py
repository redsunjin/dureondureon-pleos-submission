import unittest
from copy import deepcopy
from coordination import test_execution as base
from coordination.errors import ApiError

class ExecutionTrafficTests(unittest.TestCase):
    setUpClass=classmethod(base.ExecutionTests.setUpClass.__func__)
    tearDown=base.ExecutionTests.tearDown
    uid=base.ExecutionTests.uid
    delegate=base.ExecutionTests.delegate
    p=base.ExecutionTests.p
    prepare=base.ExecutionTests.prepare
    dispatch=base.ExecutionTests.dispatch
    accept=base.ExecutionTests.accept
    report=base.ExecutionTests.report
    heartbeats=base.ExecutionTests.heartbeats
    propose=base.ExecutionTests.propose

    def setUp(self):
        base.ExecutionTests.setUp(self)
        self.ledger.stop(self.pid,self.uid())
        self.pid=self.ledger.propose(self.trajectory,'CURB',self.now+300000,self.uid(),require_traffic=True)['planId']

    def observe(self,tracks=None,stamp=None,revision=None):
        p=self.p();t=p.get('traffic')
        return self.ledger.observe_traffic(self.pid,revision if revision is not None else t['revision'] if t else 0,
            self.now if stamp is None else stamp,[] if tracks is None else tracks,self.uid())

    def clear(self):
        self.observe();self.now+=1000;self.observe()

    def begin(self):
        self.clear();self.prepare();self.ledger.start(self.pid,self.uid());return self.dispatch()

    def track(self,**kw):
        t=dict(id='PED',kind='pedestrian',pose=dict(x=3.8,y=6,yaw=0),length=.4,width=.4,vx=0.0,vy=-.6,observedAtMillis=self.now,durationSeconds=0)
        t.update(kw);return t

    def test_missing_and_clearing_coverage_block_start(self):
        self.prepare()
        with self.assertRaises(ApiError):self.ledger.start(self.pid,self.uid())
        self.assertEqual(self.p()['trafficSafety']['safety'],'missing')
        self.observe();self.assertEqual(self.p()['trafficSafety']['safety'],'clearing')
        with self.assertRaises(ApiError):self.ledger.start(self.pid,self.uid())
        self.now+=1000;self.observe();self.ledger.start(self.pid,self.uid())
        self.assertEqual(self.p()['status'],'running')

    def test_predicted_crossing_requests_stop_without_claiming_stationary(self):
        b=self.begin();self.accept(b);self.report(b,'executing')
        before=self.ledger.state()['poses'];self.now+=1;self.observe([self.track()])
        p=self.p();self.assertEqual(p['status'],'stop_requested');self.assertEqual(p['motionKnowledge'],'unknown')
        self.assertEqual(p['trafficSafety']['safety'],'risk')
        self.assertEqual(p['trafficSafety']['riskId'],'PED');self.assertEqual(self.ledger.state()['poses'],before)
        with self.assertRaises(ApiError):self.dispatch()
        self.assertGreater(sum(u['reserved'] for u in self.ledger.state()['usage'].values()),0)
        self.report(b,'stopped',seq=2)
        self.assertEqual(self.p()['status'],'stopped')
        self.assertTrue(all(u['reserved']==0 for u in self.ledger.state()['usage'].values()))

    def test_current_segment_sweep_not_only_reported_start_pose(self):
        self.begin();self.now+=1
        self.observe([self.track(pose=dict(x=.3,y=1.5,yaw=0),vx=0,vy=0,length=.2,width=.2)])
        self.assertEqual(self.p()['trafficSafety']['safety'],'risk')
        self.assertEqual(self.p()['trafficSafety']['riskVehicle'],'P-1')

    def test_crossing_between_observation_endpoints_is_detected(self):
        self.begin();self.now+=1
        self.observe([self.track(pose=dict(x=-5,y=1.5,yaw=0),vx=5,vy=0)])
        self.assertEqual(self.p()['status'],'stop_requested')

    def test_all_stationary_vehicles_are_checked(self):
        self.begin();self.now+=1
        self.observe([self.track(pose=dict(x=24.2,y=1.5,yaw=0),vx=0,vy=0)])
        self.assertEqual(self.p()['trafficSafety']['riskVehicle'],'P-4')

    def test_empty_frame_expiry_is_not_clear(self):
        self.begin();self.now+=1501
        self.heartbeats()
        self.assertEqual(self.p()['trafficSafety']['safety'],'stale')
        self.assertEqual(self.p()['status'],'stop_requested')

    def test_clearance_never_resumes_or_reuses_old_readiness(self):
        b=self.begin();self.now+=1;self.observe([self.track()])
        self.now+=1;self.observe();self.assertEqual(self.p()['trafficSafety']['safety'],'clearing')
        self.now+=1000;self.observe();self.assertEqual(self.p()['trafficSafety']['safety'],'clear')
        self.assertEqual(self.p()['status'],'stop_requested');self.assertEqual(self.p()['ready'],{})
        with self.assertRaises(ApiError):self.dispatch()
        self.agents[b['command']['vehicle']].confirm_stop(b)
        self.assertEqual(self.p()['status'],'stopped')
        self.pid=self.ledger.propose(self.trajectory,'CURB',self.now+300000,self.uid(),require_traffic=True)['planId']
        self.assertEqual(self.p()['trafficSafety']['safety'],'missing')
        with self.assertRaises(ApiError):self.ledger.start(self.pid,self.uid())

    def test_late_or_reordered_frames_cannot_clear_a_risk(self):
        self.begin();self.now+=1;self.observe([self.track()]);p=self.p();revision=p['traffic']['revision'];stamp=self.now
        self.now+=1
        for old_stamp,rev in [(stamp-1,revision),(stamp,revision),(self.now,revision-1)]:
            with self.assertRaises(ApiError):self.observe([],stamp=old_stamp,revision=rev)
        self.assertEqual(self.p()['traffic']['revision'],revision)
        self.assertEqual(self.p()['trafficSafety']['safety'],'risk')

    def test_frame_validation_and_body_identity_collision(self):
        self.clear()
        self.now+=1
        invalid=[self.track(id='P-1'),self.track(vx=float('nan')),self.track(observedAtMillis=self.now+1),self.track(durationSeconds=2)]
        before=self.p()['traffic']['revision']
        for t in invalid:
            with self.assertRaises(ApiError):self.observe([t])
        with self.assertRaises(ApiError):self.observe([],stamp=self.now+1)
        self.assertEqual(self.p()['traffic']['revision'],before)

    def test_old_empty_snapshot_does_not_establish_fresh_coverage(self):
        self.observe([],stamp=self.now-1501)
        self.assertEqual(self.p()['trafficSafety']['safety'],'stale')
        self.assertEqual(self.p()['status'],'cancelled')

    def test_duplicate_frame_request_does_not_extend_freshness(self):
        req=self.uid();stamp=self.now
        self.ledger.observe_traffic(self.pid,0,stamp,[],req)
        self.now+=1000;self.ledger.observe_traffic(self.pid,0,stamp,[],req)
        self.assertEqual(self.p()['traffic']['observedAtMillis'],stamp)
        self.assertEqual(self.p()['traffic']['revision'],1)
        self.now+=501;self.assertEqual(self.p()['trafficSafety']['safety'],'stale')

    def test_fresh_far_obstacle_allows_explicit_execution(self):
        self.observe([self.track(pose=dict(x=90,y=90,yaw=0),vx=0,vy=0)])
        self.now+=1000;self.observe([self.track(pose=dict(x=90,y=90,yaw=0),vx=0,vy=0)])
        self.prepare();self.ledger.start(self.pid,self.uid());b=self.dispatch()
        self.agents[b['command']['vehicle']].execute(b)
        self.assertEqual(self.p()['index'],1)
        self.assertEqual(self.p()['status'],'running')
