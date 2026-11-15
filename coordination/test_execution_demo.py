import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import uuid

from coordination.execution_demo import ExecutionLab,LabServer
from simulator.execution_trajectory import ValidatedTrajectory
from simulator import curb_parking as cp
from coordination.errors import ApiError

class LabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.trajectory=ValidatedTrajectory(cp.demo_scene())
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.lab=ExecutionLab(self.tmp.name,self.trajectory)
    def tearDown(self):self.tmp.cleanup()
    def body(self,action):
        s=self.lab.snapshot();return dict(action=action,requestId=str(uuid.uuid4()),serverEpoch=s['serverEpoch'],demoId=s['demoId'],revision=s['revision'])
    def act(self,action):return self.lab.action(self.body(action))

    def test_explicit_steps_are_not_clock_playback(self):
        first=self.lab.snapshot();self.assertEqual(first['plan']['status'],'ready')
        for _ in range(3):self.assertEqual(self.lab.snapshot()['poses'],first['poses'])
        s=self.act('next');self.assertEqual(s['plan']['motionKnowledge'],'unknown');self.assertEqual(s['plan']['index'],0)
        self.assertEqual(s['poses'],first['poses'])
        s=self.act('next');self.assertEqual(s['plan']['index'],1);self.assertNotEqual(s['poses'],first['poses'])
        while s['plan']['status']=='running':s=self.act('next')
        self.assertEqual(s['plan']['status'],'completed');self.assertEqual(s['plan']['index'],15)

    def test_risk_clear_and_stop_confirmation_are_distinct(self):
        self.act('next');risk=self.act('risk')
        self.assertEqual(risk['plan']['status'],'stop_requested');self.assertEqual(risk['plan']['trafficSafety']['safety'],'risk')
        clear=self.act('clear');self.assertEqual(clear['plan']['trafficSafety']['safety'],'clear')
        self.assertEqual(clear['plan']['status'],'stop_requested')
        with self.assertRaises(ApiError):self.act('next')
        stopped=self.act('confirm-stop');self.assertEqual(stopped['plan']['status'],'stopped')
        self.assertTrue(all(u['reserved']==0 for u in stopped['usage'].values()))

    def test_stale_empty_observation_is_not_hidden_by_heartbeat(self):
        self.act('next');s=self.act('stale')
        self.assertEqual(s['plan']['reason'],'traffic_stale');self.assertEqual(s['plan']['motionKnowledge'],'unknown')

    def test_request_deduplication_and_cross_tab_revision(self):
        body=self.body('next');first=self.lab.action(body);again=self.lab.action(body)
        self.assertEqual(first['revision'],again['revision']);self.assertTrue(again['receiptReplayed'])
        body['requestId']=str(uuid.uuid4())
        with self.assertRaises(ApiError):self.lab.action(body)

    def test_reset_does_not_reinterpret_old_pending_actions(self):
        old=self.body('next');before=self.lab.snapshot();new=self.act('reset')
        self.assertNotEqual(new['demoId'],before['demoId'])
        with self.assertRaises(ApiError):self.lab.action(old)
        old=self.body('next');old['serverEpoch']='old-process'
        with self.assertRaises(ApiError):self.lab.action(old)

    def test_public_snapshot_has_no_vehicle_credentials(self):
        snapshot=json.dumps(self.lab.snapshot())
        for vehicle in self.lab.vehicles.values():self.assertNotIn(vehicle.token,snapshot)
        self.assertNotIn('agents',self.lab.snapshot());self.assertNotIn('receipts',self.lab.snapshot())

class LabHttpTests(unittest.TestCase):
    def test_http_controls_and_origin_asset_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            server=LabServer(('127.0.0.1',0),ExecutionLab(folder));worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/api/state') as r:s=json.load(r);self.assertEqual(r.headers['Cache-Control'],'no-store')
                body=dict(action='next',requestId=str(uuid.uuid4()),serverEpoch=s['serverEpoch'],demoId=s['demoId'],revision=s['revision'])
                for _ in range(2):
                    with urlopen(Request(base+'/api/actions',json.dumps(body).encode(),{'Content-Type':'application/json'})) as r:self.assertEqual(json.load(r)['plan']['index'],0)
                for path in ('/','/execution-lab.js','/execution-lab.css'):
                    with urlopen(base+path) as r:self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
                for headers,expected in (({'Origin':'https://other.example','Content-Type':'application/json'},403),({'Content-Type':'text/plain'},415)):
                    with self.assertRaises(HTTPError) as e:urlopen(Request(base+'/api/actions',json.dumps(body).encode(),headers))
                    self.assertEqual(e.exception.code,expected);e.exception.close()
                with self.assertRaises(HTTPError) as e:urlopen(base+'/../execution.py')
                self.assertEqual(e.exception.code,404);e.exception.close()
            finally:server.shutdown();server.server_close();worker.join()
