"""Device-free delegated execution contract. Not a physical vehicle controller.

All mutations are trusted local orchestration calls; vehicle reports use scoped
simulation credentials. No HTTP/UI exposure or reuse of the legacy replay clock.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from coordination.errors import ApiError, identifier

ACTIVE={'awaiting_ready','ready','running','stop_requested'}
HEARTBEAT_MS=3000
EPS=1e-6


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def number(v):return type(v) in (int,float) and math.isfinite(v)
def pose_ok(p):return isinstance(p,dict) and set(p)=={'x','y','yaw'} and all(number(v) for v in p.values())
def close_pose(a,b):return math.hypot(a['x']-b['x'],a['y']-b['y'])<=0.02 and abs(a['yaw']-b['yaw'])<=0.005


def expected(command,travel):
    p=command['source'];offset=command['offset'];k=command['curvature']
    ds=math.copysign(travel,command['distance']);yaw=p['yaw']+ds*k
    x=p['x']-offset*math.cos(p['yaw']);y=p['y']-offset*math.sin(p['yaw'])
    if abs(k)<1e-10:x+=ds*math.cos(p['yaw']);y+=ds*math.sin(p['yaw'])
    else:x+=(math.sin(yaw)-math.sin(p['yaw']))/k;y+=(math.cos(p['yaw'])-math.cos(yaw))/k
    return dict(x=x+offset*math.cos(yaw),y=y+offset*math.sin(yaw),yaw=yaw)


class ExecutionLedger:
    def __init__(self,path,clock=None):
        self.path=str(path);self.clock=clock or (lambda:time.time_ns()//1_000_000)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS delegated_execution (id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO delegated_execution VALUES(1,?)',(canonical(dict(grants={},usage={},agents={},poses={},plans={},events=[],receipts={})),));db.commit()
        def restart(d,now):
            for p in d['plans'].values():
                if p['status'] in ACTIVE:self._halt(d,p,'coordinator_restarted',now)
            return {'restarted':True}
        self._run(None,['restart'],restart)

    @staticmethod
    def _event(d,now,kind,**fields):d['events'].append(dict(sequence=len(d['events'])+1,at=now,kind=kind,**fields))

    def _run(self,request,payload,fn):
        if request is not None:identifier(request)
        try:fingerprint=digest(payload)
        except (TypeError,ValueError,OverflowError):raise ApiError(400,'invalid_execution_payload') from None
        with closing(sqlite3.connect(self.path,timeout=10)) as db:
            db.execute('BEGIN IMMEDIATE')
            d=json.loads(db.execute('SELECT data FROM delegated_execution WHERE id=1').fetchone()[0]);now=self.clock()
            self._refresh(d,now);safe=deepcopy(d);error=None;result=None
            try:
                receipt=d['receipts'].get(request) if request else None
                if receipt:
                    if receipt['fingerprint']!=fingerprint:raise ApiError(409,'request_id_reused')
                    result=deepcopy(receipt['result']);result['receiptReplayed']=True
                else:
                    result=fn(d,now)
                    if request:
                        result['receiptAt']=now
                        d['receipts'][request]=dict(fingerprint=fingerprint,result=deepcopy(result))
                        result['receiptReplayed']=False
            except ApiError as e:d=safe;error=e
            except Exception:
                db.rollback();raise
            db.execute('UPDATE delegated_execution SET data=? WHERE id=1',(canonical(d),));db.commit()
            if error:raise error
            return deepcopy(result)

    @staticmethod
    def _plan(d,pid):
        p=d['plans'].get(pid)
        if p is None:raise ApiError(404,'execution_plan_not_found')
        return p

    @staticmethod
    def _auth(d,token,vehicle):
        if not isinstance(token,str) or d['agents'].get(vehicle,{}).get('key')!=hashlib.sha256(token.encode()).hexdigest():
            raise ApiError(403,'vehicle_identity_mismatch')

    @staticmethod
    def _bound(p,vehicle,plan_digest,version):
        if type(version)!=int or p['digest']!=plan_digest or p['versions'].get(vehicle)!=version:raise ApiError(409,'execution_binding_changed')

    @staticmethod
    def _valid_grants(d,p,now):
        return all(v in d['grants'] and d['grants'][v]['version']==version and not d['grants'][v]['revoked']
            and d['grants'][v]['validFrom']<=now<d['grants'][v]['validUntil'] and d['grants'][v]['zone']==p['zone']
            for v,version in p['versions'].items())

    @staticmethod
    def _current(p):return p['commands'][p['index']] if p['index']<len(p['commands']) else None

    @staticmethod
    def _unreserve(usage,distance):
        remaining=usage['reserved']-distance
        usage['reserved']=0.0 if abs(remaining)<1e-9 else max(0.0,remaining)

    def _settle(self,d,c):
        if c['settled']:return
        u=d['usage'][c['vehicle']];self._unreserve(u,abs(c['distance']));u['used']+=c['travel'];c['settled']=True

    def _release(self,d,p):
        for c in p['commands']:
            if not c['settled']:
                u=d['usage'][c['vehicle']];self._unreserve(u,abs(c['distance']));c['settled']=True
                if c['status']=='pending':c['status']='cancelled'

    def _halt(self,d,p,reason,now):
        if p['status'] not in ACTIVE:return
        c=self._current(p)
        if c and c['status'] in ('dispatched','accepted','executing','completed'):
            p.update(status='stop_requested',reason=reason,motionKnowledge='unknown')
        else:
            p.update(status='stopped' if p['index'] else 'cancelled',reason=reason,
                motionKnowledge='reported_stationary' if p['index'] else 'not_commanded');self._release(d,p)
        p['ready']={};self._event(d,now,'halt',plan=p['id'],reason=reason,status=p['status'])

    def _refresh(self,d,now):
        from simulator.execution_traffic import refresh as traffic_refresh
        for p in d['plans'].values():
            safety=traffic_refresh(p,d['poses'],now)
            if p['status'] not in ACTIVE or p['status']=='stop_requested':continue
            if safety in ('risk','stale','unknown') or p['status']=='running' and safety in ('missing','clearing'):
                self._halt(d,p,'traffic_'+safety,now);continue
            if not self._valid_grants(d,p,now):self._halt(d,p,'delegation_changed_or_expired',now);continue
            if now>=p['expires']:self._halt(d,p,'execution_plan_expired',now);continue
            if p['status'] in ('ready','running') and any(not 0<=now-d['agents'][v]['seen']<=HEARTBEAT_MS for v in p['versions']):
                self._halt(d,p,'vehicle_connection_lost',now);continue
            c=self._current(p)
            if c and c['status'] in ('dispatched','accepted','executing') and now>=c['deadline']:
                self._halt(d,p,'execution_response_missing',now)

    def register_agent(self,vehicle,request):
        vehicle=identifier(vehicle)
        def apply(d,now):
            if vehicle in d['agents']:raise ApiError(409,'agent_already_registered')
            token=secrets.token_urlsafe(32);d['agents'][vehicle]=dict(key=hashlib.sha256(token.encode()).hexdigest(),seen=-1)
            return dict(vehicle=vehicle,token=token,simulationOnly=True)
        return self._run(request,['register',vehicle],apply)

    def delegate(self,vehicle,owner,zone,valid_from,valid_until,per_plan,total,forward,backward,yaw,request):
        vehicle,owner,zone=map(identifier,(vehicle,owner,zone))
        if any(not number(v) or v<0 for v in (per_plan,total,forward,backward,yaw)) or per_plan<=0 or total<=0 or type(valid_from)!=int or type(valid_until)!=int or valid_until<=valid_from:
            raise ApiError(400,'invalid_delegation')
        def apply(d,now):
            old=d['grants'].get(vehicle)
            if old and old['owner']!=owner:raise ApiError(403,'delegation_owner_mismatch')
            g=dict(owner=owner,zone=zone,validFrom=valid_from,validUntil=valid_until,perPlan=per_plan,total=total,forward=forward,backward=backward,yaw=yaw,revoked=False,version=1 if old is None else old['version']+1)
            d['grants'][vehicle]=g;d['usage'].setdefault(vehicle,dict(used=0.0,reserved=0.0))
            self._refresh(d,now);self._event(d,now,'delegation',vehicle=vehicle,version=g['version'],grant=deepcopy(g))
            return deepcopy(g)
        return self._run(request,['delegate',vehicle,owner,zone,valid_from,valid_until,per_plan,total,forward,backward,yaw],apply)

    def revoke(self,vehicle,owner,request):
        def apply(d,now):
            if vehicle not in d['grants']:raise ApiError(404,'delegation_not_found')
            g=d['grants'][vehicle]
            if g['owner']!=owner:raise ApiError(403,'delegation_owner_mismatch')
            g['version']+=1;g['revoked']=True;self._refresh(d,now)
            self._event(d,now,'delegation_revoked',vehicle=vehicle,version=g['version']);return dict(version=g['version'])
        return self._run(request,['revoke',vehicle,owner],apply)

    def propose(self,artifact,zone,expires,request,require_traffic=False):
        """Accept only the local simulator adapter's independently validated artifact.

        This is an internal trust boundary, not arbitrary remote trajectory input.
        """
        from simulator.execution_trajectory import ValidatedTrajectory
        if not isinstance(artifact,ValidatedTrajectory):raise ApiError(400,'validated_trajectory_required')
        zone=identifier(zone)
        if type(require_traffic)!=bool:raise ApiError(400,'invalid_traffic_requirement')
        if type(expires)!=int:raise ApiError(400,'invalid_execution_expiry')
        source=artifact.as_dict()
        def apply(d,now):
            if expires<=now:raise ApiError(400,'invalid_execution_expiry')
            initial=source['initial'];commands=deepcopy(source['commands']);vehicles=set(initial)
            if not commands:raise ApiError(400,'empty_execution_plan')
            for other in d['plans'].values():
                if other['status'] in ACTIVE and (other['zone']==zone or vehicles&set(other['initial'])):raise ApiError(409,'execution_resource_busy')
            for v,pose in initial.items():
                if v in d['poses'] and not close_pose(d['poses'][v],pose):raise ApiError(409,'execution_pose_changed')
            metrics={};versions={}
            for c in commands:
                m=metrics.setdefault(c['vehicle'],dict(total=0,forward=0,backward=0,yaw=0));cost=abs(c['distance'])
                m['total']+=cost;m['forward' if c['distance']>=0 else 'backward']+=cost;m['yaw']+=cost*abs(c['curvature'])
            for v,m in metrics.items():
                g=d['grants'].get(v);u=d['usage'].get(v)
                if v not in d['agents'] or g is None or g['revoked'] or g['zone']!=zone or not g['validFrom']<=now<g['validUntil']:raise ApiError(403,'delegation_unavailable')
                if m['total']>g['perPlan']+EPS or u['used']+u['reserved']+m['total']>g['total']+EPS or any(m[k]>g[k]+EPS for k in ('forward','backward','yaw')):raise ApiError(409,'delegation_budget_exceeded')
                versions[v]=g['version'];u['reserved']+=m['total']
            pid=str(uuid.uuid4());bound_digest=digest(dict(source=source,zone=zone,versions=versions,expires=expires,planId=pid,requireTraffic=require_traffic))
            for i,c in enumerate(commands):
                c.update(id=f'{pid}_{i+1}',sequence=i+1,status='pending',eventSequence=0,travel=0.0,settled=False,deadline=None)
            p=dict(id=pid,digest=bound_digest,zone=zone,expires=expires,versions=versions,initial=initial,commands=commands,index=0,ready={},status='awaiting_ready',reason='vehicle_readiness_required',motionKnowledge='not_commanded',simulationOnly=True,trajectorySource=source['sourceSignature'],certifiedClearance=source['certifiedClearance'],geometry=source['geometry'],trafficRequired=require_traffic,traffic=None)
            d['plans'][pid]=p;d['poses'].update(deepcopy(initial));self._event(d,now,'proposed',plan=pid)
            return dict(planId=pid,digest=bound_digest,versions=versions)
        return self._run(request,['propose',source,zone,expires,require_traffic],apply)

    def heartbeat(self,vehicle,token,request):
        def apply(d,now):
            self._auth(d,token,vehicle);d['agents'][vehicle]['seen']=now
            return dict(connectedAt=now,readiness=False,executionEvidence=False)
        return self._run(request,['heartbeat',vehicle,token],apply)

    def ready(self,pid,vehicle,token,plan_digest,grant_version,request):
        def apply(d,now):
            self._auth(d,token,vehicle);p=self._plan(d,pid);self._bound(p,vehicle,plan_digest,grant_version)
            if p['status'] not in ('awaiting_ready','ready'):raise ApiError(409,'execution_plan_not_preparable')
            if not 0<=now-d['agents'][vehicle]['seen']<=HEARTBEAT_MS:raise ApiError(409,'vehicle_heartbeat_required')
            p['ready'][vehicle]=True
            if len(p['ready'])==len(p['versions']):p['status']='ready'
            self._event(d,now,'vehicle_ready',plan=pid,vehicle=vehicle);return dict(ready=True)
        return self._run(request,['ready',pid,vehicle,token,plan_digest,grant_version],apply)

    def start(self,pid,request):
        def apply(d,now):
            p=self._plan(d,pid)
            if p['status']!='ready':raise ApiError(409,'vehicle_readiness_incomplete')
            if p.get('trafficSafety',{}).get('safety','disabled') not in ('disabled','clear'):raise ApiError(409,'traffic_not_clear')
            p.update(status='running',reason='awaiting_dispatch');return dict(started=True)
        return self._run(request,['start',pid],apply)

    def dispatch(self,pid,request):
        def apply(d,now):
            p=self._plan(d,pid);c=self._current(p)
            if p['status']!='running' or c is None or c['status']!='pending':raise ApiError(409,'previous_command_unconfirmed')
            c.update(status='dispatched',deadline=min(p['expires'],now+max(3000,int(c['duration']*1000)+3000)))
            p.update(reason='command_response_required',motionKnowledge='unknown')
            self._event(d,now,'dispatched',plan=pid,command=c['id'],vehicle=c['vehicle'])
            return dict(planId=pid,planDigest=p['digest'],grantVersion=p['versions'][c['vehicle']],command=deepcopy(c))
        return self._run(request,['dispatch',pid],apply)

    def accept(self,pid,vehicle,token,plan_digest,version,command_id,sequence,request):
        def apply(d,now):
            self._auth(d,token,vehicle);p=self._plan(d,pid);self._bound(p,vehicle,plan_digest,version);c=self._current(p)
            if type(sequence)!=int or p['status']!='running' or c is None or (c['id'],c['sequence'],c['vehicle'],c['status'])!=(command_id,sequence,vehicle,'dispatched'):raise ApiError(409,'invalid_command_acceptance')
            c['status']='accepted';self._event(d,now,'command_accepted',plan=pid,command=command_id)
            return dict(accepted=True,commandId=command_id)
        return self._run(request,['accept',pid,vehicle,token,plan_digest,version,command_id,sequence],apply)

    def stop(self,pid,request):
        def apply(d,now):
            p=self._plan(d,pid);self._halt(d,p,'coordinator_stop_requested',now);return dict(status=p['status'])
        return self._run(request,['stop',pid],apply)

    def report(self,pid,vehicle,token,plan_digest,version,command_id,sequence,event_sequence,kind,travel,pose,request):
        if kind not in ('executing','completed','stopped') or not number(travel) or travel<0 or not pose_ok(pose) or type(event_sequence)!=int or type(sequence)!=int:raise ApiError(400,'invalid_execution_report')
        def apply(d,now):
            self._auth(d,token,vehicle);p=self._plan(d,pid);self._bound(p,vehicle,plan_digest,version);c=self._current(p)
            if p['status'] not in ('running','stop_requested') or c is None or (c['id'],c['vehicle'],c['sequence'])!=(command_id,vehicle,sequence):raise ApiError(409,'execution_report_mismatch')
            if event_sequence!=c['eventSequence']+1 or not c['travel']-EPS<=travel<=abs(c['distance'])+EPS:raise ApiError(409,'execution_report_out_of_order')
            if not close_pose(expected(c,travel),pose):raise ApiError(409,'execution_pose_mismatch')
            if kind=='executing' and (p['status']!='running' or c['status'] not in ('accepted','executing')):raise ApiError(409,'execution_not_accepted')
            if kind=='completed' and (c['status']!='executing' or abs(travel-abs(c['distance']))>EPS or not close_pose(c['target'],pose)):raise ApiError(409,'execution_completion_unproven')
            if kind=='stopped' and c['status'] not in ('dispatched','accepted','executing','completed'):raise ApiError(409,'execution_not_commanded')
            c.update(eventSequence=event_sequence,travel=travel,status=kind);d['poses'][vehicle]=deepcopy(pose)
            self._event(d,now,kind,plan=pid,vehicle=vehicle,command=command_id,eventSequence=event_sequence,travel=travel,pose=pose)
            if kind in ('completed','stopped'):self._settle(d,c)
            if kind=='stopped':
                p.update(status='stopped',reason='vehicle_stop_confirmed',motionKnowledge='reported_stationary');self._release(d,p)
            elif kind=='completed' and p['status']=='running':
                p['index']+=1;p['motionKnowledge']='reported_stationary'
                if p['index']==len(p['commands']):p.update(status='completed',reason='all_vehicle_completions_confirmed')
            return dict(recorded=True,planStatus=p['status'],eventSequence=event_sequence)
        return self._run(request,['report',pid,vehicle,token,plan_digest,version,command_id,sequence,event_sequence,kind,travel,pose],apply)

    def observe_traffic(self,pid,revision,observed_at,tracks,request):
        if type(revision)!=int or revision<0 or type(observed_at)!=int or observed_at<0:
            raise ApiError(400,'invalid_traffic_frame')
        def apply(d,now):
            from simulator import curb_traffic as ct
            p=self._plan(d,pid)
            if 'geometry' not in p:raise ApiError(409,'traffic_geometry_unavailable')
            previous=p.get('traffic')
            if revision!=(previous['revision'] if previous else 0):raise ApiError(409,'traffic_revision_changed')
            if observed_at>now:raise ApiError(400,'traffic_frame_in_future')
            if previous and observed_at<=previous['observedAtMillis']:raise ApiError(409,'traffic_frame_out_of_order')
            try:clean=ct.validate(tracks,'observed',now,p['geometry']['length'],p['geometry']['width'],1)
            except (ValueError,TypeError,KeyError):raise ApiError(400,'invalid_traffic_frame') from None
            if any(t['observedAtMillis']>observed_at for t in clean['tracks']):raise ApiError(400,'invalid_traffic_frame')
            ids=set(p['geometry']['bodies'])|set(p['geometry']['obstacleIds'])
            if any(t['id'] in ids for t in clean['tracks']):raise ApiError(400,'traffic_body_identity_collision')
            clean.update(revision=revision+1,observedAtMillis=observed_at)
            if previous:clean['clearSinceMillis']=previous.get('clearSinceMillis')
            p.update(traffic=clean,trafficRequired=True)
            self._refresh(d,now)
            self._event(d,now,'traffic_observed',plan=pid,trafficRevision=clean['revision'],observedAt=observed_at,safety=p['trafficSafety']['safety'],tracks=deepcopy(clean['tracks']))
            return dict(trafficRevision=clean['revision'],safety=p['trafficSafety']['safety'])
        return self._run(request,['traffic',pid,revision,observed_at,tracks],apply)

    def state(self,pid=None):
        def read(d,now):
            public={k:deepcopy(d[k]) for k in ('grants','usage','poses','events')}
            public.update(simulationOnly=True,serverTime=now,plans=deepcopy(d['plans']))
            return public if pid is None else deepcopy(self._plan(d,pid))
        return self._run(None,['state',pid],read)
