"""Bounded changed-scene search. Synthetic poses only; failure is a hold.

Search new straight/curved prefixes and exact two-arc goal connectors, rather
than reusing the old command suffix. Every accepted interval is certified again
by curb_parking.execute. This is neither complete nor globally optimal.
"""
from dataclasses import replace
import heapq
import itertools
import math
import time
from simulator import curb_parking as cp


def observed_scene(scene, poses, obstacles):
    """Keep ORIGINAL absolute parked-car envelopes, including after repeats."""
    parked = []
    for car in scene.parked:
        pose = cp.Pose(**poses[car.id])
        low, high = car.pose.x-car.max_backward, car.pose.x+car.max_forward
        if not low-cp.EPS <= pose.x <= high+cp.EPS:
            raise ValueError('observed_vehicle_outside_envelope')
        parked.append(replace(car, pose=pose, max_backward=max(0,pose.x-low),
                              max_forward=max(0,high-pose.x), observed_at=0))
    updated = replace(scene, name='환경 변화 후 협력 주차', parked=tuple(parked),
        incoming=replace(scene.incoming, pose=cp.Pose(**poses[scene.incoming.id]), observed_at=0),
        obstacles=tuple(obstacles), observed_at=0)
    if cp._configuration_error(updated, True):
        raise ValueError('unsupported_observed_scene')
    initial = {c.id:c.pose for c in (*updated.parked, updated.incoming)}
    clearance, wall = cp.clearance_state(updated,initial)
    if clearance < updated.policy.separation-cp.EPS or wall < updated.policy.boundary-cp.EPS:
        raise ValueError('observed_clearance_violation')
    return updated


def _connector(scene, start, target):
    """Exact bicycle goal connectors: two arcs then straight at yaw=0."""
    car, p = scene.incoming, scene.policy
    for factor in (1.0, .8, .6):
        for sign in (-1,1):
            k = sign*factor/car.radius
            rear_dy = target.y-start.y+car.centre_offset*math.sin(start.yaw)
            cosine = (math.cos(start.yaw)+1-k*rear_dy)/2
            if not -1 <= cosine <= 1:
                continue
            for phi in (math.acos(cosine), -math.acos(cosine)):
                d1, d2 = (phi-start.yaw)/k, phi/k
                if max(abs(d1),abs(d2))>12 or d1*d2 < -cp.EPS:
                    continue
                # Goal arcs reverse into the curb; forward moves are searched
                # separately in the prefix.
                if d1>cp.EPS or d2>cp.EPS:
                    continue
                end = cp.advance(car, cp.advance(car,start,d1,k),d2,-k)
                if abs(end.y-target.y)>1e-6 or abs(end.yaw)>1e-6:
                    continue
                segments = [(d1,k),(d2,-k),(target.x-end.x,0.0)]
                if sum(abs(d) for d,_ in segments)>22:
                    continue
                yield segments


def _path(scene, poses, target, deadline, max_nodes=180):
    car, p = scene.incoming, scene.policy
    start=poses[car.id]
    # Static polygons and broad-phase bounds are reused across prefix trials.
    fixed=[cp.corners(c,poses[c.id]) for c in scene.parked]+[cp.corners(o) for o in scene.obstacles]
    boxes=[(min(x for x,y in poly),max(x for x,y in poly),min(y for x,y in poly),max(y for x,y in poly)) for poly in fixed]
    def move(pose, segments):
        for travel,curvature in segments:
            for command in cp._commands(car,travel,curvature,p.reverse_speed,'replan_move',p):
                if abs(curvature)<cp.EPS:
                    end=cp.advance(car,pose,command.distance,0)
                    poly=cp.corners(cp.Obstacle('sweep',cp.Pose((pose.x+end.x)/2,(pose.y+end.y)/2,pose.yaw),car.length+abs(command.distance),car.width));bound=0
                else:
                    poly=cp.corners(car,pose);bound=cp.motion_bound(car,command)
                if min(min(x,scene.length-x,y,scene.road_width-y) for x,y in poly)-bound < p.boundary-cp.EPS:
                    return None
                margin=p.separation+bound
                box=(min(x for x,y in poly)-margin,max(x for x,y in poly)+margin,
                     min(y for x,y in poly)-margin,max(y for x,y in poly)+margin)
                for other,b in zip(fixed,boxes):
                    if box[0]>b[1] or b[0]>box[1] or box[2]>b[3] or b[2]>box[3]:continue
                    if cp.distance(poly,other)-bound < p.separation-cp.EPS:return None
                pose=cp.advance(car,pose,command.distance,curvature)
        return pose
    def key(pose):return round(pose.x/.35),round(pose.y/.25),round(pose.yaw/.09)
    def heuristic(pose):return math.hypot(pose.x-target.x,pose.y-target.y)+3*abs(pose.yaw)
    serial=itertools.count();queue=[(heuristic(start),next(serial),0.0,start,())];best={key(start):0.0}
    for _ in range(max_nodes):
        if time.monotonic()>=deadline:raise ValueError('changed_scene_search_budget')
        if not queue:break
        _,_,cost,pose,prefix=heapq.heappop(queue)
        if cost>best.get(key(pose),math.inf)+1e-8:continue
        for connector in _connector(scene,pose,target):
            end=move(pose,connector)
            if end is not None:return prefix+tuple(connector)
        if cost>10:continue
        for travel in (.6,-.6):
            for curvature in (0.0,-1/car.radius,1/car.radius):
                end=move(pose,[(travel,curvature)])
                if end is None or abs(end.yaw)>1.35:continue
                new_cost=cost+abs(travel)+(.18 if travel>0 else 0)+(.12 if prefix and prefix[-1][0]*travel<0 else 0)
                if new_cost>=best.get(key(end),math.inf)-1e-8:continue
                best[key(end)]=new_cost
                heapq.heappush(queue,(new_cost+heuristic(end)*1.4,next(serial),new_cost,end,prefix+((travel,curvature),)))
    return None


