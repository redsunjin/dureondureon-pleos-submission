"""Durable, server-authoritative consent and replay for SYNTHETIC curb vehicles.

Browser participant responses are supported; no physical vehicle actuation.
One local demo curb has one active plan. An app
heartbeat advances a prevalidated simulator trajectory; gaps never get replayed.
The next access evaluates expiry/disconnection (there is no background runner).
"""
from bisect import bisect_right
from contextlib import closing, contextmanager
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import secrets
import threading
import time
import uuid

from coordination.errors import ApiError, identifier

HEARTBEAT_MS = 3000
APPROVAL_MS = 180_000
RUN_MS = 120_000
ACTIVE = ('awaiting', 'ready', 'running')


def build_template():
    from simulator import curb_parking as cp
    scene = cp.demo_scene()
    proposal = cp.plan(scene).plan
    if proposal is None:
        raise RuntimeError('demo plan unavailable')
    return template_from_plan(scene, proposal)


def template_from_plan(scene, proposal, continuation=False):
    from simulator import curb_parking as cp
    result = cp.execute(scene, proposal, continuation=continuation)
    if result['status'] != 'parked':
        raise RuntimeError('demo trajectory invalid')
    cars = (*scene.parked, scene.incoming)
    roles = ('뒤 바깥 차량', '뒤 인접 차량', '앞 인접 차량', '앞 바깥 차량', '내 차 · 새 차량')
    opening, final = dict(proposal.shifted), dict(proposal.final_parked)
    participants = []
    for car, role in zip(cars, roles):
        steps, t = [], 0.0
        for command in proposal.commands:
            if command.actor == car.id:
                key = (command.stage, 1 if command.distance > 0 else -1 if command.distance < 0 else 0)
                if steps and steps[-1]['key'] == key and abs(steps[-1]['end']-t) < 1e-7:
                    steps[-1]['end'] += command.duration
                    steps[-1]['distance'] += command.distance
                else:
                    steps.append({'key': key, 'stage': command.stage, 'start': t,
                                  'end': t+command.duration, 'distance': command.distance})
            t += command.duration
        for step in steps:
            del step['key']
        participants.append({'vehicleId': car.id, 'role': role, 'length': car.length, 'width': car.width,
                             'initialPose': asdict(car.pose), 'openingX': opening.get(car.id, proposal.entry_target.x),
                             'finalX': final.get(car.id, proposal.target.x), 'maxBackward': car.max_backward,
                             'maxForward': car.max_forward, 'steps': steps,
                             'totalTravel': sum(abs(s['distance']) for s in steps)})
    return {'sourceSignature': proposal.scene_digest, 'sceneLength': scene.length, 'roadWidth': scene.road_width,
            'initialGap': proposal.original_gap, 'openingGap': proposal.expanded_gap,
            'duration': result['elapsed_seconds'], 'participants': participants, 'frames': result['frames'],
            'certifiedClearance': result['certified_body_clearance'], 'schemaVersion': 2,
            'scene': asdict(scene), 'plan': asdict(proposal), 'continuation': continuation}


