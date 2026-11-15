from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from coordination.server import ApiError, Ledger, Server, TTL_MS


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.now = 1_000_000
        self.ledger = Ledger(self.path, lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, vehicle='V-01', space='A-14', request='request-1'):
        return self.ledger.reserve(vehicle, space, request)

    def test_candidates_reflect_server_reservation(self):
        result = self.reserve()
        self.assertEqual(result['reservation']['status'], 'active')
        state = self.ledger.state('V-02')
        self.assertFalse(next(c for c in state['candidates'] if c['spaceId'] == 'A-14')['available'])

    def test_candidate_coordinates_follow_reserved_space_and_survive_restart(self):
        state = self.reserve(space='B-03')
        target = next(c for c in state['candidates'] if c['spaceId'] == state['reservation']['spaceId'])
        self.assertEqual(target['mapPosition'], {'latitude': 37.484291, 'longitude': 127.033019, 'source': 'demo'})
        self.assertEqual(target['status'], 'reserved')
        restarted = Ledger(self.path, lambda: self.now)
        self.assertEqual(state, restarted.state('V-01'))

    def test_legacy_database_migrates_without_changing_booking_or_verified_coordinates(self):
        reservation = self.reserve()['reservation']
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE spaces SET latitude=37.5,longitude=127.1,coordinate_source='facility' WHERE id='B-03'")
        restarted = Ledger(self.path, lambda: self.now)
        self.assertEqual(reservation, restarted.state('V-01')['reservation'])
        position = next(c for c in restarted.state('V-01')['candidates'] if c['spaceId'] == 'B-03')['mapPosition']
        self.assertEqual(position['source'], 'facility')
        self.assertEqual(position['latitude'], 37.5)
        legacy = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE spaces(id TEXT PRIMARY KEY,reason TEXT NOT NULL,eta INTEGER NOT NULL,occupied INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO spaces VALUES('C-21','existing occupied space',42,1)")
        migrated = Ledger(legacy, lambda: self.now).state('V-01')
        occupied = next(c for c in migrated['candidates'] if c['spaceId'] == 'C-21')
        self.assertEqual(occupied['status'], 'occupied')
        self.assertEqual(occupied['mapPosition']['source'], 'demo')
        self.assertEqual(occupied['etaSeconds'], 42)

    def test_ten_concurrent_requests_have_one_winner(self):
        barrier = threading.Barrier(10)
        def claim(i):
            barrier.wait()
            try:
                self.reserve(f'V-{i}', request=f'request-{i}')
                return 200
            except ApiError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(claim, range(10)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 9)

    def test_concurrent_replays_issue_only_one_reservation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.reserve()['reservation'], range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event='reserved'").fetchone()[0], 1)

    def test_one_vehicle_cannot_reserve_two_spaces(self):
        self.reserve()
        with self.assertRaises(ApiError) as context:
            self.reserve(space='B-03', request='different')
        self.assertEqual(context.exception.status, 409)

    def test_expiry_is_exact_and_releases_the_space(self):
        original = self.reserve()['reservation']
        self.now += TTL_MS - 1
        self.assertEqual(self.ledger.state('V-01')['reservation']['status'], 'active')
        self.now += 1
        self.assertEqual(self.ledger.state('V-01')['reservation']['status'], 'expired')
        other = self.reserve('V-02', request='request-2')['reservation']
        self.assertNotEqual(original['reservationId'], other['reservationId'])

    def test_replay_after_expiry_does_not_renew(self):
        original = self.reserve()['reservation']
        self.now += TTL_MS + 1
        replay = self.reserve()['reservation']
        self.assertEqual(replay['reservationId'], original['reservationId'])
        self.assertEqual(replay['expiresAtMillis'], original['expiresAtMillis'])
        self.assertEqual(replay['status'], 'expired')

    def test_restart_and_successful_request_replay_preserve_original_deadline(self):
        original = self.reserve()['reservation']
        self.now += 10_000
        restarted = Ledger(self.path, lambda: self.now)
        restored = restarted.reserve('V-01', 'A-14', 'request-1')['reservation']
        self.assertEqual(original, restored)

    def test_request_id_cannot_change_vehicle_or_space(self):
        self.reserve()
        for vehicle, space in [('V-02', 'A-14'), ('V-01', 'B-03')]:
            with self.subTest(vehicle=vehicle, space=space), self.assertRaises(ApiError):
                self.reserve(vehicle, space)

    def test_cancel_is_idempotent_and_allows_rebooking(self):
        rid = self.reserve()['reservation']['reservationId']
        for _ in range(2):
            self.assertEqual(self.ledger.transition('V-01', rid, 'cancel')['reservation']['status'], 'cancelled')
        self.assertEqual(self.reserve('V-02', request='request-2')['reservation']['status'], 'active')

    def test_arrival_persists_occupancy_after_lease_deadline(self):
        rid = self.reserve()['reservation']['reservationId']
        self.ledger.transition('V-01', rid, 'arrival')
        self.now += TTL_MS * 2
        with self.assertRaises(ApiError) as context:
            self.reserve('V-02', request='request-2')
        self.assertEqual(context.exception.code, 'space_occupied')
        self.assertEqual(self.ledger.transition('V-01', rid, 'arrival')['reservation']['status'], 'arrived')

    def test_expired_reservation_cannot_arrive_or_cancel(self):
        rid = self.reserve()['reservation']['reservationId']
        self.now += TTL_MS
        for action in ('arrival', 'cancel'):
            with self.subTest(action=action), self.assertRaises(ApiError):
                self.ledger.transition('V-01', rid, action)

    def test_other_vehicle_cannot_mutate_reservation(self):
        rid = self.reserve()['reservation']['reservationId']
        with self.assertRaises(ApiError) as context:
            self.ledger.transition('V-02', rid, 'cancel')
        self.assertEqual(context.exception.status, 404)

    def test_expiration_event_is_recorded_once(self):
        self.reserve()
        self.now += TTL_MS
        self.ledger.state('V-01')
        self.ledger.state('V-01')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event='expired'").fetchone()[0], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(('127.0.0.1', 0), Ledger(Path(self.temp.name) / 'http.sqlite3'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def call(self, path, data=None):
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_http_reserve_restore_cancel_flow(self):
        self.assertEqual(self.call('/health')['status'], 'ok')
        data = {'vehicleId': 'V-07', 'spaceId': 'A-14', 'requestId': 'http-request'}
        reserved = self.call('/v1/reservations', data)['reservation']
        restored = self.call('/v1/state?vehicleId=V-07')['reservation']
        self.assertEqual(reserved, restored)
        cancelled = self.call(f"/v1/reservations/{reserved['reservationId']}/cancel", {'vehicleId': 'V-07'})
        self.assertEqual(cancelled['reservation']['status'], 'cancelled')

    def test_http_rejects_invalid_input_and_conflict(self):
        with self.assertRaises(HTTPError) as context:
            self.call('/v1/reservations', {'vehicleId': None})
        self.assertEqual(context.exception.code, 400)
        self.call('/v1/reservations', {'vehicleId': 'V-01', 'spaceId': 'A-14', 'requestId': 'r1'})
        with self.assertRaises(HTTPError) as context:
            self.call('/v1/reservations', {'vehicleId': 'V-02', 'spaceId': 'A-14', 'requestId': 'r2'})
        self.assertEqual(context.exception.code, 409)


if __name__ == '__main__':
    unittest.main()
