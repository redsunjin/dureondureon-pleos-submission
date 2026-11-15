from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
import threading
from coordination.curb_sessions import CurbSessions,build_template
from coordination.errors import ApiError
from coordination.server import Ledger,Server
from simulator import curb_parking as cp,curb_replan as cr


class ObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.template=build_template()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db';self.now=100000;self.seq=0
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
    def tearDown(self):self.temp.cleanup()
    def uid(self):self.seq+=1;return f'change-{self.seq}'
    def state(self):return self.ledger.state('V-07')['session']
    def act(self,s,action,car=None):return self.ledger.act(s['sessionId'],'V-07',self.uid(),s['revision'],action,car,f"{s['sessionId']}_{car}" if car else None)['session']
    def approve(self,s):
        for v in s['participants']:s=self.act(s,'approve',v['vehicleId'])
        return s
    def running(self):
        s=self.ledger.create('V-07',self.uid())['session'];s=self.act(self.approve(s),'start')
        for _ in range(24):self.now+=500;s=self.state()
        return s
    def observe(self,s,poses=None,obstacles=None,request=None):return self.ledger.observe(s['sessionId'],'V-07',request or self.uid(),s['revision'],poses,obstacles)['session']
    def replan(self,s):return self.ledger.replan(s['sessionId'],'V-07',self.uid(),s['revision'])['session']
    def poses(self,s):return {v['vehicleId']:v['pose'] for v in s['participants']}
    def obstacle(self,x=16,y=5.9,length=.25,width=.25):return {'id':'BOX','pose':{'x':x,'y':y,'yaw':0},'length':length,'width':width}
    def finish(self,s):
        s=self.act(self.approve(s),'start')
        while s['status']=='running':self.now+=500;s=self.state()
        self.assertEqual(s['status'],'completed');return s

    def test_running_observation_stops_and_invalidates_every_vote_in_one_write(self):
        old=self.running();new=self.observe(old,obstacles=[self.obstacle()])
        self.assertEqual(new['reason'],'environment_changed');self.assertEqual(new['status'],'stopped')
        self.assertEqual(self.poses(new),self.poses(old));self.assertTrue(all(v['decision']=='pending' for v in new['participants']))
        self.assertEqual(new['environmentRevision'],1);self.assertEqual(new['obstacles'],[self.obstacle()]);self.assertEqual(new['plannedPath'],[])
        for _ in range(5):self.now+=500;self.assertEqual(self.poses(self.state()),self.poses(new))
        with self.assertRaises(ApiError):self.act(new,'start')

    def test_changed_obstacle_gets_new_controls_all_reconsent_and_completion(self):
        old=self.running();changed=self.observe(old,obstacles=[self.obstacle()]);child=self.replan(changed)
        self.assertEqual(child['replanMode'],'changed_environment_search');self.assertEqual(self.poses(child),self.poses(changed))
        self.assertEqual(child['environmentRevision'],1);self.assertTrue(child['plannedPath']);self.assertEqual(child['obstacles'],changed['obstacles'])
        for v in child['participants'][:4]:child=self.act(child,'approve',v['vehicleId'])
        with self.assertRaises(ApiError):self.act(child,'start')
        child=self.act(child,'approve','NEW');done=self.act(child,'start')
        while done['status']=='running':self.now+=500;done=self.state()
        self.assertEqual(done['status'],'completed');self.assertTrue(all(v['progressPercent']==100 for v in done['participants']))

    def test_vehicle_pose_patch_keeps_other_cars_and_searches_from_changed_heading(self):
        old=self.running();pose={**self.poses(old)['NEW']};pose['y']+=.15;pose['yaw']+=.03
        changed=self.observe(old,[{'vehicleId':'NEW','pose':pose}]);self.assertEqual(self.poses(changed)['NEW'],pose)
        for i,p in self.poses(old).items():
            if i!='NEW':self.assertEqual(self.poses(changed)[i],p)
        child=self.replan(changed);self.assertEqual(self.poses(child),self.poses(changed));self.finish(child)

    def test_change_during_reconsent_invalidates_new_votes_and_blocks_old_requests(self):
        s=self.replan(self.observe(self.running(),obstacles=[self.obstacle()]));s=self.act(s,'approve','P-1')
        changed=self.observe(s,obstacles=[])
        self.assertTrue(all(v['decision']=='pending' for v in changed['participants']))
        with self.assertRaises(ApiError):self.act(s,'approve','P-2')
        child=self.replan(changed);self.assertEqual(child['environmentRevision'],2);self.assertEqual(child['replanGeneration'],2)

    def test_rejection_cannot_bypass_changed_scene_reconsent(self):
        child=self.replan(self.observe(self.running(),obstacles=[self.obstacle()]));rejected=self.act(child,'reject','P-2')
        with self.assertRaises(ApiError):self.act(rejected,'start')
        self.assertEqual(self.poses(self.state()),self.poses(rejected))

    def test_overlapping_observation_is_saved_but_replan_fails_without_reset(self):
        s=self.running();own=self.poses(s)['NEW'];changed=self.observe(s,obstacles=[self.obstacle(own['x'],own['y'],1,1)])
        with self.assertRaises(ApiError) as error:self.replan(changed)
        self.assertEqual(error.exception.status,422);self.assertEqual(self.poses(self.state()),self.poses(changed));self.assertEqual(self.state()['obstacles'],changed['obstacles'])
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM curb_sessions').fetchone()[0],1)

    def test_out_of_envelope_observation_is_retained_and_not_given_a_new_envelope(self):
        s=self.running();pose={**self.poses(s)['P-1'],'x':.3};changed=self.observe(s,[{'vehicleId':'P-1','pose':pose}])
        with self.assertRaises(ApiError):self.replan(changed)
        self.assertEqual(self.poses(self.state())['P-1'],pose)

    def test_lost_reply_replay_does_not_apply_pose_change_twice(self):
        s=self.running();patch=[{'vehicleId':'NEW','pose':{**self.poses(s)['NEW'],'y':4.8}}]
        changed=self.observe(s,patch,request='lost-change');replay=self.observe(s,patch,request='lost-change')
        self.assertEqual(replay,changed)
        with self.assertRaises(ApiError):self.observe(s,patch,request='other-stale')

    def test_restart_keeps_observed_geometry_and_reset_votes(self):
        changed=self.observe(self.running(),obstacles=[self.obstacle()])
        self.ledger=CurbSessions(self.path,lambda:self.now,lambda:self.template)
        restored=self.state();self.assertEqual(restored,changed);self.finish(self.replan(restored))

    def test_structurally_bad_observations_are_rejected_without_mutation(self):
        s=self.running();before=self.poses(s)
        for poses,obstacles in [([{'vehicleId':'UNKNOWN','pose':before['NEW']}],None),([{'vehicleId':[],'pose':before['NEW']}],None),
            ([{'vehicleId':'NEW','pose':{'x':float('nan'),'y':4,'yaw':0}}],None),
            (None,[self.obstacle(length=-1)]),(None,[self.obstacle(),self.obstacle()]),
            ([{'vehicleId':'NEW','pose':before['NEW']}]*2,None)]:
            with self.assertRaises(ApiError) as error:self.observe(s,poses,obstacles)
            self.assertEqual(error.exception.status,400)
            self.assertEqual(self.state()['revision'],s['revision']);self.assertEqual(self.poses(self.state()),before)

    def test_noop_completed_and_superseded_observations_cannot_reset_a_plan(self):
        s=self.running()
        with self.assertRaises(ApiError):self.observe(s,obstacles=[])
        stopped=self.act(s,'stop');child=self.replan(stopped)
        with self.assertRaises(ApiError):self.observe(stopped,obstacles=[self.obstacle()])
        done=self.finish(child)
        with self.assertRaises(ApiError):self.observe(done,obstacles=[self.obstacle()])

    def test_simultaneous_changes_have_one_revision_winner(self):
        s=self.running()
        def update(i):
            try:self.observe(s,obstacles=[self.obstacle(16+i*.05)]);return 200
            except ApiError as e:return e.status
        with ThreadPoolExecutor(2) as pool:self.assertEqual(sorted(pool.map(update,[0,1])),[200,409])

    def test_observation_during_route_calculation_rejects_the_stale_result(self):
        source=self.observe(self.running(),obstacles=[self.obstacle()])
        calculate=self.ledger._replan_template
        def changed_during_calculation(row):
            result=calculate(row)
            self.observe(source,obstacles=[])
            return result
        with patch.object(self.ledger,'_replan_template',side_effect=changed_during_calculation):
            with self.assertRaises(ApiError) as error:self.replan(source)
        self.assertEqual(error.exception.status,409)
        latest=self.state();self.assertEqual(latest['environmentRevision'],2);self.assertEqual(latest['status'],'stopped')
        self.assertEqual(latest['obstacles'],[]);self.assertEqual(self.poses(latest),self.poses(source))
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM curb_sessions').fetchone()[0],1)

    def test_actual_http_observation_endpoint_returns_stopped_geometry(self):
        server=Server(('127.0.0.1',0),Ledger(self.path,lambda:self.now));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            s=self.running();payload={'ownerVehicleId':'V-07','requestId':self.uid(),'revision':s['revision'],'obstacles':[self.obstacle()]}
            req=Request(f'http://127.0.0.1:{server.server_port}/v1/curb/sessions/{s["sessionId"]}/observations',json.dumps(payload).encode(),{'Content-Type':'application/json'})
            observed=json.load(urlopen(req))['session'];self.assertEqual(observed['status'],'stopped');self.assertEqual(observed['obstacles'],payload['obstacles'])
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
