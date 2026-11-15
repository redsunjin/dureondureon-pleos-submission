"""Conservative traffic coverage for acknowledged execution, without clock replay.

The active/next command can be anywhere along its unconfirmed remaining arc.
Intersect its entire swept body against each obstacle's next 12 seconds. This
intentionally loses time alignment and can overestimate risk; never infer car
progress from wall time. All thresholds are simulation assumptions.
"""
import math
from coordination.execution import expected
from simulator import curb_parking as cp
from simulator import curb_traffic as ct

HORIZON=12.0
STEP=.5
SPATIAL_STEP=.25


def box(poly):return (min(x for x,y in poly),max(x for x,y in poly),min(y for x,y in poly),max(y for x,y in poly))
def apart(a,b,margin):return a[0]>b[1]+margin or b[0]>a[1]+margin or a[2]>b[3]+margin or b[2]>a[3]+margin


def evaluate(plan,poses,now):
    traffic=plan.get('traffic')
    if not traffic:return dict(safety='missing' if plan.get('trafficRequired') else 'disabled',riskId=None,riskVehicle=None,minimumClearanceBound=None)
    if ct.stale(traffic,now) or now<traffic['observedAtMillis'] or now-traffic['observedAtMillis']>ct.FRESH_MS:
        return dict(safety='stale',riskId=None,riskVehicle=None,minimumClearanceBound=None)
    if not traffic['tracks']:return dict(safety='clear',riskId=None,riskVehicle=None,minimumClearanceBound=None)
    geometry=plan['geometry'];segments=[]
    command=plan['commands'][plan['index']] if plan['index']<len(plan['commands']) else None
    for vehicle,spec in geometry['bodies'].items():
        car=cp.Car(vehicle,cp.Pose(**poses[vehicle]),length=spec['length'],width=spec['width'],rear_overhang=spec['rearOverhang'])
        radius=math.hypot(max(car.rear_overhang,car.length-car.rear_overhang),car.width/2)
        uncertainty=.02+radius*.005
        if command and command['vehicle']==vehicle and command['status'] in ('pending','dispatched','accepted','executing'):
            left=max(0,abs(command['distance'])-command['travel'])
            bound=left*(1+radius*abs(command['curvature']))
            count=max(1,math.ceil(bound/SPATIAL_STEP))
            if count>2000:return dict(safety='unknown',riskId=None,riskVehicle=vehicle,minimumClearanceBound=None)
            for i in range(count):
                pose=cp.Pose(**expected(command,command['travel']+left*i/count));poly=cp.corners(car,pose)
                segments.append((vehicle,poly,box(poly),bound/count+uncertainty))
        else:
            poly=cp.corners(car);segments.append((vehicle,poly,box(poly),uncertainty))
    minimum=math.inf
    for track in traffic['tracks']:
        speed=math.hypot(track['vx'],track['vy'])
        for index in range(math.ceil(HORIZON/STEP)):
            t=index*STEP
            pose=ct.position(track,now+t*1000,1)
            poly=cp.corners(cp.Obstacle(track['id'],pose,track['length'],track['width']));bounds=box(poly)
            age=max(0,(now-track['observedAtMillis'])/1000)
            moving_bound=speed*STEP+ct.GROWTH*(age+t+STEP)+1e-4
            for vehicle,body,body_bounds,body_bound in segments:
                margin=geometry['separation']+moving_bound+body_bound
                if apart(bounds,body_bounds,margin):continue
                clearance=cp.distance(body,poly)-moving_bound-body_bound;minimum=min(minimum,clearance)
                if clearance<geometry['separation']-cp.EPS:
                    return dict(safety='risk',riskId=track['id'],riskVehicle=vehicle,riskWindowSeconds=t,minimumClearanceBound=round(clearance,5))
    return dict(safety='clear',riskId=None,riskVehicle=None,minimumClearanceBound=None if minimum==math.inf else round(minimum,5))


def refresh(plan,poses,now):
    result=evaluate(plan,poses,now);traffic=plan.get('traffic')
    if traffic:
        if result['safety']=='clear':
            since=traffic.get('clearSinceMillis')
            if since is None or now<since:since=now
            traffic['clearSinceMillis']=since
            if now-since<ct.CLEAR_MS:result['safety']='clearing'
        else:traffic['clearSinceMillis']=None
        traffic.update(result,evaluatedAtMillis=now)
    plan['trafficSafety']=dict(result,horizonSeconds=HORIZON,forecastPolicy='untimed_current_command_sweep',
        clearRemainingMillis=max(0,ct.CLEAR_MS-(now-traffic['clearSinceMillis'])) if traffic and traffic.get('clearSinceMillis') is not None else ct.CLEAR_MS)
    return result['safety']
