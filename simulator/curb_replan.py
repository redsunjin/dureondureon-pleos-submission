"""Replan from an authoritative stopped command-boundary pose, without resetting it.

Aligned approaches use the spacing/path search again. A turned/parked incoming
car uses a revalidated remaining command suffix. This is bounded continuation,
not a general arbitrary-pose or changed-environment route search.
"""
from dataclasses import replace
import math

from simulator import curb_parking as cp


def scene_from_dict(data):
    def car(c):
        return cp.Car(**{**c, 'pose': cp.Pose(**c['pose'])})
    return cp.Scene(**{**data, 'parked': tuple(car(c) for c in data['parked']),
        'incoming': car(data['incoming']), 'policy': cp.Policy(**data['policy']),
        'obstacles': tuple(cp.Obstacle(**{**o, 'pose': cp.Pose(**o['pose'])}) for o in data['obstacles'])})


def plan_from_dict(data):
    return cp.Plan(**{**data, 'target': cp.Pose(**data['target']),
        'entry_target': cp.Pose(**data['entry_target']),
        'shifted': tuple(tuple(p) for p in data['shifted']),
        'final_parked': tuple(tuple(p) for p in data['final_parked']),
        'commands': tuple(cp.Command(**c) for c in data['commands'])})


def _gap(scene, proposal, poses):
    cars = sorted(scene.parked, key=lambda c: c.pose.x)
    g = proposal.gap_index
    left = poses[cars[g-1].id].x+cars[g-1].length/2 if g else scene.policy.boundary
    right = poses[cars[g].id].x-cars[g].length/2 if g < len(cars) else scene.length-scene.policy.boundary
    return right-left


def prepare(scene, source, command_count, checkpoint):
    """Return (new scene, validated new plan, planning mode), or refuse in place.

    Original absolute movement envelopes survive successive replans. All time is
    rebased to a fresh virtual snapshot; human consent is handled separately by
    the session API and must never be inherited from this model's assumptions.
    """
    if source.scene_digest != cp.signature(scene) or cp._configuration_error(scene, True):
        raise ValueError('invalid_source_scene')
    if not isinstance(command_count, int) or isinstance(command_count, bool) or not 0 <= command_count < len(source.commands):
        raise ValueError('no_remaining_commands')
    cars = cp._bodies(scene)
    if set(checkpoint) != set(cars):
        raise ValueError('checkpoint_participants_changed')
    poses = {i: c.pose for i, c in cars.items()}
    for command in source.commands[:command_count]:
        if command.actor not in cars:
            raise ValueError('invalid_source_actor')
        poses[command.actor] = cp.advance(cars[command.actor], poses[command.actor], command.distance, command.curvature)
    current = {i: cp.Pose(**p) for i, p in checkpoint.items()}
    for i, p in current.items():
        if not all(math.isfinite(v) for v in (p.x,p.y,p.yaw)) or math.dist((p.x,p.y,p.yaw),(poses[i].x,poses[i].y,poses[i].yaw)) > 1e-7:
            raise ValueError('checkpoint_does_not_match_commands')
    parked = []
    for car in scene.parked:
        p = current[car.id]
        lower, upper = car.pose.x-car.max_backward, car.pose.x+car.max_forward
        if not lower-cp.EPS <= p.x <= upper+cp.EPS:
            raise ValueError('checkpoint_outside_original_envelope')
        parked.append(replace(car, pose=p, max_backward=max(0,p.x-lower),
                              max_forward=max(0,upper-p.x), observed_at=0))
    updated = replace(scene, name='현재 배치 협력 평행주차', parked=tuple(parked),
        incoming=replace(scene.incoming, pose=current[scene.incoming.id], observed_at=0), observed_at=0)
    minimum, wall = cp.clearance_state(updated, current)
    if minimum < updated.policy.separation-cp.EPS or wall < updated.policy.boundary-cp.EPS:
        raise ValueError('checkpoint_clearance_violation')
    fresh = cp.plan(updated)
    if fresh.plan is not None:
        return updated, fresh.plan, 'current_pose_search'
    # Mid-turn and post-parking poses are outside the fresh two-arc search's
    # initial-heading assumptions. Validate the remainder, not old frame images.
    commands = list(source.commands[command_count:])
    if commands[0].actor==scene.incoming.id and commands[0].stage!='steering_at_rest':
        commands.insert(0,cp.Command(scene.incoming.id,0,0,0.5,'steering_at_rest'))
    opening = {c.id: current[c.id].x for c in updated.parked}
    trial_poses = dict(current)
    for command in commands:
        trial_poses[command.actor] = cp.advance(cars[command.actor],trial_poses[command.actor],command.distance,command.curvature)
        if command.stage=='create_space':
            opening[command.actor] = trial_poses[command.actor].x
    proposal = replace(source, scene_digest=cp.signature(updated), created_at=0,
        expires_at=updated.policy.plan_ttl, original_gap=_gap(updated,source,current),
        shifted=tuple((c.id,opening[c.id]) for c in updated.parked), commands=tuple(commands), route_trials=1)
    result = cp.execute(updated,proposal,continuation=True)
    if result['status']!='parked':
        raise ValueError('remaining_path_not_certified')
    largest = max(_gap(updated,proposal,{i:cp.Pose(**p) for i,p in f['poses'].items()}) for f in result['frames'])
    proposal = replace(proposal,expanded_gap=largest)
    return updated,proposal,'remaining_path_revalidated'
