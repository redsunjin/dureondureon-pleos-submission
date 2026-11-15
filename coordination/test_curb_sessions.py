from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from coordination.curb_sessions import CurbSessions, APPROVAL_MS, HEARTBEAT_MS, build_template
from coordination.errors import ApiError
from coordination.server import Ledger, Server


class CurbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = build_template()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'curb.db'
        self.now = 100_000
        self.ledger = CurbSessions(self.path, lambda: self.now, lambda: self.template)
        self.sequence = 0

    def tearDown(self):
        self.temp.cleanup()

    def create(self, request='create-1', owner='V-07'):
        return self.ledger.create(owner, request)['session']

    def act(self, s, action, participant=None, request=None, **kw):
        self.sequence += 1
        return self.ledger.act(s['sessionId'], 'V-07', request or f'act-{self.sequence}',
            s['revision'], action, participant, f"{s['sessionId']}_{participant}" if participant else None, **kw)['session']

    def ready(self):
        s = self.create()
        for p in s['participants']:
            s = self.act(s, 'approve', p['vehicleId'])
        return s

    def test_requests_are_real_simulator_steps_and_initial_pose(self):
        s = self.create()
        self.assertTrue(s['demo'])
        self.assertEqual(len(s['participants']), 5)
        self.assertAlmostEqual(s['openingGap'], 7.89)
        for c in s['participants']:
            self.assertEqual(c['decision'], 'pending')
            self.assertEqual(c['pose'], c['initialPose'])
            self.assertEqual(c['progressPercent'], 0)
            self.assertGreater(c['totalTravel'], 0)
        rear = next(c for c in s['participants'] if c['vehicleId'] == 'P-2')
        self.assertEqual([x['stage'] for x in rear['steps']], ['create_space','settle_spacing'])
        self.assertLess(rear['steps'][0]['distance'], 0)
        self.assertGreater(rear['steps'][1]['distance'], 0)

    def test_all_five_consents_then_explicit_start_required(self):
        s = self.create()
        with self.assertRaises(ApiError):
            self.act(s, 'start')
        for p in s['participants'][:-1]:
            s = self.act(s, 'approve', p['vehicleId'])
            self.assertEqual(s['status'], 'awaiting')
        s = self.act(s, 'approve', 'NEW')
        self.assertEqual(s['status'], 'ready')
        self.now += 1000
        s = self.ledger.state('V-07')['session']
        self.assertEqual(s['elapsedSeconds'], 0)
        self.assertEqual(self.act(s, 'start')['status'], 'running')

    def test_refusal_prevents_motion_and_allows_fresh_plan(self):
        s = self.act(self.create(), 'reject', 'P-3')
        self.assertEqual(s['status'], 'rejected')
        self.now += 2000
        self.assertEqual(self.ledger.state('V-07')['session']['elapsedSeconds'], 0)
        fresh = self.create('create-2')
        self.assertNotEqual(s['sessionId'], fresh['sessionId'])
        self.assertTrue(all(c['decision'] == 'pending' for c in fresh['participants']))

    def test_progress_and_final_pose_are_server_selected_frames(self):
        s = self.act(self.ready(), 'start')
        self.now += 500
        s = self.ledger.state('V-07')['session']
        self.assertGreater(s['elapsedSeconds'], 1.9)
        self.assertEqual(s['actor'], 'P-1')
        self.assertGreater(s['participants'][0]['progressPercent'], 0)
        self.assertLess(s['participants'][0]['progressPercent'], 100)
        for _ in range(50):
            self.now += 500
            s = self.ledger.state('V-07')['session']
        self.assertEqual(s['status'], 'completed')
        self.assertTrue(all(c['motionState'] == 'complete' and c['progressPercent']==100 for c in s['participants']))
        self.assertAlmostEqual(s['elapsedSeconds'], self.template['duration'])
        self.assertEqual({c['vehicleId']: c['pose'] for c in s['participants']}, self.template['frames'][-1]['poses'])
        poses = {c['vehicleId']: c['pose'] for c in s['participants']}
        self.assertAlmostEqual(poses['NEW']['x']-2.4-poses['P-2']['x']-2.3, 0.42)
        self.assertAlmostEqual(poses['P-3']['x']-2.3-poses['NEW']['x']-2.4, 0.42)

    def test_withdrawal_during_motion_freezes_all_participants(self):
        s = self.act(self.ready(), 'start')
        self.now += 400
        s = self.act(s, 'withdraw', 'P-2')
        poses = [p['pose'] for p in s['participants']]
        self.assertEqual(s['reason'], 'consent_withdrawn')
        self.assertNotEqual(poses[0], s['participants'][0]['initialPose'])
        self.now += 60_000
        later = self.ledger.state('V-07')['session']
        self.assertEqual([p['pose'] for p in later['participants']], poses)
        with self.assertRaises(ApiError):
            self.act(later, 'start')

    def test_heartbeat_loss_never_catches_up_across_gap(self):
        s = self.act(self.ready(), 'start')
        self.now += 500
        s = self.ledger.state('V-07')['session']
        before = s['elapsedSeconds']
        self.now += HEARTBEAT_MS+1
        s = self.ledger.state('V-07')['session']
        self.assertEqual(s['reason'], 'connection_lost')
        self.assertEqual(s['elapsedSeconds'], before)

    def test_server_restart_stops_in_place_and_retains_session_identity(self):
        s = self.act(self.ready(), 'start')
        self.now += 500
        before = self.ledger.state('V-07')['session']
        restarted = CurbSessions(self.path, lambda: self.now, lambda: self.template)
        after = restarted.state('V-07')['session']
        self.assertEqual(after['sessionId'], before['sessionId'])
        self.assertEqual(after['elapsedSeconds'], before['elapsedSeconds'])
        self.assertEqual(after['reason'], 'server_restarted')

    def test_clock_rollback_and_approval_expiry_do_not_run(self):
        s = self.create()
        self.now += APPROVAL_MS
        self.assertEqual(self.ledger.state('V-07')['session']['status'], 'expired')
        s = self.create('create-2')
        for p in s['participants']:
            s = self.act(s, 'approve', p['vehicleId'])
        s = self.act(s, 'start')
        self.now -= 1
        self.assertEqual(self.ledger.state('V-07')['session']['status'], 'stopped')

    def test_repeated_create_and_consent_are_idempotent_and_key_reuse_rejected(self):
        s = self.create()
        self.assertEqual(s, self.create())
        approved = self.act(s, 'approve', 'P-1', request='same-action')
        replay = self.act(s, 'approve', 'P-1', request='same-action')
        self.assertEqual(approved, replay)
        with self.assertRaises(ApiError) as error:
            self.act(s, 'reject', 'P-1', request='same-action')
        self.assertEqual(error.exception.code, 'request_id_reused')

    def test_stale_revision_wrong_request_and_invalid_participant_are_rejected(self):
        s = self.create()
        self.act(s, 'approve', 'P-1')
        with self.assertRaises(ApiError) as error:
            self.act(s, 'approve', 'P-2')
        self.assertEqual(error.exception.code, 'curb_revision_changed')
        current = self.ledger.state('V-07')['session']
        with self.assertRaises(ApiError):
            self.ledger.act(s['sessionId'], 'V-07', 'bad', current['revision'], 'approve', 'P-2', 'other-message')
        with self.assertRaises(ApiError):
            self.act(current, 'approve', {'bad': 'type'})

    def test_stop_is_terminal_and_replayed_start_does_not_resume(self):
        s = self.ready()
        running = self.act(s, 'start', request='start-1')
        stopped = self.act(running, 'stop')
        replay = self.act(s, 'start', request='start-1')
        self.assertEqual(replay, stopped)

    def test_two_clients_cannot_hold_the_same_curb(self):
        gate = threading.Barrier(2)
        def create(owner):
            gate.wait()
            try:
                self.ledger.create(owner, owner)
                return 200
            except ApiError as error:
                return error.status
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(create, ['V-07','V-08'])), [200,409])


