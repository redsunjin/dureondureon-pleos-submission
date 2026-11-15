"""HTTP boundary regressions for the local simulation authority."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from coordination.server import Ledger, Server


class LoopbackBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(('127.0.0.1', 0), Ledger(Path(self.temp.name) / 'state.db'))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(); self.temp.cleanup()

    def call(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        hdrs = {'Content-Type': 'application/json'} if payload is not None else {}
        hdrs.update(headers or {})
        try:
            conn.request(method, path, None if payload is None else json.dumps(payload).encode(), hdrs)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_native_reservation_restore_cancel(self):
        status, data = self.call('POST', '/v1/reservations', dict(vehicleId='NATIVE', spaceId='A-14', requestId='reserve'))
        self.assertEqual(status, 200)
        reservation = data['reservation']
        self.assertEqual(self.call('GET', '/v1/state?vehicleId=NATIVE')[1]['reservation'], reservation)
        status, data = self.call('POST', '/v1/reservations/' + reservation['reservationId'] + '/cancel', dict(vehicleId='NATIVE'))
        self.assertEqual((status, data['reservation']['status']), (200, 'cancelled'))

    def test_native_and_same_origin_parameterized_json(self):
        status, data = self.call('POST', '/v1/curb/sessions', dict(ownerVehicleId='NATIVE', requestId='create'))
        self.assertEqual(status, 200)
        self.assertEqual(self.call('GET', '/v1/curb/state?ownerVehicleId=NATIVE')[1]['session']['sessionId'], data['session']['sessionId'])
        status, data = self.call('POST', '/v1/reservations', dict(vehicleId='BROWSER', spaceId='A-14', requestId='browser'),
                                 {'Origin': 'http://' + self.host, 'Content-Type': 'Application/JSON; charset=utf-8'})
        self.assertEqual((status, data['reservation']['status']), (200, 'active'))

    def test_plain_text_cannot_allocate_session(self):
        status, _ = self.call('POST', '/v1/curb/sessions', dict(ownerVehicleId='UNTRUSTED', requestId='plain'), {'Content-Type': 'text/plain'})
        self.assertEqual(status, 415)
        self.assertIsNone(self.call('GET', '/v1/curb/state?ownerVehicleId=UNTRUSTED')[1]['session'])

    def test_foreign_origin_cannot_allocate_reservation(self):
        status, _ = self.call('POST', '/v1/reservations', dict(vehicleId='UNTRUSTED', spaceId='A-14', requestId='foreign'), {'Origin': 'https://untrusted.example'})
        self.assertEqual(status, 403)
        self.assertIsNone(self.call('GET', '/v1/state?vehicleId=UNTRUSTED')[1]['reservation'])

    def test_core_reads_reject_foreign_host_origin_and_null(self):
        for path in ['/v1/state?vehicleId=X', '/v1/curb/state?ownerVehicleId=X', '/health']:
            for headers in [{'Host': 'untrusted.example'}, {'Origin': 'https://untrusted.example'}, {'Origin': 'null'}]:
                with self.subTest(path=path, headers=headers):
                    self.assertEqual(self.call('GET', path, headers=headers)[0], 403)

    def test_owner_routes_and_parser_aliases_share_boundary(self):
        paths = ['/v1/curb/sessions', '/v1/curb/sessions/unused/actions', '/v1/curb/sessions/unused/observations',
                 '/v1/curb/sessions/unused/traffic', '/v1/curb/sessions/unused/replan', '/v1/reservations',
                 '/v1/reservations/unused/cancel', '/v1/reservations/unused/arrival']
        for path in paths:
            for alias in [path, path + '?probe=1', path + ';probe=1', 'http://untrusted.example' + path]:
                with self.subTest(path=alias):
                    self.assertEqual(self.call('POST', alias, {}, {'Origin': 'https://untrusted.example'})[0], 403)

    def test_browser_simple_media_and_foreign_host_rejected(self):
        for content_type in ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data; boundary=test']:
            with self.subTest(content_type=content_type):
                self.assertEqual(self.call('POST', '/v1/reservations', {}, {'Content-Type': content_type})[0], 415)
        self.assertEqual(self.call('POST', '/v1/curb/sessions', {}, {'Host': 'untrusted.example'})[0], 403)


if __name__ == '__main__':
    unittest.main()
