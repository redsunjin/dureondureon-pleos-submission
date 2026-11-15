"""Internal trajectory artifact, produced only after curb simulator validation."""
from copy import deepcopy
from dataclasses import asdict
from coordination.execution import close_pose
from simulator import curb_parking as cp


class ValidatedTrajectory:
    def __init__(self,scene,proposal=None,continuation=False):
        if proposal is None:
            proposal=cp.plan(scene).plan
            if proposal is None:raise ValueError('no validated curb plan')
        result=cp.execute(scene,proposal,continuation=continuation)
        if result['status']!='parked':raise ValueError('unvalidated curb trajectory')
        cars={car.id:car for car in (*scene.parked,scene.incoming)}
        initial={v:asdict(c.pose) for v,c in cars.items()};poses={v:c.pose for v,c in cars.items()}
        commands=[];previous_key=None
        for command in proposal.commands:
            car=cars[command.actor];before=poses[car.id]
            after=cp.advance(car,before,command.distance,command.curvature);poses[car.id]=after
            key=(command.actor,command.stage,command.curvature,command.distance>=0)
            if key==previous_key:
                commands[-1]['distance']+=command.distance;commands[-1]['duration']+=command.duration;commands[-1]['target']=asdict(after)
            else:
                commands.append(dict(vehicle=car.id,stage=command.stage,distance=command.distance,curvature=command.curvature,
                    duration=command.duration,offset=car.centre_offset,source=asdict(before),target=asdict(after)))
            previous_key=key
        if not all(close_pose(asdict(poses[v]),result['final_poses'][v]) for v in cars):raise ValueError('execution trace mismatch')
        self._data=dict(initial=initial,commands=commands,sourceSignature=proposal.scene_digest,
            geometry=dict(length=scene.length,width=scene.road_width,separation=scene.policy.separation,obstacleIds=[o.id for o in scene.obstacles],
                bodies={v:dict(length=c.length,width=c.width,rearOverhang=c.rear_overhang) for v,c in cars.items()}),
            certifiedClearance=result['certified_body_clearance'],finalPoses=result['final_poses'])

    def as_dict(self):return deepcopy(self._data)

