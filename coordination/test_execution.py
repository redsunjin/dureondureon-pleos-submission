from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
import uuid

from coordination.errors import ApiError
from coordination.execution import ExecutionLedger,HEARTBEAT_MS,expected
from simulator.execution_trajectory import ValidatedTrajectory
from simulator.delegated_execution import VirtualVehicle
from simulator import curb_parking as cp


class ExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.trajectory=ValidatedTrajectory(cp.demo_scene())

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name);self.path=self.folder/'authority.db';self.now=100000
        self.ledger=ExecutionLedger(self.path,lambda:self.now);self.agents={};self.tokens={}
        source=self.trajectory.as_dict();self.costs={}
        for c in source['commands']:self.costs[c['vehicle']]=self.costs.get(c['vehicle'],0)+abs(c['distance'])
        for v,cost in self.costs.items():
            token=self.ledger.register_agent(v,self.uid())['token'];self.tokens[v]=token
            self.delegate(v,cost*4+1)
            self.agents[v]=VirtualVehicle(self.folder/(v+'.db'),v,token,self.ledger,source['initial'][v])
        self.pid=self.propose()

    def tearDown(self):self.tmp.cleanup()
    def uid(self):return str(uuid.uuid4())
    def delegate(self,v,total=None,**kw):
        p=dict(vehicle=v,owner='OWNER',zone='CURB',valid_from=self.now,valid_until=self.now+600000,per_plan=100,total=total or 100,forward=100,backward=100,yaw=100,request=self.uid());p.update(kw)
        return self.ledger.delegate(**p)
    def propose(self,**kw):return self.ledger.propose(self.trajectory,kw.get('zone','CURB'),self.now+300000,self.uid())['planId']
    def prepare(self):
        for a in self.agents.values():a.ready(self.pid)
    def begin(self):self.prepare();self.ledger.start(self.pid,self.uid());return self.dispatch()
    def dispatch(self):return self.ledger.dispatch(self.pid,self.uid())
    def p(self):return self.ledger.state(self.pid)
    def cmd(self):return self.p()['commands'][self.p()['index']]
    def report(self,b,kind,travel=0,pose=None,seq=1,request=None,**kw):
        c=b['command'];v=c['vehicle']
        args=dict(pid=self.pid,vehicle=v,token=self.tokens[v],plan_digest=b['planDigest'],version=b['grantVersion'],command_id=c['id'],sequence=c['sequence'],event_sequence=seq,kind=kind,travel=travel,pose=pose or expected(c,travel),request=request or self.uid());args.update(kw)
        return self.ledger.report(**args)
    def accept(self,b,request=None):
        c=b['command'];v=c['vehicle']
        return self.ledger.accept(self.pid,v,self.tokens[v],b['planDigest'],b['grantVersion'],c['id'],c['sequence'],request or self.uid())
    def heartbeats(self):
        for a in self.agents.values():a.heartbeat()

    def test_heartbeat_is_not_readiness_or_completion(self):
        self.heartbeats()
        with self.assertRaises(ApiError):self.ledger.start(self.pid,self.uid())
        self.assertEqual(self.p()['ready'],{})
        b=self.begin();self.now+=2000;self.heartbeats()
        self.assertEqual(self.p()['status'],'running');self.assertEqual(self.p()['index'],0)
        self.assertEqual(self.p()['commands'][0]['status'],'dispatched')

    def test_wall_clock_never_completes_and_timeout_is_unknown(self):
        b=self.begin();self.now+=40000
        s=self.p();self.assertEqual(s['status'],'stop_requested');self.assertEqual(s['motionKnowledge'],'unknown')
        self.assertEqual(s['index'],0)
        with self.assertRaises(ApiError):self.dispatch()
        self.assertGreater(self.ledger.state()['usage'][b['command']['vehicle']]['reserved'],0)

    def test_grant_change_invalidates_readiness(self):
        self.prepare();self.delegate(next(iter(self.agents)))
        self.assertEqual(self.p()['status'],'cancelled')
        self.assertEqual(self.p()['ready'],{})
        with self.assertRaises(ApiError):self.ledger.start(self.pid,self.uid())

    def test_revoke_inflight_requires_stop_confirmation(self):
        b=self.begin();v=b['command']['vehicle'];self.accept(b);self.report(b,'executing')
        self.ledger.revoke(v,'OWNER',self.uid())
        self.assertEqual(self.p()['status'],'stop_requested')
        before=self.ledger.state()['usage'][v]['reserved']
        self.assertGreater(before,0)
        self.report(b,'stopped',seq=2)
        self.assertEqual(self.p()['status'],'stopped')
        self.assertTrue(all(u['reserved']==0 for u in self.ledger.state()['usage'].values()))

    def test_expiry_during_execution_cannot_be_renewed_into_resume(self):
        v=next(iter(self.agents));self.delegate(v,valid_until=self.now+1000)
        # Old plan was invalidated; a new one is necessary even before execution.
        self.pid=self.propose();b=self.begin();self.now+=1001
        self.assertEqual(self.p()['status'],'stop_requested')
        self.delegate(v)
        self.assertEqual(self.p()['status'],'stop_requested')
        with self.assertRaises(ApiError):self.dispatch()

    def test_wrong_vehicle_digest_version_and_sequence_rejected(self):
        b=self.begin();c=b['command'];v=c['vehicle'];other=next(a for a in self.agents if a!=v)
        for token,h,version,sequence in [(self.tokens[other],b['planDigest'],b['grantVersion'],c['sequence']),
                (self.tokens[v],'0'*64,b['grantVersion'],c['sequence']),
                (self.tokens[v],b['planDigest'],b['grantVersion']+1,c['sequence']),
                (self.tokens[v],b['planDigest'],b['grantVersion'],c['sequence']+1)]:
            with self.assertRaises(ApiError):self.ledger.accept(self.pid,v,token,h,version,c['id'],sequence,self.uid())
        self.assertEqual(self.cmd()['status'],'dispatched')

    def test_acceptance_is_not_execution_and_completion_needs_evidence(self):
        b=self.begin();c=b['command'];self.accept(b)
        with self.assertRaises(ApiError):self.dispatch()
        with self.assertRaises(ApiError):self.report(b,'completed',abs(c['distance']))
        self.report(b,'executing')
        with self.assertRaises(ApiError):self.report(b,'completed',0,seq=2)
        bad=dict(c['target'],x=c['target']['x']+1)
        with self.assertRaises(ApiError):self.report(b,'completed',abs(c['distance']),pose=bad,seq=2)
        self.report(b,'completed',abs(c['distance']),seq=2)
        self.assertEqual(self.p()['index'],1)

    def test_progress_monotonic_and_reports_ordered(self):
        b=self.begin();self.accept(b);cost=abs(b['command']['distance'])
        self.report(b,'executing',cost*.5)
        with self.assertRaises(ApiError):self.report(b,'executing',cost*.3,seq=2)
        with self.assertRaises(ApiError):self.report(b,'executing',cost*.6,seq=3)
        with self.assertRaises(ApiError):self.report(b,'executing',cost*.6,seq=1)
        self.assertAlmostEqual(self.cmd()['travel'],cost*.5)
        self.report(b,'stopped',cost*.5,seq=2)
        usage=self.ledger.state()['usage'][b['command']['vehicle']]
        self.assertAlmostEqual(usage['used'],cost*.5);self.assertEqual(usage['reserved'],0)

    def test_partial_stop_pose_persists_after_restart(self):
        b=self.begin();self.accept(b);travel=abs(b['command']['distance'])*.4
        self.report(b,'executing',travel)
        self.ledger.stop(self.pid,self.uid());self.assertEqual(self.p()['motionKnowledge'],'unknown')
        self.report(b,'stopped',travel,seq=2)
        pose=self.ledger.state()['poses'][b['command']['vehicle']]
        self.ledger=ExecutionLedger(self.path,lambda:self.now)
        self.assertEqual(self.ledger.state()['poses'][b['command']['vehicle']],pose)
        self.assertEqual(self.p()['status'],'stopped')

    def test_duplicate_receipts_and_virtual_restart_do_not_repeat_motion(self):
        b=self.begin();v=b['command']['vehicle'];a=self.agents[v]
        a.execute(b);first=a.local_state();usage=self.ledger.state()['usage'][v]['used']
        a=VirtualVehicle(self.folder/(v+'.db'),v,self.tokens[v],self.ledger,b['command']['source'])
        a.execute(b)
        self.assertEqual(a.local_state(),first);self.assertEqual(self.ledger.state()['usage'][v]['used'],usage)

    def test_lost_completion_reserves_budget_and_stop_ack_recovers_actual_pose(self):
        b=self.begin();v=b['command']['vehicle'];a=self.agents[v]
        a.execute(b,deliver_completion=False);actual=a.local_state()['pose']
        self.now+=40000;self.assertEqual(self.p()['status'],'stop_requested')
        self.assertNotEqual(self.ledger.state()['poses'][v],actual)
        a=VirtualVehicle(self.folder/(v+'.db'),v,self.tokens[v],self.ledger,b['command']['source'])
        a.confirm_stop(b)
        self.assertEqual(self.ledger.state()['poses'][v],actual)
        self.assertEqual(self.p()['status'],'stopped');self.assertEqual(a.local_state()['executedCommands'],1)

    def test_late_completion_does_not_replace_stop_confirmation(self):
        b=self.begin();v=b['command']['vehicle'];a=self.agents[v]
        a.execute(b,deliver_completion=False);self.ledger.stop(self.pid,self.uid())
        a.execute(b)
        self.assertEqual(self.p()['status'],'stop_requested')
        with self.assertRaises(ApiError):self.dispatch()
        usage=self.ledger.state()['usage'][v]['used'];a.confirm_stop(b)
        self.assertEqual(self.ledger.state()['usage'][v]['used'],usage)
        self.assertEqual(self.p()['status'],'stopped')

    def test_coordinator_restart_invalidates_cached_acceptance_before_effect(self):
        b=self.begin();v=b['command']['vehicle'];c=b['command']
        self.accept(b,'accept_'+c['id']);self.report(b,'executing',travel=0.0,request='moving_'+c['id'])
        self.ledger=ExecutionLedger(self.path,lambda:self.now);a=self.agents[v];a.ledger=self.ledger
        with self.assertRaises(ValueError):a.execute(b)
        self.assertEqual(a.local_state()['executedCommands'],0)
        self.assertEqual(self.p()['status'],'stop_requested');a.confirm_stop(b)
        self.assertEqual(self.p()['status'],'stopped')

    def test_budget_reservation_is_atomic_and_survives_cancel(self):
        initial=deepcopy(self.ledger.state()['usage'])
        def other(_):
            try:self.propose();return 200
            except ApiError as e:return e.status
        with ThreadPoolExecutor(2) as pool:self.assertEqual(list(pool.map(other,range(2))),[409,409])
        self.assertEqual(self.ledger.state()['usage'],initial)
        self.ledger.stop(self.pid,self.uid())
        self.assertTrue(all(u['reserved']==0 for u in self.ledger.state()['usage'].values()))
        self.assertTrue(all(u['used']==0 for u in self.ledger.state()['usage'].values()))

    def test_used_budget_cannot_reset_with_new_delegation(self):
        b=self.begin();v=b['command']['vehicle'];self.agents[v].execute(b)
        self.ledger.stop(self.pid,self.uid());self.assertEqual(self.p()['motionKnowledge'],'reported_stationary');used=self.ledger.state()['usage'][v]['used']
        self.delegate(v,total=used)
        self.assertEqual(self.ledger.state()['usage'][v]['used'],used)
        # Same initial scene must also be rejected after movement, never silently reset.
        with self.assertRaises(ApiError):self.propose()

    def test_narrow_zone_direction_and_distance_grants_reject_plan(self):
        self.ledger.stop(self.pid,self.uid());v=next(iter(self.agents))
        for kw in [dict(zone='OTHER'),dict(per_plan=.01),dict(total=.01),dict(forward=0,backward=0)]:
            self.delegate(v,**kw)
            with self.assertRaises(ApiError):self.propose()
            self.assertTrue(all(u['reserved']==0 for u in self.ledger.state()['usage'].values()))
            self.delegate(v)

    def test_request_mutation_is_not_a_duplicate(self):
        rid=self.uid();self.ledger.heartbeat(next(iter(self.agents)),self.tokens[next(iter(self.agents))],rid)
        with self.assertRaises(ApiError):self.ledger.start(self.pid,rid)

    def test_timeout_commits_even_when_dispatch_fails(self):
        self.begin();self.now+=40000
        with self.assertRaises(ApiError):self.dispatch()
        self.assertEqual(self.p()['status'],'stop_requested')

    def test_full_simulator_trajectory_requires_explicit_vehicle_completions(self):
        self.prepare();self.ledger.start(self.pid,self.uid())
        while self.p()['status']=='running':
            self.heartbeats();b=self.dispatch();self.agents[b['command']['vehicle']].execute(b);self.now+=100
        p=self.p();self.assertEqual(p['status'],'completed')
        self.assertEqual(sum(a.local_state()['executedCommands'] for a in self.agents.values()),len(p['commands']))
        s=self.ledger.state()
        for v,target in self.trajectory.as_dict()['finalPoses'].items():
            for axis in target:self.assertAlmostEqual(s['poses'][v][axis],target[axis])
        self.assertTrue(all(u['reserved']==0 for u in s['usage'].values()))
        for v,cost in self.costs.items():self.assertAlmostEqual(s['usage'][v]['used'],cost)

    def test_fixed_vehicle_needs_no_agent_or_delegation_and_never_moves(self):
        self.ledger.stop(self.pid,self.uid())
        scene=cp.demo_scene();fixed=cp.Car('FIXED',cp.Pose(40,1.5),consent=False,max_backward=0,max_forward=0)
        trajectory=ValidatedTrajectory(replace(scene,length=50,parked=scene.parked+(fixed,)))
        self.pid=self.ledger.propose(trajectory,'CURB',self.now+300000,self.uid())['planId']
        p=self.p();self.assertNotIn('FIXED',p['versions'])
        for v in p['versions']:self.agents[v].ready(self.pid)
        self.ledger.start(self.pid,self.uid())
        while self.p()['status']=='running':
            self.heartbeats();b=self.dispatch();self.agents[b['command']['vehicle']].execute(b)
            self.assertEqual(self.ledger.state()['poses']['FIXED'],dict(x=40,y=1.5,yaw=0))
        self.assertEqual(self.p()['status'],'completed')

    def test_two_plans_racing_reserve_once(self):
        self.ledger.stop(self.pid,self.uid())
        def create(_):
            try:return (200,self.propose())
            except ApiError as e:return (e.status,None)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(create,range(2)))
        self.assertEqual(sorted(r[0] for r in results),[200,409])
        for v,cost in self.costs.items():self.assertAlmostEqual(self.ledger.state()['usage'][v]['reserved'],cost)

    def test_duplicate_commands_reaching_two_executor_instances_move_once(self):
        b=self.begin();v=b['command']['vehicle']
        agents=[VirtualVehicle(self.folder/(v+'.db'),v,self.tokens[v],self.ledger,b['command']['source']) for _ in range(2)]
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda a:a.execute(b),agents))
        self.assertEqual(agents[0].local_state()['executedCommands'],1)
        self.assertAlmostEqual(self.ledger.state()['usage'][v]['used'],abs(b['command']['distance']))

    def test_stop_confirmation_releases_lock_but_old_reports_stay_old(self):
        b=self.begin();self.accept(b);self.report(b,'executing');self.ledger.stop(self.pid,self.uid())
        with self.assertRaises(ApiError):self.propose()
        self.report(b,'stopped',seq=2)
        old=self.pid;self.pid=self.propose()
        self.assertNotEqual(self.pid,old)
        c=b['command'];v=c['vehicle']
        with self.assertRaises(ApiError):self.ledger.report(old,v,self.tokens[v],b['planDigest'],b['grantVersion'],c['id'],c['sequence'],3,'completed',abs(c['distance']),c['target'],self.uid())
        self.assertEqual(self.p()['status'],'awaiting_ready')

    def test_executor_restart_before_effect_retries_only_current_permitted_command(self):
        b=self.begin();c=b['command'];v=c['vehicle']
        self.accept(b,'accept_'+c['id']);self.report(b,'executing',travel=0.0,request='moving_'+c['id'])
        a=VirtualVehicle(self.folder/(v+'.db'),v,self.tokens[v],self.ledger,c['source'])
        self.assertEqual(a.local_state()['executedCommands'],0)
        a.execute(b);a.execute(b)
        self.assertEqual(a.local_state()['executedCommands'],1)

    def test_new_plan_cannot_spend_used_distance_after_grant_renewal(self):
        b=self.begin();v=b['command']['vehicle'];self.agents[v].execute(b);self.ledger.stop(self.pid,self.uid())
        state=self.ledger.state();used=state['usage'][v]['used'];self.delegate(v,total=used)
        scene=cp.demo_scene()
        def moved(car):return replace(car,pose=cp.Pose(**state['poses'][car.id]))
        trajectory=ValidatedTrajectory(replace(scene,parked=tuple(moved(c) for c in scene.parked),incoming=moved(scene.incoming)))
        with self.assertRaises(ApiError) as error:self.ledger.propose(trajectory,'CURB',self.now+300000,self.uid())
        self.assertEqual(error.exception.code,'delegation_budget_exceeded')
        self.assertEqual(self.ledger.state()['usage'][v]['used'],used)

    def test_read_snapshots_cannot_mutate_authority(self):
        snapshot=self.ledger.state();snapshot['grants'].clear();snapshot['plans'][self.pid]['status']='completed'
        self.assertEqual(self.p()['status'],'awaiting_ready');self.assertEqual(len(self.ledger.state()['grants']),len(self.agents))
