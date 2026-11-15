"""SQLite-backed local parking authority. Run: python3 -m coordination.server.

Binds to loopback only. Synthetic vehicle IDs are identifiers, NOT authentication.
No vehicle control, physical occupancy sensing, or collision-free entry passes.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from urllib.parse import parse_qs, urlparse
import uuid

TTL_MS = 45_000
SPACES = (
    ("A-14", "입구와 가까운 후보", 60),
    ("B-03", "중앙 구역 후보", 120),
    ("C-21", "출차 방향을 고려한 후보", 180),
)
# Synthetic entrance points on public roads, solely for the development demo.
# Real facilities must replace these with verified entrance coordinates/source.
DEMO_POSITIONS = {
    "A-14": (37.48544722, 127.03636666),
    "B-03": (37.484291, 127.033019),
    "C-21": (37.483147, 127.031240),
}
from coordination.errors import ApiError, identifier


class Ledger:
    def __init__(self, path: str | Path, clock=None):
        self.path = str(path)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS spaces (
                    id TEXT PRIMARY KEY, reason TEXT NOT NULL, eta INTEGER NOT NULL,
                    occupied INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reservations (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    vehicle_id TEXT NOT NULL, space_id TEXT NOT NULL REFERENCES spaces(id),
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','expired','cancelled','arrived')));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_space ON reservations(space_id) WHERE status='active';
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_vehicle ON reservations(vehicle_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id TEXT NOT NULL,
                    event TEXT NOT NULL, at INTEGER NOT NULL);
            ''')
            db.executemany("INSERT OR IGNORE INTO spaces(id,reason,eta) VALUES(?,?,?)", SPACES)
            columns = {row['name'] for row in db.execute("PRAGMA table_info(spaces)")}
            for name, kind in (("latitude", "REAL"), ("longitude", "REAL"), ("coordinate_source", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE spaces ADD COLUMN {name} {kind}")
            for space_id, (lat, lon) in DEMO_POSITIONS.items():
                db.execute("""UPDATE spaces SET latitude=?,longitude=?,coordinate_source='demo'
                    WHERE id=? AND latitude IS NULL AND longitude IS NULL AND coordinate_source IS NULL""",
                    (lat, lon, space_id))
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            expired = db.execute("SELECT id FROM reservations WHERE status='active' AND expires_at<=?", (now,)).fetchall()
            for row in expired:
                db.execute("INSERT INTO events(reservation_id,event,at) VALUES(?,?,?)", (row['id'], 'expired', now))
            db.execute("UPDATE reservations SET status='expired' WHERE status='active' AND expires_at<=?", (now,))
            yield db, now
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _state(self, db, now, vehicle):
        reservation = db.execute("SELECT * FROM reservations WHERE vehicle_id=? ORDER BY rowid DESC LIMIT 1", (vehicle,)).fetchone()
        rows = db.execute('''SELECT s.*, r.vehicle_id FROM spaces s
            LEFT JOIN reservations r ON r.space_id=s.id AND r.status='active' ORDER BY s.id''').fetchall()
        candidates = [{"spaceId": r['id'], "reason": r['reason'], "etaSeconds": r['eta'],
                       "mapPosition": None if r['latitude'] is None or r['longitude'] is None else {
                           "latitude": r['latitude'], "longitude": r['longitude'], "source": r['coordinate_source']},
                       "available": not r['occupied'] and r['vehicle_id'] is None,
                       "status": 'occupied' if r['occupied'] else 'reserved' if r['vehicle_id'] else 'available'} for r in rows]
        return {"serverTimeMillis": now, "leaseDurationMillis": TTL_MS, "candidates": candidates,
                "reservation": None if reservation is None else {
                    "reservationId": reservation['id'], "spaceId": reservation['space_id'],
                    "status": reservation['status'], "expiresAtMillis": reservation['expires_at'],
                    "requestId": reservation['request_id']}}

    def state(self, vehicle):
        vehicle = identifier(vehicle)
        with self.transaction() as (db, now):
            return self._state(db, now, vehicle)

    def reserve(self, vehicle, space, request):
        vehicle, space, request = map(identifier, (vehicle, space, request))
        with self.transaction() as (db, now):
            receipt = db.execute("SELECT * FROM reservations WHERE request_id=?", (request,)).fetchone()
            if receipt:
                if receipt['vehicle_id'] != vehicle or receipt['space_id'] != space:
                    raise ApiError(409, "request_id_reused")
                # Replaying a successful request never renews or revives its lease.
                return self._state(db, now, vehicle)
            target = db.execute("SELECT * FROM spaces WHERE id=?", (space,)).fetchone()
            if not target:
                raise ApiError(404, "space_not_found")
            if target['occupied']:
                raise ApiError(409, "space_occupied")
            if db.execute("SELECT 1 FROM reservations WHERE status='active' AND (space_id=? OR vehicle_id=?)", (space, vehicle)).fetchone():
                raise ApiError(409, "reservation_conflict")
            rid = str(uuid.uuid4())
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?)", (rid, request, vehicle, space, now, now + TTL_MS, 'active'))
            db.execute("INSERT INTO events(reservation_id,event,at) VALUES(?,?,?)", (rid, 'reserved', now))
            return self._state(db, now, vehicle)

    def transition(self, vehicle, reservation_id, action):
        vehicle, reservation_id = map(identifier, (vehicle, reservation_id))
        if action not in ('cancel', 'arrival'):
            raise ApiError(404, "not_found")
        desired = 'cancelled' if action == 'cancel' else 'arrived'
        with self.transaction() as (db, now):
            row = db.execute("SELECT * FROM reservations WHERE id=? AND vehicle_id=?", (reservation_id, vehicle)).fetchone()
            if row is None:
                raise ApiError(404, "reservation_not_found")
            if row['status'] == desired:
                return self._state(db, now, vehicle)
            if row['status'] != 'active':
                raise ApiError(409, "reservation_not_active")
            db.execute("UPDATE reservations SET status=? WHERE id=?", (desired, reservation_id))
            db.execute("INSERT INTO events(reservation_id,event,at) VALUES(?,?,?)", (reservation_id, desired, now))
            if action == 'arrival':
                db.execute("UPDATE spaces SET occupied=1 WHERE id=?", (row['space_id'],))
            return self._state(db, now, vehicle)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, ledger, site_map=None):
        self.ledger = ledger
        from coordination.curb_sessions import CurbSessions
        self.curb = CurbSessions(ledger.path, ledger.clock)
        from coordination.vacancy import VacancyLedger
        self.vacancy = VacancyLedger(ledger.path,ledger.clock)
        from coordination.site_map import SiteMap
        self.site_map = site_map if site_map is not None else SiteMap()
        from coordination.site_reviews import SiteReviews
        self.site_reviews = SiteReviews(ledger.path,self.site_map,ledger.clock)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def admin_origin(self):
        host=self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
            raise ApiError(403,'local_dashboard_only')
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
            raise ApiError(403,'dashboard_origin_rejected')

    def admin_file(self,name):
        self.admin_origin()
        types={'participate.html':'text/html; charset=utf-8','participate.js':'text/javascript; charset=utf-8','index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8','site-map.js':'text/javascript; charset=utf-8','site-reviews.js':'text/javascript; charset=utf-8','kakao-map.js':'text/javascript; charset=utf-8','osm-map.js':'text/javascript; charset=utf-8','style.css':'text/css; charset=utf-8','demo-map.png':'image/png','vendor/leaflet/leaflet.js':'text/javascript; charset=utf-8','vendor/leaflet/leaflet.css':'text/css; charset=utf-8'}
        raw=(Path(__file__).parent/'admin'/name).read_bytes()
        self.send_response(200);self.send_header('Content-Type',types[name]);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        policy="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if self.server.site_map.enabled:
            # Allow only the selected provider; Kakao additionally requires a browser JS key.
            if self.server.site_map.provider == 'osm':
                policy="default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; connect-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            else:
                policy="default-src 'self'; script-src 'self' https://dapi.kakao.com https://t1.daumcdn.net; style-src 'self'; style-src-attr 'unsafe-inline'; connect-src 'self' https://dapi.kakao.com https://*.daumcdn.net; img-src 'self' data: https://*.daumcdn.net https://*.daum.net; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        self.send_header('Content-Security-Policy',policy)
        self.end_headers();self.wfile.write(raw)

    def respond(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        def route():
            self.admin_origin()
            parsed = urlparse(self.path)
            files={'/participate/':'participate.html','/admin/participate.js':'participate.js','/admin':'index.html','/admin/':'index.html','/admin/app.js':'app.js','/admin/site-map.js':'site-map.js','/admin/site-reviews.js':'site-reviews.js','/admin/kakao-map.js':'kakao-map.js','/admin/osm-map.js':'osm-map.js','/admin/style.css':'style.css','/admin/demo-map.png':'demo-map.png','/admin/vendor/leaflet/leaflet.js':'vendor/leaflet/leaflet.js','/admin/vendor/leaflet/leaflet.css':'vendor/leaflet/leaflet.css'}
            if parsed.path in files:
                self.admin_file(files[parsed.path]);return None
            if parsed.path=='/v1/vacancy/state':
                self.admin_origin()
                return self.server.vacancy.state(parse_qs(parsed.query).get('eventId',[''])[0])
            if parsed.path=='/v1/site-map':
                self.admin_origin()
                return self.server.site_map.public_config()
            if parsed.path=='/v1/site-map/survey':
                self.admin_origin()
                from coordination.site_survey import render_survey
                site=self.server.site_reviews.site()
                expected=parse_qs(parsed.query).get('layout',[''])[0]
                if expected != site['geometryDigest']:
                    raise ApiError(409,'site_review_map_changed')
                raw=render_survey(site,self.server.site_reviews.state(),self.server.ledger.clock()).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename="site-survey-v%d.html"' % site['mapVersion'])
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers();self.wfile.write(raw);return None
            if parsed.path=='/v1/site-map/reviews':
                self.admin_origin()
                return self.server.site_reviews.state()
            if parsed.path == '/health':
                return {"status": "ok"}
            if parsed.path == '/v1/curb/state':
                return self.server.curb.state(parse_qs(parsed.query).get('ownerVehicleId', [''])[0])
            if parsed.path == '/v1/state':
                return self.server.ledger.state(parse_qs(parsed.query).get('vehicleId', [''])[0])
            raise ApiError(404, "not_found")
        self.handle_api(route)

    def do_POST(self):
        def route():
            self.admin_origin()
            if self.headers.get_content_type() != 'application/json':
                raise ApiError(415, 'json_required')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ApiError(400, "invalid_length")
            if not 0 < size <= 4096:
                raise ApiError(413, "invalid_body_size")
            try:
                data = json.loads(self.rfile.read(size),parse_constant=self.invalid_json_constant)
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "invalid_json")
            if not isinstance(data, dict):
                raise ApiError(400, "invalid_json")
            path = urlparse(self.path).path
            if path.startswith('/v1/curb/participation') or path.startswith('/v1/curb/participant/'):
                self.admin_origin()
                if self.headers.get_content_type()!='application/json':raise ApiError(415,'json_required')
                if path=='/v1/curb/participation':return self.server.curb.participation(data.get('sessionId'),data.get('ownerVehicleId'))
                if path=='/v1/curb/participant/poll':return self.server.curb.participant_poll(data.get('token'))
                if path=='/v1/curb/participant/respond':return self.server.curb.participant_act(data.get('token'),data.get('requestId'),data.get('revision'),data.get('action'),data.get('moveRequestId'))
                raise ApiError(404,'not_found')
            if path=='/v1/site-map/reviews':
                self.admin_origin()
                if self.headers.get_content_type()!='application/json':raise ApiError(415,'json_required')
                return self.server.site_reviews.save(data)
            if path.startswith('/v1/vacancy/'):
                self.admin_origin()
                if self.headers.get_content_type()!='application/json':raise ApiError(415,'json_required')
                if path=='/v1/vacancy/observations':return self.server.vacancy.observe(data)
                if path=='/v1/vacancy/assignments':return self.server.vacancy.assign(data)
                source=re.fullmatch(r'/v1/vacancy/sources/([A-Za-z0-9_-]+)/consent',path)
                if source:return self.server.vacancy.consent(source[1],data)
                zone=re.fullmatch(r'/v1/vacancy/zones/([A-Za-z0-9_-]+)/status',path)
                if zone:return self.server.vacancy.zone(zone[1],data)
                assignment=re.fullmatch(r'/v1/vacancy/assignments/([A-Za-z0-9_-]+)/(cancel|arrival)',path)
                if assignment:return self.server.vacancy.transition(assignment[1],assignment[2],data)
                raise ApiError(404,'not_found')
            if path == '/v1/curb/sessions':
                return self.server.curb.create(data.get('ownerVehicleId'), data.get('requestId'))
            traffic = re.fullmatch(r'/v1/curb/sessions/([A-Za-z0-9_-]+)/traffic',path)
            if traffic:
                return self.server.curb.traffic(traffic[1],data.get('ownerVehicleId'),data.get('requestId'),
                    data.get('revision'),data.get('tracks'),data.get('source','observed'),data.get('scenario'))
            observation = re.fullmatch(r'/v1/curb/sessions/([A-Za-z0-9_-]+)/observations',path)
            if observation:
                return self.server.curb.observe(observation[1],data.get('ownerVehicleId'),data.get('requestId'),
                    data.get('revision'),data.get('vehiclePoses'),data.get('obstacles'))
            replan = re.fullmatch(r'/v1/curb/sessions/([A-Za-z0-9_-]+)/replan',path)
            if replan:
                return self.server.curb.replan(replan[1],data.get('ownerVehicleId'),data.get('requestId'),data.get('revision'))
            curb = re.fullmatch(r'/v1/curb/sessions/([A-Za-z0-9_-]+)/actions', path)
            if curb:
                return self.server.curb.act(curb[1], data.get('ownerVehicleId'), data.get('requestId'),
                    data.get('revision'), data.get('action'), data.get('participantId'), data.get('moveRequestId'))
            if path == '/v1/reservations':
                return self.server.ledger.reserve(data.get('vehicleId'), data.get('spaceId'), data.get('requestId'))
            match = re.fullmatch(r'/v1/reservations/([A-Za-z0-9_-]+)/([a-z]+)', path)
            if match:
                return self.server.ledger.transition(data.get('vehicleId'), *match.groups())
            raise ApiError(404, "not_found")
        self.handle_api(route)

    def handle_api(self, call):
        self.connection.settimeout(5)
        try:
            result=call()
            if result is not None:self.respond(200,result)
        except ApiError as error:
            self.respond(error.status, {"error": error.code})
        except (TimeoutError, ConnectionError, BrokenPipeError):
            return
        except sqlite3.OperationalError:
            self.respond(503, {"error": "ledger_unavailable"})

    @staticmethod
    def invalid_json_constant(value):
        raise ValueError('nonfinite_json_number')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--database', default='work/coordination.sqlite3')
    parser.add_argument('--site-map', default=str(Path(__file__).parent/'data'/'sejong-arboretum.geojson'), help='현장 지도 검토용 GeoJSON 파일 (원장과 별도)')
    parser.add_argument('--map-provider', choices=('osm','kakao'), default='osm')
    args = parser.parse_args()
    from coordination.site_map import SiteMap
    try:
        site_map = SiteMap(args.site_map, public_key=os.environ.get('SWARM_KAKAO_MAP_JS_KEY',''),provider=args.map_provider)
    except (OSError, ValueError):
        parser.error('현장 지도 설정을 읽지 못했습니다. 파일 형식·좌표·버전·출처를 확인하세요.')
    server = Server(('127.0.0.1', args.port), Ledger(args.database), site_map)
    print(f'Coordination API listening at http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
