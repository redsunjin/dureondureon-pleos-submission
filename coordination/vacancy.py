"""Local synthetic event-parking observations and dispatch ledger.

Space IDs map to a predefined DEMO map, not GPS-derived sensor detections.
Observation sharing is independent of vehicle movement; this never moves a car.
Source IDs and operator roles are demonstration inputs, not authentication.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from coordination.errors import ApiError, identifier

EVENT = 'festival-demo'
MAP_VERSION = 1
MIN_CONFIDENCE = .8
ASSIGNMENT_MS = 45000
STATES = ('vacant','occupied','blocked','unknown')


def integer(value, minimum=0, maximum=2**53-1):
    return isinstance(value,int) and not isinstance(value,bool) and minimum<=value<=maximum


class VacancyLedger:
    def __init__(self,path,clock=None):
        self.path=str(path);self.clock=clock or (lambda:time.time_ns()//1000000)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS vacancy_zones(id TEXT PRIMARY KEY,label TEXT NOT NULL,open INTEGER NOT NULL,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vacancy_spaces(id TEXT PRIMARY KEY,zone_id TEXT NOT NULL,row_id TEXT NOT NULL,ordinal INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vacancy_sources(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sharing INTEGER NOT NULL,revision INTEGER NOT NULL,last_sequence INTEGER NOT NULL,last_observed INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS vacancy_observations(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,space_id TEXT NOT NULL,source_id TEXT NOT NULL,
                    consent_revision INTEGER NOT NULL,sequence INTEGER NOT NULL,observed INTEGER NOT NULL,received INTEGER NOT NULL,
                    state TEXT NOT NULL,confidence REAL NOT NULL,ttl INTEGER NOT NULL,view TEXT NOT NULL,UNIQUE(source_id,sequence));
                CREATE INDEX IF NOT EXISTS vacancy_space_reports ON vacancy_observations(space_id,source_id);
                CREATE TABLE IF NOT EXISTS vacancy_assignments(id TEXT PRIMARY KEY,space_id TEXT NOT NULL,arrival_id TEXT NOT NULL,
                    created INTEGER NOT NULL,expires INTEGER NOT NULL,status TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS vacancy_one_space ON vacancy_assignments(space_id) WHERE status='active';
                CREATE UNIQUE INDEX IF NOT EXISTS vacancy_one_arrival ON vacancy_assignments(arrival_id) WHERE status='active';
                CREATE TABLE IF NOT EXISTS vacancy_receipts(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vacancy_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER NOT NULL,event TEXT NOT NULL,subject TEXT NOT NULL,details TEXT NOT NULL);
            ''')
            for zone,label in [('A','입구 가까운 구역'),('B','중앙 구역'),('C','안쪽 구역')]:
                db.execute('INSERT OR IGNORE INTO vacancy_zones VALUES(?,?,1,1)',(zone,label))
                for n in range(1,7):db.execute('INSERT OR IGNORE INTO vacancy_spaces VALUES(?,?,?,?)',(f'{zone}-{n:02}',zone,zone+'1',n))
            for sid,kind,sharing in [('STAFF-1','field',1),('CAR-1','vehicle',0),('CAR-2','vehicle',0),('CAR-3','vehicle',0)]:
                db.execute('INSERT OR IGNORE INTO vacancy_sources VALUES(?,?,?,1,0,0)',(sid,kind,sharing))
            db.commit()

    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE');now=self.clock()
                expired=db.execute("SELECT * FROM vacancy_assignments WHERE status='active' AND (expires<=? OR created>?)",(now,now)).fetchall()
                for a in expired:
                    db.execute("UPDATE vacancy_assignments SET status='expired' WHERE id=?",(a['id'],))
                    self.event(db,now,'assignment_expired',a['space_id'],{'assignmentId':a['id']})
                yield db,now
                db.commit()
            except Exception:
                db.rollback();raise

    @staticmethod
    def event(db,now,kind,subject,details):
        db.execute('INSERT INTO vacancy_events(at,event,subject,details) VALUES(?,?,?,?)',(now,kind,subject,json.dumps(details)))

    @staticmethod
    def scope(payload):
        if payload.get('eventId')!=EVENT or not integer(payload.get('mapVersion'),1) or payload['mapVersion']!=MAP_VERSION:
            raise ApiError(409,'vacancy_map_changed')

    @staticmethod
    def request(payload):
        request_id=identifier(payload.get('requestId'))
        fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
        return request_id,fingerprint

    def replay(self,db,now,request_id,fingerprint):
        row=db.execute('SELECT * FROM vacancy_receipts WHERE request_id=?',(request_id,)).fetchone()
        if row:
            if row['fingerprint']!=fingerprint:raise ApiError(409,'request_id_reused')
            result=json.loads(row['result'])
            if result['kind']=='assignment':
                a=db.execute('SELECT * FROM vacancy_assignments WHERE id=?',(result['id'],)).fetchone();result=dict(a)
                result['kind']='assignment'
            return {**self.snapshot(db,now),'actionResult':result}
        return None

    def finish(self,db,now,request_id,fingerprint,result):
        db.execute('INSERT INTO vacancy_receipts VALUES(?,?,?)',(request_id,fingerprint,json.dumps(result)))
        return {**self.snapshot(db,now),'actionResult':result}

    def snapshot(self,db,now):
        zones=[dict(r) for r in db.execute('SELECT * FROM vacancy_zones ORDER BY id')]
        sources=[dict(r) for r in db.execute('SELECT * FROM vacancy_sources ORDER BY kind,id')]
        assignments=[dict(r) for r in db.execute("SELECT * FROM vacancy_assignments WHERE status='active' OR rowid IN (SELECT rowid FROM vacancy_assignments ORDER BY rowid DESC LIMIT 100) ORDER BY rowid DESC")]
        active={a['space_id']:a for a in assignments if a['status']=='active'}
        # Select the latest report for EACH observer/space; never count reports as spaces.
        rows=db.execute('''SELECT o.*,s.sharing,s.revision AS source_revision,s.kind FROM vacancy_observations o JOIN vacancy_sources s ON s.id=o.source_id
            WHERE o.rowid IN (SELECT max(rowid) FROM vacancy_observations GROUP BY space_id,source_id) ORDER BY o.rowid DESC''').fetchall()
        by_space={}
        for row in rows:by_space.setdefault(row['space_id'],[]).append(dict(row))
        zone_map={z['id']:z for z in zones};spaces=[]
        for r in db.execute('SELECT * FROM vacancy_spaces ORDER BY id'):
            reports=by_space.get(r['id'],[])
            eligible=[o for o in reports if o['sharing'] and o['consent_revision']==o['source_revision']]
            fresh=[o for o in eligible if 0<=now-o['observed']<o['ttl']]
            usable=[o for o in fresh if o['confidence']>=MIN_CONFIDENCE and o['state']!='unknown']
            values={o['state'] for o in usable}
            if 'blocked' in values:occupancy,reason='blocked','obstacle_reported'
            elif 'occupied' in values and 'vacant' in values:occupancy,reason='unknown','conflicting_reports'
            elif values:occupancy,reason=next(iter(values)),'fresh_report'
            elif fresh:occupancy,reason='unknown','low_confidence' if all(o['confidence']<MIN_CONFIDENCE for o in fresh) else 'occluded'
            elif eligible:occupancy,reason='unknown','clock_mismatch' if any(o['observed']>now for o in eligible) else 'stale'
            elif reports:occupancy,reason='unknown','sharing_inactive'
            else:occupancy,reason='unknown','unobserved'
            zone=zone_map[r['zone_id']];assignment=active.get(r['id'])
            available=bool(zone['open'] and occupancy=='vacant' and not assignment)
            valid_until=min((o['observed']+o['ttl'] for o in usable),default=now)
            item={**dict(r),'occupancy':occupancy,'reason':reason,'zoneOpen':bool(zone['open']),
                'available':available,'availableUntilMillis':valid_until if available else None,
                'occupancyUntilMillis':valid_until if occupancy!='unknown' else None,
                'assignment':assignment,'freshSourceCount':len(fresh),'reports':[
                    {k:o[k] for k in ('source_id','kind','sequence','observed','received','state','confidence','ttl','view')} |
                    {'sharingActive':bool(o['sharing'] and o['consent_revision']==o['source_revision']),
                     'fresh':o in fresh,'expiresAtMillis':o['observed']+o['ttl']} for o in reports]}
            spaces.append(item)
        for a in assignments:
            space=next(s for s in spaces if s['id']==a['space_id'])
            a['attentionReason']=('zone_closed' if not space['zoneOpen'] else space['reason'] if space['occupancy']!='vacant' else None) if a['status']=='active' else None
        def totals(items):return {'total':len(items),'available':sum(s['available'] for s in items),
            'occupied':sum(s['occupancy']=='occupied' for s in items),'unknown':sum(s['occupancy']=='unknown' for s in items),
            'blocked':sum(s['occupancy']=='blocked' for s in items),'assigned':sum(s['assignment'] is not None for s in items),
            'closed':sum(not s['zoneOpen'] for s in items)}
        for z in zones:z['counts']=totals([s for s in spaces if s['zone_id']==z['id']])
        events=[dict(r) for r in db.execute('SELECT * FROM vacancy_events ORDER BY sequence DESC LIMIT 30')]
        for e in events:e['details']=json.loads(e['details'])
        return {'eventId':EVENT,'eventLabel':'가을 축제 · 가상 주차장','mapVersion':MAP_VERSION,'demo':True,
            'serverTimeMillis':now,'sequence':events[0]['sequence'] if events else 0,'assignmentDurationMillis':ASSIGNMENT_MS,
            'minimumConfidence':MIN_CONFIDENCE,'counts':totals(spaces),'zones':zones,'spaces':spaces,
            'sources':sources,'assignments':assignments,'events':events}

    def state(self,event):
        if event!=EVENT:raise ApiError(404,'vacancy_event_not_found')
        with self.transaction() as (db,now):return self.snapshot(db,now)

    def observe(self,payload):
        self.scope(payload)
        try:request_id,fingerprint=self.request({**payload,'operation':'observation'})
        except (ValueError,TypeError):raise ApiError(400,'invalid_observation') from None
        sid=identifier(payload.get('sourceId'));space=identifier(payload.get('spaceId'))
        sequence=payload.get('sequence');observed=payload.get('observedAtMillis');ttl=payload.get('ttlMillis');confidence=payload.get('confidence')
        if not integer(sequence,1) or not integer(observed) or not integer(ttl,1000,120000) or not isinstance(confidence,(int,float)) or isinstance(confidence,bool) or not math.isfinite(confidence) or not 0<=confidence<=1 or payload.get('state') not in STATES or payload.get('view') not in ('left','right','front','field'):
            raise ApiError(400,'invalid_observation')
        with self.transaction() as (db,now):
            replay=self.replay(db,now,request_id,fingerprint)
            if replay:return replay
            source=db.execute('SELECT * FROM vacancy_sources WHERE id=?',(sid,)).fetchone()
            if not source:raise ApiError(404,'vacancy_source_not_found')
            if not source['sharing']:raise ApiError(409,'vacancy_sharing_disabled')
            if not db.execute('SELECT 1 FROM vacancy_spaces WHERE id=?',(space,)).fetchone():raise ApiError(404,'vacancy_space_not_found')
            if observed>now:raise ApiError(400,'observation_in_future')
            if sequence<=source['last_sequence'] or observed<source['last_observed']:raise ApiError(409,'observation_out_of_order')
            oid=str(uuid.uuid4())
            db.execute('INSERT INTO vacancy_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (oid,request_id,space,sid,source['revision'],sequence,observed,now,payload['state'],confidence,ttl,payload['view']))
            db.execute('UPDATE vacancy_sources SET last_sequence=?,last_observed=? WHERE id=?',(sequence,observed,sid))
            self.event(db,now,'observation',space,{'sourceId':sid,'state':payload['state'],'observationId':oid})
            return self.finish(db,now,request_id,fingerprint,{'kind':'observation','id':oid})

    def consent(self,sid,payload):
        self.scope(payload);sid=identifier(sid);rid,fp=self.request({**payload,'operation':'consent','sourceId':sid})
        if not isinstance(payload.get('enabled'),bool) or not integer(payload.get('revision'),1):raise ApiError(400,'invalid_consent')
        with self.transaction() as (db,now):
            replay=self.replay(db,now,rid,fp)
            if replay:return replay
            source=db.execute('SELECT * FROM vacancy_sources WHERE id=?',(sid,)).fetchone()
            if not source or source['kind']!='vehicle':raise ApiError(404,'vacancy_source_not_found')
            if source['revision']!=payload['revision']:raise ApiError(409,'vacancy_revision_changed')
            if bool(source['sharing'])!=payload['enabled']:
                db.execute('UPDATE vacancy_sources SET sharing=?,revision=revision+1 WHERE id=?',(payload['enabled'],sid))
                self.event(db,now,'sharing_enabled' if payload['enabled'] else 'sharing_disabled',sid,{})
            return self.finish(db,now,rid,fp,{'kind':'consent','id':sid})

    def zone(self,zid,payload):
        self.scope(payload);zid=identifier(zid);rid,fp=self.request({**payload,'operation':'zone','zoneId':zid})
        if not isinstance(payload.get('open'),bool) or not integer(payload.get('revision'),1):raise ApiError(400,'invalid_zone_status')
        with self.transaction() as (db,now):
            replay=self.replay(db,now,rid,fp)
            if replay:return replay
            zone=db.execute('SELECT * FROM vacancy_zones WHERE id=?',(zid,)).fetchone()
            if not zone:raise ApiError(404,'vacancy_zone_not_found')
            if zone['revision']!=payload['revision']:raise ApiError(409,'vacancy_revision_changed')
            if bool(zone['open'])!=payload['open']:
                db.execute('UPDATE vacancy_zones SET open=?,revision=revision+1 WHERE id=?',(payload['open'],zid))
                self.event(db,now,'zone_opened' if payload['open'] else 'zone_closed',zid,{})
            return self.finish(db,now,rid,fp,{'kind':'zone','id':zid})

    def assign(self,payload):
        self.scope(payload);rid,fp=self.request({**payload,'operation':'assign'})
        space=identifier(payload.get('spaceId'));arrival=identifier(payload.get('arrivalId'))
        with self.transaction() as (db,now):
            replay=self.replay(db,now,rid,fp)
            if replay:return replay
            snapshot=self.snapshot(db,now);target=next((s for s in snapshot['spaces'] if s['id']==space),None)
            if target is None:raise ApiError(404,'vacancy_space_not_found')
            if not target['available']:raise ApiError(409,'vacancy_not_available')
            if db.execute("SELECT 1 FROM vacancy_assignments WHERE arrival_id=? AND status='active'",(arrival,)).fetchone():raise ApiError(409,'vacancy_arrival_already_assigned')
            aid=str(uuid.uuid4());db.execute('INSERT INTO vacancy_assignments VALUES(?,?,?,?,?,?)',(aid,space,arrival,now,now+ASSIGNMENT_MS,'active'))
            self.event(db,now,'assignment',space,{'assignmentId':aid,'arrivalId':arrival})
            return self.finish(db,now,rid,fp,{'kind':'assignment','id':aid,'status':'active','expires':now+ASSIGNMENT_MS})

    def transition(self,aid,action,payload):
        self.scope(payload);aid=identifier(aid)
        if action not in ('cancel','arrival'):raise ApiError(404,'not_found')
        rid,fp=self.request({**payload,'operation':action,'assignmentId':aid})
        with self.transaction() as (db,now):
            replay=self.replay(db,now,rid,fp)
            if replay:return replay
            a=db.execute('SELECT * FROM vacancy_assignments WHERE id=?',(aid,)).fetchone()
            if not a:raise ApiError(404,'vacancy_assignment_not_found')
            desired='cancelled' if action=='cancel' else 'arrived'
            if a['status']==desired:return self.finish(db,now,rid,fp,{'kind':'assignment','id':aid,'status':desired})
            if a['status']!='active':raise ApiError(409,'vacancy_assignment_not_active')
            if action=='arrival':
                target=next(s for s in self.snapshot(db,now)['spaces'] if s['id']==a['space_id'])
                if target['occupancy']!='occupied':raise ApiError(409,'vacancy_occupancy_not_confirmed')
            db.execute('UPDATE vacancy_assignments SET status=? WHERE id=?',(desired,aid))
            self.event(db,now,'arrival' if action=='arrival' else 'assignment_cancelled',a['space_id'],{'assignmentId':aid})
            return self.finish(db,now,rid,fp,{'kind':'assignment','id':aid,'status':desired})