class CurbSessions:
    def __init__(self, path, clock=None, template_factory=build_template):
        self.path = str(path)
        self.clock = clock or (lambda: time.time_ns()//1_000_000)
        self.template_factory = template_factory
        self._template = None
        self._template_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS curb_sessions(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, owner TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL,
                    last_tick INTEGER NOT NULL, elapsed REAL NOT NULL, rate INTEGER NOT NULL,
                    data TEXT NOT NULL, decisions TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_curb_plan ON curb_sessions(scope)
                    WHERE status IN ('awaiting','ready','running');
                CREATE TABLE IF NOT EXISTS curb_participants(
                    session_id TEXT NOT NULL, vehicle_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
                    last_seen INTEGER, PRIMARY KEY(session_id,vehicle_id));
                CREATE TABLE IF NOT EXISTS curb_receipts(
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, session_id TEXT NOT NULL);
            ''')
            # A new server process never resumes virtual motion from wall-clock elapsed time.
            db.execute("UPDATE curb_sessions SET status='stopped',reason='server_restarted',revision=revision+1 WHERE status='running'")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                now = self.clock()
                # Expired plans do not retain a lock on the demonstration curb.
                db.execute("UPDATE curb_sessions SET status='expired',reason='plan_expired',revision=revision+1 WHERE status IN ('awaiting','ready') AND expires<=?", (now,))
                # A participant response gap freezes the saved arrangement before any advance.
                stale=db.execute("SELECT DISTINCT s.id FROM curb_sessions s JOIN curb_participants p ON p.session_id=s.id WHERE s.status IN ('awaiting','ready','running') AND json_extract(s.decisions, '$.\"' || p.vehicle_id || '\"')='approved' AND (p.last_seen IS NULL OR p.last_seen>? OR ?-p.last_seen>?)",(now,now,HEARTBEAT_MS)).fetchall()
                for item in stale:
                    row=dict(db.execute('SELECT * FROM curb_sessions WHERE id=?',(item['id'],)).fetchone())
                    row.update(status='stopped',reason='participant_connection_lost',revision=row['revision']+1,
                        decisions=json.dumps({c['vehicleId']:'pending' for c in json.loads(row['data'])['participants']}))
                    self._save(db,row)
                # No advancing through a gap, including while another owner creates a plan.
                db.execute("UPDATE curb_sessions SET status='stopped',reason='connection_lost',revision=revision+1 WHERE status='running' AND (last_tick>? OR ?-last_tick>?)", (now, now, HEARTBEAT_MS))
                db.execute("UPDATE curb_sessions SET status='expired',reason='plan_expired',revision=revision+1 WHERE status='running' AND expires<=?", (now,))
                yield db, now
                db.commit()
            except Exception:
                db.rollback()
                raise

    def template(self):
        with self._template_lock:
            if self._template is None:
                self._template = self.template_factory()
            return self._template

    def _load(self, db, session_id, owner):
        row = db.execute('SELECT * FROM curb_sessions WHERE id=? AND owner=?', (session_id, owner)).fetchone()
        if row is None:
            raise ApiError(404, 'curb_session_not_found')
        return dict(row)

    def _advance(self, db, row, now):
        row=self._traffic_refresh(db,row,now)
        if row['status'] == 'running':
            data = json.loads(row['data'])
            row['elapsed'] = min(data['duration'], row['elapsed']+(now-row['last_tick'])/1000*row['rate'])
            row['last_tick'] = now
            if row['elapsed'] >= data['duration']:
                row['status'], row['reason'] = 'completed', 'completed'
                row['revision'] += 1
            self._save(db, row)
        return row

    def _traffic_refresh(self,db,row,now):
        from simulator import curb_traffic as ct
        data=json.loads(row['data']);traffic=data.get('traffic')
        if not traffic or row['status']=='completed':return row
        running=row['status']=='running'
        elapsed=data.get('trafficCheckpointElapsed',row['elapsed'])
        travel=not data.get('observation') or 'trafficCheckpointElapsed' in data
        extra=max(0,(now-row['last_tick'])/1000*row['rate']) if running else 0
        result=ct.forecast(data,elapsed,traffic,now,row['rate'],travel=travel,extra=extra,
            anchor=row['last_tick'] if running else now)
        if result['safety'] in ('risk','stale'):
            traffic.update(result,clearSinceMillis=None)
            if row['status'] in ACTIVE:
                index,frame=self._checkpoint(row,data)
                data['observation']={'poses':frame['poses'],'obstacles':frame.get('obstacles',[])}
                data['trafficCheckpointElapsed']=row['elapsed']
                data['environmentRevision']=data.get('environmentRevision',0)+1
                row.update(status='stopped',reason='traffic_risk' if result['safety']=='risk' else 'traffic_stale',
                    revision=row['revision']+1,decisions=json.dumps({c['vehicleId']:'pending' for c in data['participants']}))
            elif row['reason'] in ('traffic_risk','traffic_stale','traffic_cleared'):
                row['reason']='traffic_risk' if result['safety']=='risk' else 'traffic_stale'
        else:
            since=traffic.get('clearSinceMillis')
            if since is None or now<since:since=now
            safety='clear' if now-since>=ct.CLEAR_MS else 'clearing'
            traffic.update(result,safety=safety,clearSinceMillis=since)
            # Clearing never resumes a stopped plan or copies its old votes.
            if row['reason'] in ('traffic_risk','traffic_stale','traffic_cleared') and safety=='clear':
                row['reason']='traffic_cleared'
        row['data']=json.dumps(data)
        db.execute('UPDATE curb_sessions SET data=? WHERE id=?',(row['data'],row['id']))
        self._save(db,row)
        return row

    def _save(self, db, row):
        db.execute('''UPDATE curb_sessions SET status=?,reason=?,revision=?,expires=?,last_tick=?,elapsed=?,decisions=? WHERE id=?''',
                   tuple(row[k] for k in ('status','reason','revision','expires','last_tick','elapsed','decisions','id')))

    @staticmethod
    def _checkpoint(row,data):
        if data.get('observation'):
            return 0, {'time':0.0,'stage':'stopped','actor':None,**data['observation']}
        frames=data['frames']
        # Completion must not miss the last rounded timestamp.
        index=len(frames)-1 if row['status']=='completed' else max(0,bisect_right([f['time'] for f in frames],row['elapsed']+1e-6)-1)
        return index,frames[index]

    def _response(self, row, now):
        if row is None:
            return {'serverTimeMillis': now, 'session': None}
        data, decisions = json.loads(row['data']), json.loads(row['decisions'])
        from simulator import curb_traffic as ct
        frames = data['frames']
        frame_index,frame = self._checkpoint(row,data)
        t = data['duration'] if row['status']=='completed' else frame['time']
        participants = []
        for car in data['participants']:
            traveled = sum(abs(step['distance'])*max(0, min(1, (t-step['start'])/(step['end']-step['start']))) for step in car['steps'])
            decision = decisions[car['vehicleId']]
            progress = min(100, 100*traveled/car['totalTravel']) if car['totalTravel'] else 0
            if row['status']=='completed':
                progress = 100
            if row['status'] in ('stopped','expired','rejected'):
                motion = 'stopped'
            elif row['status'] == 'completed' or progress >= 100-1e-6:
                motion = 'complete'
            elif row['status']=='running' and car['totalTravel']<=1e-8:
                motion = 'holding'
            elif row['status'] == 'running' and frame['actor'] == car['vehicleId']:
                motion = 'moving' if frame['stage'] != 'steering_at_rest' else 'steering'
            else:
                motion = 'waiting'
            participants.append({**car, 'moveRequestId': f"{row['id']}_{car['vehicleId']}",
                                 'decision': decision, 'progressPercent': progress, 'motionState': motion,
                                 'pose': frame['poses'][car['vehicleId']]})
        return {'serverTimeMillis': now, 'session': {
            'sessionId': row['id'], 'revision': row['revision'], 'status': row['status'], 'reason': row['reason'],
            'expiresAtMillis': row['expires'], 'demo': True, 'playbackRate': row['rate'],
            'parentSessionId': data.get('parentSessionId'), 'parentRevision': data.get('parentRevision'),
            'replanGeneration': data.get('replanGeneration',0), 'replanMode': data.get('replanMode'),
            'checkpointHash': data.get('checkpointHash'),
            'environmentRevision': data.get('environmentRevision',0),
            'traffic':ct.response(data.get('traffic'),now,row['rate']),
            'obstacles': frame.get('obstacles',[]),
            'plannedPath': [] if data.get('observation') else [f['poses']['NEW'] for f in frames[::max(1,len(frames)//80)]]+[frames[-1]['poses']['NEW']],
            'sourceSignature': data['sourceSignature'], 'sceneLength': data['sceneLength'], 'roadWidth': data['roadWidth'],
            'initialGap': data['initialGap'], 'openingGap': data['openingGap'], 'durationSeconds': data['duration'],
            'elapsedSeconds': t, 'lastStage': frame['stage'],
            'stage': frame['stage'] if row['status']=='running' else 'parked' if row['status']=='completed' else 'waiting' if row['status'] in ('awaiting','ready') else 'stopped',
            'actor': frame['actor'] if row['status']=='running' else None,
            'participantMode':bool(data.get('participantMode')), 'participants': participants, 'heartbeatMillis': HEARTBEAT_MS}}

    @staticmethod
    def _fingerprint(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _replay(self, db, request_id, fingerprint, owner, now):
        receipt = db.execute('SELECT * FROM curb_receipts WHERE request_id=?', (request_id,)).fetchone()
        if receipt:
            if receipt['fingerprint'] != fingerprint:
                raise ApiError(409, 'request_id_reused')
            original=self._load(db, receipt['session_id'], owner)
            latest=db.execute('SELECT * FROM curb_sessions WHERE owner=? ORDER BY rowid DESC LIMIT 1',(owner,)).fetchone()
            # Replaying an old receipt must not redraw an ancestor arrangement
            # after a descendant replan has become the current session.
            row=dict(latest) if latest else original
            if json.loads(row['data']).get('traffic'):row=self._advance(db,row,now)
            return self._response(row, now)
        return None

    def state(self, owner):
        owner = identifier(owner)
        with self.transaction() as (db, now):
            row = db.execute('SELECT * FROM curb_sessions WHERE owner=? ORDER BY rowid DESC LIMIT 1', (owner,)).fetchone()
            return self._response(self._advance(db, dict(row), now) if row else None, now)

    def create(self, owner, request_id):
        owner, request_id = identifier(owner), identifier(request_id)
        fingerprint = self._fingerprint(['create', owner])
        data = self.template()
        with self.transaction() as (db, now):
            replay = self._replay(db, request_id, fingerprint, owner, now)
            if replay:
                return replay
            if db.execute("SELECT 1 FROM curb_sessions WHERE status IN ('awaiting','ready','running')").fetchone():
                raise ApiError(409, 'curb_busy')
            session_id = str(uuid.uuid4())
            decisions = {c['vehicleId']: 'pending' for c in data['participants']}
            db.execute('INSERT INTO curb_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session_id, 'demo-curb', owner, 'awaiting', 'awaiting_consent', 1,
                        now, now+APPROVAL_MS, now, 0.0, 4, json.dumps(data), json.dumps(decisions)))
            db.execute('INSERT INTO curb_receipts VALUES(?,?,?)', (request_id, fingerprint, session_id))
            return self._response(self._load(db, session_id, owner), now)

    def _replan_source(self,db,source_id,owner,revision):
        row=self._load(db,source_id,owner)
        if row['revision']!=revision:
            raise ApiError(409,'curb_revision_changed')
        latest=db.execute('SELECT id FROM curb_sessions WHERE owner=? ORDER BY rowid DESC LIMIT 1',(owner,)).fetchone()
        if latest['id']!=source_id:
            raise ApiError(409,'curb_source_superseded')
        if row['status'] not in ('stopped','rejected','expired'):
            raise ApiError(409,'curb_replan_source_not_stopped')
        if db.execute("SELECT 1 FROM curb_sessions WHERE status IN ('awaiting','ready','running')").fetchone():
            raise ApiError(409,'curb_busy')
        return row

    def _replan_template(self,row):
        from simulator import curb_replan as cr
        data=json.loads(row['data'])
        metadata=data
        if 'scene' not in data or 'plan' not in data:
            # Recover only an EXACT match to the previous canonical demo format;
            # unknown historical poses must never be reset to a guessed scene.
            canonical=self.template()
            fields=('vehicleId','length','width','initialPose')
            old_cars=[{k:c[k] for k in fields} for c in data['participants']]
            canonical_cars=[{k:c[k] for k in fields} for c in canonical['participants']]
            if data['frames']!=canonical['frames'] or data['sourceSignature']!=canonical['sourceSignature'] or old_cars!=canonical_cars:
                raise ValueError('legacy_checkpoint_not_recoverable')
            metadata=canonical
        source_scene=cr.scene_from_dict(metadata['scene'])
        source_plan=cr.plan_from_dict(metadata['plan'])
        if data.get('observation'):
            from simulator import curb_dynamic as cd
            from simulator import curb_parking as cp
            if source_plan.scene_digest!=cp.signature(source_scene):raise ValueError('observed_source_signature_changed')
            observed=data['observation']
            obstacles=tuple(cp.Obstacle(**{**o,'pose':cp.Pose(**o['pose'])}) for o in observed['obstacles'])
            scene=cd.observed_scene(source_scene,observed['poses'],obstacles)
            proposal=cd.plan(scene,source_plan.gap_index)
            result=template_from_plan(scene,proposal,continuation=True)
            if result['frames'][0]['poses']!=observed['poses']:
                raise ValueError('observation_would_change')
            result.update(parentSessionId=row['id'],parentRevision=row['revision'],
                replanGeneration=data.get('replanGeneration',0)+1,replanMode='changed_environment_search',
                checkpointHash=self._fingerprint(observed),environmentRevision=data.get('environmentRevision',0))
            return result
        if len(data['frames'])!=len(source_plan.commands)+2:
            raise ValueError('checkpoint_frame_map_changed')
        index,frame=self._checkpoint(row,data)
        scene,proposal,mode=cr.prepare(source_scene,source_plan,index,frame['poses'])
        result=template_from_plan(scene,proposal,continuation=True)
        if result['frames'][0]['poses']!=frame['poses']:
            raise ValueError('replan_would_change_checkpoint')
        result.update(parentSessionId=row['id'],parentRevision=row['revision'],
            replanGeneration=data.get('replanGeneration',0)+1,replanMode=mode,
            checkpointHash=self._fingerprint(frame['poses']),environmentRevision=data.get('environmentRevision',0))
        return result

    def observe(self,session_id,owner,request_id,revision,vehicle_poses=None,obstacles=None):
        """Developer observation boundary; save changed geometry and STOP atomically.

        Geometric danger is retained as a stopped observation, not discarded or
        reset. Structural errors are rejected. Replanning is a separate action.
        """
        session_id,owner,request_id=map(identifier,(session_id,owner,request_id))
        if not isinstance(revision,int) or isinstance(revision,bool) or revision<1:
            raise ApiError(400,'invalid_revision')
        if vehicle_poses is None:vehicle_poses=[]
        if not isinstance(vehicle_poses,list) or len(vehicle_poses)>5 or (obstacles is not None and (not isinstance(obstacles,list) or len(obstacles)>12)):
            raise ApiError(400,'invalid_observation')
        fingerprint=self._fingerprint(['observe',session_id,owner,revision,vehicle_poses,obstacles])
        self.state(owner)
        with self.transaction() as (db,now):
            replay=self._replay(db,request_id,fingerprint,owner,now)
            if replay:return replay
            row=self._load(db,session_id,owner)
            latest=db.execute('SELECT id FROM curb_sessions WHERE owner=? ORDER BY rowid DESC LIMIT 1',(owner,)).fetchone()
            if row['revision']!=revision or latest['id']!=session_id or row['status']=='completed':
                raise ApiError(409,'curb_observation_source_changed')
            if db.execute("SELECT 1 FROM curb_sessions WHERE status IN ('awaiting','ready','running') AND id<>?",(session_id,)).fetchone():
                raise ApiError(409,'curb_busy')
            row=self._advance(db,row,now)
            if row['status']=='completed':raise ApiError(409,'curb_observation_source_changed')
            data=json.loads(row['data']);_,frame=self._checkpoint(row,data)
            poses=json.loads(json.dumps(frame['poses']));old_obstacles=frame.get('obstacles',[])
            def number(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
            def pose_ok(p):
                return isinstance(p,dict) and set(p)=={'x','y','yaw'} and all(number(v) for v in p.values()) and 0<=p['x']<=data['sceneLength'] and 0<=p['y']<=data['roadWidth'] and abs(p['yaw'])<=math.pi
            seen=set()
            for item in vehicle_poses:
                if not isinstance(item,dict) or set(item)!={'vehicleId','pose'} or not isinstance(item['vehicleId'],str) or item['vehicleId'] not in poses or item['vehicleId'] in seen or not pose_ok(item['pose']):
                    raise ApiError(400,'invalid_observation')
                seen.add(item['vehicleId']);poses[item['vehicleId']]=item['pose']
            if obstacles is None:obstacles=old_obstacles
            obstacle_ids=set(poses)
            for o in obstacles:
                if not isinstance(o,dict) or set(o)!={'id','pose','length','width'} or not pose_ok(o['pose']):raise ApiError(400,'invalid_observation')
                oid=identifier(o['id'])
                if oid in obstacle_ids or not number(o['length']) or not number(o['width']) or not 0<o['length']<=data['sceneLength'] or not 0<o['width']<=data['roadWidth']:
                    raise ApiError(400,'invalid_observation')
                obstacle_ids.add(oid)
            if poses==frame['poses'] and obstacles==old_obstacles:raise ApiError(409,'curb_observation_unchanged')
            data['observation']={'poses':poses,'obstacles':obstacles}
            data.pop('trafficCheckpointElapsed',None)
            data['environmentRevision']=data.get('environmentRevision',0)+1
            row.update(status='stopped',reason='environment_changed',revision=row['revision']+1,
                decisions=json.dumps({i:'pending' for i in poses}))
            row['data']=json.dumps(data);db.execute('UPDATE curb_sessions SET data=? WHERE id=?',(row['data'],session_id))
            self._save(db,row)
            db.execute('INSERT INTO curb_receipts VALUES(?,?,?)',(request_id,fingerprint,session_id))
            return self._response(row,now)

    def traffic(self,session_id,owner,request_id,revision,tracks=None,source='observed',scenario=None):
        """Full moving-object coverage update; safe updates do not change votes.

        Control revision changes only on a safety stop. Tracking has a separate
        revision, so a fresh observer does not race every driver's consent.
        """
        from simulator import curb_traffic as ct
        session_id,owner,request_id=map(identifier,(session_id,owner,request_id))
        if not isinstance(revision,int) or isinstance(revision,bool) or revision<1:raise ApiError(400,'invalid_revision')
        fingerprint=self._fingerprint(['traffic',session_id,owner,revision,tracks,source,scenario])
        self.state(owner)
        with self.transaction() as (db,now):
            replay=self._replay(db,request_id,fingerprint,owner,now)
            if replay:return replay
            row=self._load(db,session_id,owner)
            latest=db.execute('SELECT id FROM curb_sessions WHERE owner=? ORDER BY rowid DESC LIMIT 1',(owner,)).fetchone()
            if row['revision']!=revision or latest['id']!=session_id or row['status']=='completed':raise ApiError(409,'curb_observation_source_changed')
            row=self._advance(db,row,now);data=json.loads(row['data'])
            if row['status']=='completed':raise ApiError(409,'curb_observation_source_changed')
            if 'scene' not in data or 'plan' not in data:raise ApiError(422,'curb_traffic_metadata_unavailable')
            try:
                if scenario is not None:
                    if source!='scripted' or tracks is not None:raise ValueError('invalid_scenario')
                    tracks=ct.scripted(scenario,data,now)
                motion=ct.validate(tracks,source,now,data['sceneLength'],data['roadWidth'],row['rate'])
                if any(t['id'] in {c['vehicleId'] for c in data['participants']} or t['id'] in {o['id'] for o in self._checkpoint(row,data)[1].get('obstacles',[])} for t in motion['tracks']):raise ValueError('duplicate_traffic_body')
            except (ValueError,TypeError,KeyError):raise ApiError(400,'invalid_traffic') from None
            previous=data.get('traffic')
            if previous:
                newest=max((t['observedAtMillis'] for t in previous['tracks']),default=previous['receivedAtMillis'])
                if source=='observed' and any(t['observedAtMillis']<newest for t in motion['tracks']):raise ApiError(409,'traffic_observation_out_of_order')
                motion['revision']=previous['revision']+1
                motion['clearSinceMillis']=previous.get('clearSinceMillis')
            data['traffic']=motion;row['data']=json.dumps(data)
            db.execute('UPDATE curb_sessions SET data=? WHERE id=?',(row['data'],session_id))
            row=self._traffic_refresh(db,row,now)
            db.execute('INSERT INTO curb_receipts VALUES(?,?,?)',(request_id,fingerprint,session_id))
            return self._response(row,now)

    def replan(self,source_id,owner,request_id,revision):
        source_id,owner,request_id=map(identifier,(source_id,owner,request_id))
        if not isinstance(revision,int) or isinstance(revision,bool) or revision<1:
            raise ApiError(400,'invalid_revision')
        fingerprint=self._fingerprint(['replan',source_id,owner,revision])
        # Commit any autonomous traffic stop/clearance before a requested action
        # can fail. A refusal must not roll back the safety state.
        self.state(owner)
        with self.transaction() as (db,now):
            replay=self._replay(db,request_id,fingerprint,owner,now)
            if replay:return replay
            source=self._replan_source(db,source_id,owner,revision)
            if json.loads(source['data']).get('traffic',{}).get('safety','clear')!='clear':raise ApiError(409,'curb_traffic_unsafe')
        # Geometry validation happens outside the write lock. Recheck the source
        # and curb lock after calculation so concurrent plans cannot fork it.
        try:
            data=self._replan_template(source)
        except (ValueError,KeyError,TypeError,RuntimeError,OverflowError,IndexError):
            raise ApiError(422,'curb_replan_unavailable') from None
        with self.transaction() as (db,now):
            replay=self._replay(db,request_id,fingerprint,owner,now)
            if replay:return replay
            current=self._replan_source(db,source_id,owner,revision)
            static=lambda raw:{k:v for k,v in json.loads(raw).items() if k!='traffic'}
            if static(current['data'])!=static(source['data']) or current['elapsed']!=source['elapsed']:
                raise ApiError(409,'curb_revision_changed')
            traffic=json.loads(current['data']).get('traffic')
            if traffic:
                from simulator import curb_traffic as ct
                if traffic['safety']!='clear' or ct.forecast(data,0,traffic,now,current['rate'])['safety']!='clear':
                    raise ApiError(409,'curb_traffic_unsafe')
                data['traffic']=traffic
            if json.loads(source['data']).get('participantMode'):data['participantMode']=True
            session_id=str(uuid.uuid4())
            decisions={c['vehicleId']:'pending' for c in data['participants']}
            db.execute('INSERT INTO curb_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (session_id,'demo-curb',owner,'awaiting','awaiting_reconsent',1,now,now+APPROVAL_MS,now,0.0,source['rate'],json.dumps(data),json.dumps(decisions)))
            db.execute('INSERT INTO curb_receipts VALUES(?,?,?)',(request_id,fingerprint,session_id))
            return self._response(self._load(db,session_id,owner),now)

    def act(self, session_id, owner, request_id, revision, action, participant=None, move_request=None, participant_token=None):
        session_id, owner, request_id = map(identifier, (session_id, owner, request_id))
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ApiError(400, 'invalid_revision')
        if action not in ('approve','reject','withdraw','start','stop'):
            raise ApiError(400, 'invalid_curb_action')
        if action in ('approve','reject','withdraw'):
            participant, move_request = identifier(participant), identifier(move_request)
        fingerprint = self._fingerprint([session_id, owner, revision, action, participant, move_request]+([participant_token] if participant_token is not None else []))
        with self.transaction() as (db, now):
            replay = self._replay(db, request_id, fingerprint, owner, now)
            if replay:
                return replay
            row = self._load(db,session_id,owner);before_revision=row['revision']
            if json.loads(row['data']).get('participantMode') and action in ('approve','reject','withdraw'):
                identity=db.execute('SELECT vehicle_id FROM curb_participants WHERE session_id=? AND token=?',(session_id,participant_token)).fetchone()
                if not identity or identity['vehicle_id']!=participant:raise ApiError(403,'participant_response_required')
            row = self._advance(db,row,now)
            if row['revision']!=before_revision and row['reason'] in ('traffic_risk','traffic_stale'):
                db.execute('INSERT INTO curb_receipts VALUES(?,?,?)',(request_id,fingerprint,session_id))
                return self._response(row,now)
            if row['revision'] != revision:
                raise ApiError(409, 'curb_revision_changed')
            if row['status'] not in ACTIVE:
                raise ApiError(409, 'curb_session_terminal')
            if action in ('start','approve') and json.loads(row['data']).get('traffic',{}).get('safety','clear')!='clear':
                raise ApiError(409,'curb_traffic_unsafe')
            decisions = json.loads(row['decisions'])
            if action in ('approve','reject','withdraw'):
                if participant not in decisions or move_request != f'{session_id}_{participant}':
                    raise ApiError(400, 'invalid_move_request')
                if action in ('approve','reject') and (row['status'] != 'awaiting' or decisions[participant] != 'pending'):
                    raise ApiError(409, 'curb_decision_not_pending')
                if action == 'withdraw' and decisions[participant] != 'approved':
                    raise ApiError(409, 'curb_not_approved')
                decisions[participant] = {'approve': 'approved', 'reject': 'rejected', 'withdraw': 'withdrawn'}[action]
                if action != 'approve':
                    row['status'], row['reason'] = ('rejected','vehicle_rejected') if action == 'reject' else ('stopped','consent_withdrawn')
                elif all(v == 'approved' for v in decisions.values()):
                    row['status'], row['reason'] = 'ready', 'all_approved'
            elif action == 'start':
                if row['status'] != 'ready' or not all(v == 'approved' for v in decisions.values()):
                    raise ApiError(409, 'consent_incomplete')
                row.update(status='running', reason='running', last_tick=now, expires=now+RUN_MS)
            else:
                row['status'], row['reason'] = 'stopped', 'user_stopped'
            row['decisions'] = json.dumps(decisions)
            row['revision'] += 1
            self._save(db, row)
            db.execute('INSERT INTO curb_receipts VALUES(?,?,?)', (request_id, fingerprint, session_id))
            return self._response(row, now)

    def participation(self, session_id, owner):
        """Local demo invitation setup; no production vehicle identity is asserted."""
        session_id,owner=identifier(session_id),identifier(owner)
        self.state(owner)
        with self.transaction() as (db,now):
            row=self._load(db,session_id,owner);data=json.loads(row['data'])
            if row['status'] not in ('awaiting','ready','running'):raise ApiError(409,'curb_session_terminal')
            if not data.get('participantMode'):
                if row['status']!='awaiting' or any(v!='pending' for v in json.loads(row['decisions']).values()):
                    raise ApiError(409,'participation_requires_new_plan')
                data['participantMode']=True
                row['data']=json.dumps(data);row['revision']+=1
                db.execute('UPDATE curb_sessions SET data=? WHERE id=?',(row['data'],session_id));self._save(db,row)
            for car in data['participants']:
                db.execute('INSERT OR IGNORE INTO curb_participants VALUES(?,?,?,NULL)',(session_id,car['vehicleId'],secrets.token_urlsafe(32)))
            invitations=[dict(r) for r in db.execute('SELECT vehicle_id AS vehicleId,token FROM curb_participants WHERE session_id=?',(session_id,))]
            return {**self._response(row,now),'invitations':invitations}

    def _identity(self, token):
        if not isinstance(token,str) or not 20<=len(token)<=100:raise ApiError(403,'invalid_participant_token')
        with closing(self.connect()) as db:
            row=db.execute('SELECT p.*,s.owner FROM curb_participants p JOIN curb_sessions s ON s.id=p.session_id WHERE token=?',(token,)).fetchone()
        if not row:raise ApiError(403,'invalid_participant_token')
        return dict(row)

    @staticmethod
    def _personal(response, vehicle):
        session=response['session']
        if session is None:return response
        own=next((p for p in session['participants'] if p['vehicleId']==vehicle),None)
        if not own:raise ApiError(409,'participant_plan_changed')
        return {'serverTimeMillis':response['serverTimeMillis'],'session':{k:session[k] for k in ('sessionId','revision','status','reason','expiresAtMillis','demo','heartbeatMillis')},
            'participant':own,'approvedCount':sum(p['decision']=='approved' for p in session['participants']), 'participantCount':len(session['participants'])}

    def participant_poll(self,token):
        identity=self._identity(token)
        # Commit stops even when the subsequent response/action is rejected.
        with self.transaction() as (db,now):
            row=self._traffic_refresh(db,self._load(db,identity['session_id'],identity['owner']),now)
            db.execute('UPDATE curb_participants SET last_seen=? WHERE token=?',(now,token))
            return self._personal(self._response(row,now),identity['vehicle_id'])

    def participant_act(self, token, request_id, revision, action, move_request):
        identity=self._identity(token)
        if action not in ('approve','reject','withdraw'):raise ApiError(403,'participant_action_forbidden')
        self.state(identity['owner'])
        # Only a fresh poll acknowledges connectivity. Retrying a decision is not a heartbeat.
        with closing(self.connect()) as db:
            seen=db.execute('SELECT last_seen FROM curb_participants WHERE token=?',(token,)).fetchone()[0]
        if seen is None or not 0<=self.clock()-seen<=HEARTBEAT_MS:raise ApiError(409,'participant_poll_required')
        result=self.act(identity['session_id'],identity['owner'],request_id,revision,action,
            identity['vehicle_id'],move_request,participant_token=token)
        if result['session']['sessionId']!=identity['session_id']:
            with self.transaction() as (db,now):
                result=self._response(self._load(db,identity['session_id'],identity['owner']),now)
        return self._personal(result,identity['vehicle_id'])