def plan(scene, preferred_gap=None):
    """New bounded route search from an independently validated observed scene."""
    deadline=time.monotonic()+5.0
    cars=tuple(sorted(scene.parked,key=lambda c:c.pose.x));p=scene.policy
    initial={c.id:c.pose for c in (*cars,scene.incoming)}
    if cp._configuration_error(scene,True):raise ValueError('invalid_observed_scene')
    minimum,wall=cp.clearance_state(scene,initial)
    if minimum<p.separation-cp.EPS or wall<p.boundary-cp.EPS:raise ValueError('observed_clearance_violation')
    # Aligned approaches have an exact existing search for the staging straight
    # and reverse arcs. Recompute it from the NEW scene before the prefix search;
    # a goal-distance heuristic alone can miss the necessary forward staging.
    if abs(scene.incoming.pose.yaw)<cp.EPS and scene.incoming.pose.y>scene.parking_y+cp.EPS:
        fresh=cp.plan(scene)
        if fresh.plan is not None:return fresh.plan
    minimum_opening=scene.incoming.length+2*(p.separation+.02)
    candidates={}
    for extra in (0,.5,1.0,1.5,2.0,2.5):
        for gap in range(len(cars)+1):
            for opening in cp._openings(scene,cars,gap,minimum_opening+extra,True):
                shifted=cp._redistribute(scene,cars,gap,minimum_opening+extra,opening,True)
                if shifted is None:continue
                left=shifted[cars[gap-1].id]+cars[gap-1].length/2 if gap else p.boundary
                right=shifted[cars[gap].id]-cars[gap].length/2 if gap<len(cars) else scene.length-p.boundary
                candidates[(gap,tuple(round(shifted[c.id],6) for c in cars))]=(sum(abs(shifted[c.id]-c.pose.x) for c in cars),gap,left,right,shifted)
    trials=0
    for _,gap,left,right,shifted in sorted(candidates.values(),key=lambda x:(x[1]!=preferred_gap,x[0],x[1]))[:12]:
        poses=dict(initial);commands=[];clear=True
        for car in (*cars[:gap],*reversed(cars[gap:])):
            for command in cp._commands(car,shifted[car.id]-car.pose.x,0,p.shift_speed,'create_space',p):
                if not cp._step_clear(scene,car,poses,command,scene.obstacles):clear=False;break
                poses[car.id]=cp.advance(car,poses[car.id],command.distance,0);commands.append(command)
            if not clear:break
        if not clear:continue
        for bias in (0,-.25,.25):
            trials+=1
            target=cp.Pose((left+right)/2+bias*max(0,right-left-minimum_opening),scene.parking_y)
            segments=_path(scene,poses,target,deadline)
            if segments is None:continue
            route=list(commands);last=None
            for travel,curvature in segments:
                if abs(travel)<cp.EPS:continue
                steering=(curvature,travel>0)
                if steering!=last:route.append(cp.Command(scene.incoming.id,0,0,.5,'steering_at_rest'))
                route+=cp._commands(scene.incoming,travel,curvature,p.reverse_speed,'replan_move',p);last=steering
            # Recompute settling from this new target and changed obstacles;
            # keep a car in place if its compacting movement is not certified.
            settled=dict(shifted);final_poses={**poses,scene.incoming.id:target}
            for side in (tuple(reversed(cars[:gap])),cars[gap:]):
                rear=bool(side) and side[0].pose.x<target.x
                edge=target.x+(-1 if rear else 1)*(scene.incoming.length/2+p.separation+.02)
                for car in side:
                    desired=(max(shifted[car.id],min(edge-car.length/2,car.pose.x+car.max_forward)) if rear
                        else min(shifted[car.id],max(edge+car.length/2,car.pose.x-car.max_backward)))
                    moves=cp._commands(car,desired-shifted[car.id],0,p.shift_speed,'settle_spacing',p)
                    trial=dict(final_poses)
                    for move in moves:
                        if not cp._step_clear(scene,car,trial,move,scene.obstacles):break
                        trial[car.id]=cp.advance(car,trial[car.id],move.distance,0)
                    else:
                        route+=moves;final_poses=trial;settled[car.id]=desired
                    edge=settled[car.id]+(-1 if rear else 1)*(car.length/2+p.separation+.02)
            original_left=cars[gap-1].pose.x+cars[gap-1].length/2 if gap else p.boundary
            original_right=cars[gap].pose.x-cars[gap].length/2 if gap<len(cars) else scene.length-p.boundary
            proposal=cp.Plan(cp.signature(scene),0,p.plan_ttl,gap,original_right-original_left,right-left,target,target,
                tuple((c.id,settled[c.id]) for c in cars),tuple((c.id,shifted[c.id]) for c in cars),tuple(route),trials)
            result=cp.execute(scene,proposal,continuation=True,include_frames=False)
            if result['status']=='parked':return proposal
    raise ValueError('changed_scene_search_exhausted')