class CurbHttpTests(unittest.TestCase):
    def test_http_plan_consent_refusal_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            server = Server(('127.0.0.1',0), Ledger(Path(folder)/'state.db'))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def call(path, body=None):
                request = Request(base+path, data=json.dumps(body).encode() if body else None,
                                  headers={'Content-Type':'application/json'})
                with urlopen(request) as response:
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    return json.load(response)
            try:
                s = call('/v1/curb/sessions', {'ownerVehicleId':'V-07','requestId':'http-create'})['session']
                p = s['participants'][0]
                s = call(f"/v1/curb/sessions/{s['sessionId']}/actions", {'ownerVehicleId':'V-07',
                    'requestId':'http-reject','revision':s['revision'],'action':'reject',
                    'participantId':p['vehicleId'],'moveRequestId':p['moveRequestId']})['session']
                self.assertEqual(s['status'], 'rejected')
                self.assertEqual(call('/v1/curb/state?ownerVehicleId=V-07')['session']['sessionId'],s['sessionId'])
                with self.assertRaises(HTTPError) as error:
                    call(f"/v1/curb/sessions/{s['sessionId']}/actions", {'ownerVehicleId':'V-07','requestId':'invalid','revision':True,'action':'start'})
                self.assertEqual(error.exception.code,400)
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
