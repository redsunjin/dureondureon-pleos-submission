from contextlib import closing
from pathlib import Path
import json,math,sqlite3,tempfile,threading,unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from coordination.curb_sessions import CurbSessions,build_template
from coordination.errors import ApiError
from coordination.server import Server,Ledger


class TrafficSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.template=build_template()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.db';self.now=100000;self.seq=0
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
    def tearDown(self):self.temp.cleanup()
    def uid(self):self.seq+=1;return f'traffic-{self.seq}'
    def state(self):return self.ledger.state('V-07')['session']
    def poses(self,s):return {v['vehicleId']:v['pose'] for v in s['participants']}
    def act(self,s,a,car=None,uid=None):return self.ledger.act(s['sessionId'],'V-07',uid or self.uid(),s['revision'],a,car,f"{s['sessionId']}_{car}" if car else None)['session']
    def approve(self,s):
        for c in s['participants']:s=self.act(s,'approve',c['vehicleId'])
        return s
    def running(self,at=40):
        s=self.ledger.create('V-07',self.uid())['session'];s=self.act(self.approve(s),'start')
        for _ in range(at//2):self.now+=500;s=self.state()
        return s
    def traffic(self,s,scenario='pedestrian',uid=None,tracks=None,source='scripted'):
        return self.ledger.traffic(s['sessionId'],'V-07',uid or self.uid(),s['revision'],tracks,source,scenario)['session']
    def replan(self,s):return self.ledger.replan(s['sessionId'],'V-07',self.uid(),s['revision'])['session']
    def cleared(self,s):
        for _ in range(14):self.now+=500;s=self.state()
        self.assertEqual(s['traffic']['safety'],'clear');return s
    def finish(self,s):
        s=self.act(self.approve(s),'start')
        while s['status']=='running':self.now+=500;s=self.state()
        self.assertEqual(s['status'],'completed');return s
    def live_clear(self,s,stamp=None):
        return self.traffic(s,None,source='observed',tracks=[])

    def test_crossing_risk_freezes_before_advancing_and_drops_every_old_vote(self):
        before=self.running();stopped=self.traffic(before)
        self.assertEqual(stopped['reason'],'traffic_risk');self.assertEqual(self.poses(stopped),self.poses(before))
        self.assertEqual(stopped['traffic']['safety'],'risk');self.assertFalse(stopped['traffic']['safeToReplan'])
        self.assertTrue(all(v['decision']=='pending' for v in stopped['participants']))
        with self.assertRaises(ApiError):self.replan(stopped)
        self.now+=500;later=self.state();self.assertEqual(self.poses(later),self.poses(stopped))
        self.assertNotEqual(later['traffic']['tracks'][0]['pose'],stopped['traffic']['tracks'][0]['pose'])

    def test_execution_monitor_stops_when_previously_distant_vehicle_enters_horizon(self):
        s=self.running(28)
        track={'id':'ONCOMING','kind':'vehicle','pose':{'x':65,'y':4.55,'yaw':math.pi},
            'length':4,'width':1.5,'vx':-2,'vy':0,'observedAtMillis':self.now,'durationSeconds':40}
        s=self.traffic(s,None,tracks=[track]);self.assertEqual(s['status'],'running')
        self.assertNotEqual(s['traffic']['safety'],'risk')
        for _ in range(20):
            self.now+=500;s=self.state()
            if s['status']=='stopped':break
        self.assertEqual(s['reason'],'traffic_risk')
        frozen=self.poses(s);self.now+=500;self.assertEqual(self.poses(self.state()),frozen)

    def test_clearance_requires_dwell_then_new_plan_all_new_votes_and_explicit_start(self):
        stopped=self.traffic(self.running());clear=self.cleared(stopped)
        self.assertEqual(clear['status'],'stopped');self.assertEqual(clear['reason'],'traffic_cleared');self.assertEqual(self.poses(clear),self.poses(stopped))
        child=self.replan(clear);self.assertEqual(self.poses(child),self.poses(clear));self.assertNotEqual(child['sessionId'],clear['sessionId'])
        self.assertTrue(all(v['decision']=='pending' for v in child['participants']))
        for car in child['participants'][:4]:child=self.act(child,'approve',car['vehicleId'])
        with self.assertRaises(ApiError):self.act(child,'start')
        ready=self.act(child,'approve','NEW');self.now+=300;ready=self.state();self.assertEqual(ready['status'],'ready');self.assertEqual(ready['elapsedSeconds'],0)
        s=self.act(ready,'start')
        while s['status']=='running':self.now+=500;s=self.state()
        self.assertEqual(s['status'],'completed')

    def test_moving_vehicle_crosses_then_parking_completes(self):
        s=self.traffic(self.running(32),'vehicle');self.assertEqual(s['traffic']['safety'],'risk')
        self.finish(self.replan(self.cleared(s)))

    def test_distant_motion_updates_do_not_reset_safe_approvals_or_revision(self):
        s=self.ledger.create('V-07',self.uid())['session'];s=self.live_clear(s);self.now+=1000;s=self.live_clear(s)
        s=self.act(s,'approve','P-1');revision=s['revision']
        for _ in range(3):self.now+=100;s=self.live_clear(s)
        self.assertEqual(s['revision'],revision);self.assertEqual(s['participants'][0]['decision'],'approved')

    def test_expired_live_coverage_is_a_hold_even_for_empty_object_list(self):
        s=self.running();s=self.live_clear(s);self.now+=1501;stopped=self.state()
        self.assertEqual(stopped['reason'],'traffic_stale');self.assertEqual(stopped['traffic']['safety'],'stale')
        with self.assertRaises(ApiError):self.replan(stopped)
        fresh=self.live_clear(stopped);self.assertEqual(fresh['traffic']['safety'],'clearing')
        self.now+=999;self.assertEqual(self.live_clear(fresh)['traffic']['safety'],'clearing')
        self.now+=1;fresh=self.live_clear(self.state());self.assertTrue(fresh['traffic']['safeToReplan'])
        self.assertEqual(fresh['status'],'stopped')

    def test_start_action_cannot_rollback_a_new_stale_coverage_stop(self):
        s=self.ledger.create('V-07',self.uid())['session'];s=self.live_clear(s);self.now+=1000;s=self.live_clear(s);s=self.approve(s)
        self.now+=1501;stopped=self.act(s,'start',uid='stale-start')
        self.assertEqual(stopped['status'],'stopped');self.assertEqual(stopped['reason'],'traffic_stale')
        self.assertEqual(self.state()['status'],'stopped')
        replay=self.ledger.act(s['sessionId'],'V-07','stale-start',s['revision'],'start')['session']
        self.assertEqual(replay['status'],'stopped')

    def test_new_risk_during_renewed_consent_invalidates_those_votes_again(self):
        source=self.cleared(self.traffic(self.running()));child=self.replan(source);child=self.act(child,'approve','P-1')
        t={'id':'NEAR','kind':'pedestrian','pose':self.poses(child)['NEW'],'length':.6,'width':.6,'vx':0,'vy':0,'observedAtMillis':self.now,'durationSeconds':0}
        stopped=self.traffic(child,None,tracks=[t],source='observed')
        self.assertEqual(stopped['status'],'stopped');self.assertTrue(all(v['decision']=='pending' for v in stopped['participants']))
        with self.assertRaises(ApiError):self.act(child,'approve','P-2')

    def test_clearance_flicker_restarts_the_dwell_period(self):
        s=self.traffic(self.running());s=self.traffic(s,'clear');self.now+=800;s=self.state();self.assertEqual(s['traffic']['safety'],'clearing')
        s=self.traffic(s);self.assertEqual(s['traffic']['safety'],'risk');s=self.traffic(s,'clear')
        self.now+=800;self.assertEqual(self.state()['traffic']['safety'],'clearing')
        self.now+=200;self.assertEqual(self.state()['traffic']['safety'],'clear')

    def test_new_risk_after_clearance_replaces_the_cleared_reason(self):
        clear=self.cleared(self.traffic(self.running()))
        self.assertEqual(clear['reason'],'traffic_cleared')
        risk=self.traffic(clear)
        self.assertEqual(risk['status'],'stopped');self.assertEqual(risk['reason'],'traffic_risk')
        self.assertFalse(risk['traffic']['safeToReplan']);self.assertEqual(self.poses(risk),self.poses(clear))

    def test_lost_traffic_reply_replays_without_restarting_the_script_clock(self):
        before=self.running();s=self.traffic(before,uid='lost-script');self.now+=500
        replay=self.traffic(before,uid='lost-script')
        self.assertEqual(replay['traffic']['revision'],s['traffic']['revision'])
        self.assertNotEqual(replay['traffic']['tracks'][0]['pose'],s['traffic']['tracks'][0]['pose'])

    def test_restart_retains_stopped_geometry_but_live_coverage_must_be_fresh(self):
        s=self.live_clear(self.running());self.now+=2000
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template);restored=self.state()
        self.assertEqual(restored['status'],'stopped');self.assertEqual(restored['traffic']['safety'],'stale');self.assertEqual(self.poses(restored),self.poses(s))

    def test_bad_or_out_of_order_motion_does_not_replace_valid_coverage(self):
        s=self.running();t={'id':'FAR','kind':'vehicle','pose':{'x':1,'y':20,'yaw':0},'length':4,'width':1.5,'vx':1,'vy':0,'observedAtMillis':self.now,'durationSeconds':0}
        s=self.traffic(s,None,tracks=[t],source='observed');version=s['traffic']['revision'];self.now+=100
        for tracks in ([{**t,'observedAtMillis':t['observedAtMillis']-1}],[{**t,'vx':float('nan')}],[{**t,'id':'P-1'}]):
            with self.assertRaises(ApiError):self.traffic(self.state(),None,tracks=tracks,source='observed')
            self.assertEqual(self.state()['traffic']['revision'],version)

    def test_new_risk_during_geometry_calculation_cannot_publish_a_stale_plan(self):
        source=self.cleared(self.traffic(self.running()));calculate=self.ledger._replan_template
        def change(row):
            result=calculate(row)
            t={'id':'HOLD','kind':'pedestrian','pose':self.poses(source)['NEW'],'length':.6,'width':.6,'vx':0,'vy':0,'observedAtMillis':self.now,'durationSeconds':0}
            self.traffic(source,None,tracks=[t],source='observed');return result
        with patch.object(self.ledger,'_replan_template',side_effect=change):
            with self.assertRaises(ApiError):self.replan(source)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM curb_sessions').fetchone()[0],1)
        self.assertEqual(self.state()['traffic']['safety'],'risk')

    def test_actual_http_traffic_endpoint_returns_predictive_stop(self):
        server=Server(('127.0.0.1',0),Ledger(self.path,lambda:self.now));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            s=self.running();body={'ownerVehicleId':'V-07','requestId':self.uid(),'revision':s['revision'],'source':'scripted','scenario':'pedestrian'}
            req=Request(f'http://127.0.0.1:{server.server_port}/v1/curb/sessions/{s["sessionId"]}/traffic',json.dumps(body).encode(),{'Content-Type':'application/json'})
            r=json.load(urlopen(req))['session'];self.assertEqual(r['reason'],'traffic_risk');self.assertEqual(r['traffic']['tracks'][0]['kind'],'pedestrian')
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
