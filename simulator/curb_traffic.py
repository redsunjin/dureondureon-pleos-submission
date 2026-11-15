"""Constant-velocity moving rectangles against a timed parking trajectory.

Synthetic research: no sensor inference, braking dynamics or background control.
Each interval bounds BOTH moving bodies, not just sampled endpoints. Forecasts
have a finite horizon and a growing position margin. Stale live coverage holds.
"""
from bisect import bisect_right
from dataclasses import asdict
import math
from simulator import curb_parking as cp

HORIZON = 12.0  # virtual seconds; the demo world runs at the session's rate
FRESH_MS = 1500
CLEAR_MS = 1000  # real milliseconds of uninterrupted forecast clearance
GROWTH = .02  # additional assumed position uncertainty / virtual second


def validate(tracks, source, now, length, width, rate):
    if source not in ('observed','scripted') or not isinstance(tracks,list) or len(tracks)>8:
        raise ValueError('invalid_traffic')
    seen=set();clean=[]
    def number(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
    for t in tracks:
        if not isinstance(t,dict) or set(t)!={'id','kind','pose','length','width','vx','vy','observedAtMillis','durationSeconds'}:
            raise ValueError('invalid_traffic')
        identifier=t['id'];p=t['pose']
        if not isinstance(identifier,str) or not 1<=len(identifier)<=60 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in identifier) or identifier in seen:
            raise ValueError('invalid_traffic_id')
        seen.add(identifier)
        if t['kind'] not in ('pedestrian','vehicle') or not isinstance(p,dict) or set(p)!={'x','y','yaw'} or not all(number(v) for v in p.values()):
            raise ValueError('invalid_traffic')
        if abs(p['x'])>100 or abs(p['y'])>100 or abs(p['yaw'])>math.pi or not all(number(t[k]) for k in ('length','width','vx','vy','durationSeconds')):
            raise ValueError('invalid_traffic')
        if not 0<t['length']<=8 or not 0<t['width']<=3 or math.hypot(t['vx'],t['vy'])>10:
            raise ValueError('invalid_traffic')
        stamp=t['observedAtMillis']
        if not isinstance(stamp,int) or isinstance(stamp,bool) or stamp>now or stamp<0:
            raise ValueError('invalid_traffic_timestamp')
        if source=='scripted':
            if stamp!=now or not 0<t['durationSeconds']<=60:
                raise ValueError('invalid_script')
            end=cp.Pose(p['x']+t['vx']*t['durationSeconds'],p['y']+t['vy']*t['durationSeconds'],p['yaw'])
            poly=cp.corners(cp.Obstacle(identifier,end,t['length'],t['width']))
            # Scripted completion may clear only a known actor entirely outside
            # the road. Live observations NEVER clear merely by extrapolation.
            if not (max(x for x,y in poly)<-.5 or min(x for x,y in poly)>length+.5 or max(y for x,y in poly)<-.5 or min(y for x,y in poly)>width+.5):
                raise ValueError('script_must_exit_road')
        elif t['durationSeconds']!=0:
            raise ValueError('invalid_live_duration')
        clean.append({**t,'pose':dict(p)})
    return {'source':source,'tracks':clean,'receivedAtMillis':now,'clearSinceMillis':None,'safety':'clearing','revision':1}


def position(track, stamp, rate):
    dt=max(0,(stamp-track['observedAtMillis'])/1000*rate)
    p=track['pose']
    return cp.Pose(p['x']+track['vx']*dt,p['y']+track['vy']*dt,p['yaw'])


def live_tracks(traffic, now, rate):
    return [t for t in traffic['tracks'] if traffic['source']!='scripted' or
            (now-t['observedAtMillis'])/1000*rate<t['durationSeconds']]


def stale(traffic,now):
    if now<traffic['receivedAtMillis']:return True
    return traffic['source']=='observed' and (now-traffic['receivedAtMillis']>FRESH_MS or
        any(now-t['observedAtMillis']>FRESH_MS for t in traffic['tracks']))


def forecast(data, elapsed, traffic, now, rate, *, travel=True, extra=0.0, anchor=None):
    """Conservative timed intervals from the authoritative COMMAND boundary.

    extra covers all progress proposed since the preceding server checkpoint,
    in addition to the lookahead. The caller stops BEFORE advancing on risk.
    """
    from simulator.curb_replan import scene_from_dict
    scene=scene_from_dict(data['scene']);cars=cp._bodies(scene)
    if stale(traffic,now):return {'safety':'stale','riskId':None,'timeToRiskSeconds':None,'minimumClearance':None}
    tracks=live_tracks(traffic,now,rate)
    if not tracks:return {'safety':'clear','riskId':None,'timeToRiskSeconds':None,'minimumClearance':None}
    frames=data['frames'];commands=data['plan']['commands']
    index=max(0,min(len(commands),bisect_right([f['time'] for f in frames],elapsed+1e-6)-1))
    if data.get('observation') and not travel:
        initial={i:cp.Pose(**p) for i,p in data['observation']['poses'].items()}
    else:initial={i:cp.Pose(**p) for i,p in frames[index]['poses'].items()}
    horizon=HORIZON+extra;offset=0.0;poses=initial;minimum=math.inf
    while offset<horizon-1e-8:
        if travel and index<len(commands):
            command=cp.Command(**commands[index]);duration=command.duration
            bounds={i:cp.motion_bound(c,command) if i==command.actor else 0.0 for i,c in cars.items()}
        else:
            command=None;duration=.1;bounds={i:0.0 for i in cars}
        for t in tracks:
            stamp=(now if anchor is None else anchor)+offset/rate*1000
            if traffic['source']=='scripted' and (stamp-t['observedAtMillis'])/1000*rate>=t['durationSeconds']:continue
            p=position(t,stamp,rate);obstacle=cp.Obstacle(t['id'],p,t['length'],t['width']);poly=cp.corners(obstacle)
            obstacle_bound=math.hypot(t['vx'],t['vy'])*duration+GROWTH*(offset+duration)+1e-4
            bx=(min(x for x,y in poly),max(x for x,y in poly),min(y for x,y in poly),max(y for x,y in poly))
            for i,car in cars.items():
                body=cp.corners(car,poses[i]);margin=scene.policy.separation+obstacle_bound+bounds[i]
                ax=(min(x for x,y in body),max(x for x,y in body),min(y for x,y in body),max(y for x,y in body))
                if ax[0]>bx[1]+margin or bx[0]>ax[1]+margin or ax[2]>bx[3]+margin or bx[2]>ax[3]+margin:continue
                clearance=cp.distance(body,poly)-obstacle_bound-bounds[i]
                minimum=min(minimum,clearance)
                if clearance<scene.policy.separation-cp.EPS:
                    return {'safety':'risk','riskId':t['id'],'timeToRiskSeconds':round(offset,3),
                            'minimumClearance':round(clearance,5)}
        if command:
            poses={**poses,command.actor:cp.advance(cars[command.actor],poses[command.actor],command.distance,command.curvature)}
            index+=1
        offset+=duration
    return {'safety':'clear','riskId':None,'timeToRiskSeconds':None,
            'minimumClearance':None if minimum==math.inf else round(minimum,5)}


def response(traffic,now,rate):
    if not traffic:return {'safety':'disabled','source':None,'tracks':[],'horizonSeconds':HORIZON,'safeToReplan':True,'clearRemainingMillis':0}
    safety=traffic['safety'];since=traffic.get('clearSinceMillis')
    remaining=max(0,CLEAR_MS-(now-since)) if since is not None else CLEAR_MS
    tracks=[]
    for t in live_tracks(traffic,now,rate):
        pose=position(t,now,rate)
        tracks.append({**{k:t[k] for k in ('id','kind','length','width','vx','vy')},'pose':asdict(pose),
            'predictedPose':asdict(cp.Pose(pose.x+t['vx']*HORIZON,pose.y+t['vy']*HORIZON,pose.yaw))})
    return {'safety':safety,'source':traffic['source'],'tracks':tracks,'horizonSeconds':HORIZON,
            'safeToReplan':safety=='clear','clearRemainingMillis':remaining,
            'riskId':traffic.get('riskId'),'timeToRiskSeconds':traffic.get('timeToRiskSeconds'),
            'minimumClearance':traffic.get('minimumClearance'),'revision':traffic['revision']}


def scripted(scenario, data, now):
    """Explicit demo trajectories; never presented as live sensor evidence."""
    if scenario=='clear':return []
    if scenario=='pedestrian':
        gap=data['plan']['gap_index'];cars=sorted(data['participants'][:-1],key=lambda c:c['initialPose']['x'])
        left=cars[gap-1]['openingX']+cars[gap-1]['length']/2 if gap else .2
        right=cars[gap]['openingX']-cars[gap]['length']/2 if gap<len(cars) else data['sceneLength']-.2
        return [{'id':'DEMO-PED','kind':'pedestrian','pose':{'x':(left+right)/2,'y':8.0,'yaw':-math.pi/2},
            'length':.6,'width':.6,'vx':0.0,'vy':-.6,'observedAtMillis':now,'durationSeconds':16.0}]
    if scenario=='vehicle':
        return [{'id':'DEMO-CAR','kind':'vehicle','pose':{'x':-5.0,'y':6.55,'yaw':0.0},
            'length':4.0,'width':1.2,'vx':2.0,'vy':0.0,'observedAtMillis':now,'durationSeconds':20.0}]
    raise ValueError('unknown_traffic_scenario')
