"""Cooperative curb parking with bounded 1-D redistribution and a 2-D bicycle model.

Synthetic, deterministic geometry research only. No actuator/Pleos control. A failed
bounded two-arc search is not proof that all possible parking maneuvers are infeasible.
All coordinates/metres are local; Pose is body centre, x along the curb, y into road.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable

EPS = 1e-8


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    yaw: float = 0.0


@dataclass(frozen=True)
class Car:
    id: str
    pose: Pose
    length: float = 4.6
    width: float = 1.8
    wheelbase: float = 2.7
    rear_overhang: float = 0.9
    max_steer: float = 0.55
    max_backward: float = 1.6
    max_forward: float = 1.6
    consent: bool = True
    connected: bool = True
    observed_at: float = 0.0

    @property
    def centre_offset(self) -> float:
        return self.length / 2 - self.rear_overhang

    @property
    def radius(self) -> float:
        return self.wheelbase / math.tan(self.max_steer)


@dataclass(frozen=True)
class Obstacle:
    id: str
    pose: Pose
    length: float
    width: float


@dataclass(frozen=True)
class Policy:
    clearance: float = 0.30
    position_uncertainty: float = 0.05
    wall_clearance: float = 0.15
    dt: float = 0.1
    shift_speed: float = 0.30
    approach_speed: float = 0.60
    reverse_speed: float = 0.35
    max_state_age: float = 2.0
    plan_ttl: float = 120.0
    opening_step: float = 0.25
    search_extra: float = 4.0
    max_route_trials: int = 240

    @property
    def separation(self) -> float:
        return self.clearance + 2 * self.position_uncertainty

    @property
    def boundary(self) -> float:
        return self.wall_clearance + self.position_uncertainty


@dataclass(frozen=True)
class Scene:
    name: str
    parked: tuple[Car, ...]
    incoming: Car
    length: float = 30.0
    road_width: float = 7.5
    parking_y: float = 1.5
    obstacles: tuple[Obstacle, ...] = ()
    observed_at: float = 0.0
    policy: Policy = Policy()


@dataclass(frozen=True)
class Command:
    actor: str
    distance: float  # signed rear-axle arc length, not a Cartesian interpolation
    curvature: float
    duration: float
    stage: str


@dataclass(frozen=True)
class Plan:
    scene_digest: str
    created_at: float
    expires_at: float
    gap_index: int
    original_gap: float
    expanded_gap: float
    target: Pose
    entry_target: Pose
    final_parked: tuple[tuple[str, float], ...]
    shifted: tuple[tuple[str, float], ...]
    commands: tuple[Command, ...]
    route_trials: int


@dataclass(frozen=True)
class Decision:
    status: str
    reason: str
    plan: Plan | None = None
    route_trials: int = 0


@dataclass(frozen=True)
class Fault:
    at: float
    kind: str  # communication_lost / consent_withdrawn / obstacle
    vehicle_id: str | None = None
    obstacle: Obstacle | None = None

    def __post_init__(self):
        if not math.isfinite(self.at) or self.at < 0 or self.kind not in ('communication_lost', 'consent_withdrawn', 'obstacle'):
            raise ValueError('invalid fault')
        if self.kind != 'obstacle' and not self.vehicle_id:
            raise ValueError('fault requires participant')
        if self.kind == 'obstacle':
            o = self.obstacle
            if o is None or not all(math.isfinite(v) for v in (o.pose.x, o.pose.y, o.pose.yaw, o.length, o.width)) or min(o.length, o.width) <= 0:
                raise ValueError('invalid obstacle')


def corners(body: Car | Obstacle, pose: Pose | None = None) -> tuple[tuple[float, float], ...]:
    p = pose or body.pose
    c, s = math.cos(p.yaw), math.sin(p.yaw)
    return tuple((p.x + x*c - y*s, p.y + x*s + y*c)
                 for x, y in ((-body.length/2, -body.width/2), (body.length/2, -body.width/2),
                              (body.length/2, body.width/2), (-body.length/2, body.width/2)))


def overlaps(a: tuple, b: tuple) -> bool:
    """Separating-axis test for convex rectangles; touching counts as intersection."""
    for polygon in (a, b):
        for i in (0, 1):
            p, q = polygon[i], polygon[i+1]
            axis = (-(q[1]-p[1]), q[0]-p[0])
            aa = [x*axis[0]+y*axis[1] for x, y in a]
            bb = [x*axis[0]+y*axis[1] for x, y in b]
            if max(aa) < min(bb)-EPS or max(bb) < min(aa)-EPS:
                return False
    return True


def _point_segment(p: tuple, a: tuple, b: tuple) -> float:
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = max(0.0, min(1.0, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy)))
    return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)


def distance(a: tuple, b: tuple) -> float:
    if overlaps(a, b):
        return 0.0
    return min(_point_segment(p, poly[i], poly[(i+1) % 4])
               for points, poly in ((a, b), (b, a)) for p in points for i in range(4))


def advance(car: Car, pose: Pose, ds: float, curvature: float) -> Pose:
    """Exact constant-curvature rear-axle integration, then back to body centre."""
    offset = car.centre_offset
    rx, ry = pose.x-offset*math.cos(pose.yaw), pose.y-offset*math.sin(pose.yaw)
    yaw = pose.yaw + ds*curvature
    if abs(curvature) < EPS:
        rx += ds*math.cos(pose.yaw)
        ry += ds*math.sin(pose.yaw)
    else:
        rx += (math.sin(yaw)-math.sin(pose.yaw))/curvature
        ry += (math.cos(pose.yaw)-math.cos(yaw))/curvature
    return Pose(rx+offset*math.cos(yaw), ry+offset*math.sin(yaw), yaw)


def motion_bound(car: Car, command: Command) -> float:
    # Every body point moves by at most rear-axle travel + radius * yaw change.
    r = math.hypot(max(car.rear_overhang, car.length-car.rear_overhang), car.width/2)
    return abs(command.distance) * (1 + r*abs(command.curvature))


def signature(scene: Scene) -> str:
    return hashlib.sha256(json.dumps(asdict(scene), sort_keys=True, allow_nan=False).encode()).hexdigest()


def _bodies(scene: Scene) -> dict[str, Car]:
    return {c.id: c for c in (*scene.parked, scene.incoming)}


def _configuration_error(scene: Scene, continuation: bool = False) -> str | None:
    p = scene.policy
    positive = (scene.length, scene.road_width, p.dt, p.shift_speed, p.approach_speed,
                p.reverse_speed, p.max_state_age, p.plan_ttl, p.opening_step)
    if not all(math.isfinite(x) and x > 0 for x in positive):
        return 'invalid_policy'
    if not all(math.isfinite(x) and x >= 0 for x in (p.clearance, p.position_uncertainty, p.wall_clearance, p.search_extra)):
        return 'invalid_policy'
    if not isinstance(p.max_route_trials, int) or isinstance(p.max_route_trials, bool) or p.max_route_trials < 1:
        return 'invalid_policy'
    if p.search_extra/p.opening_step > 256 or len(scene.parked) > 64:
        return 'search_configuration_too_large'
    if not math.isfinite(scene.observed_at) or not math.isfinite(scene.parking_y):
        return 'invalid_scene'
    ids = [c.id for c in (*scene.parked, scene.incoming, *scene.obstacles)]
    if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
        return 'invalid_identifiers'
    for body in (*scene.parked, scene.incoming, *scene.obstacles):
        if not all(math.isfinite(x) for x in (body.pose.x, body.pose.y, body.pose.yaw, body.length, body.width)) or min(body.length, body.width) <= 0:
            return 'invalid_geometry'
    for car in (*scene.parked, scene.incoming):
        if not all(math.isfinite(x) for x in (car.wheelbase, car.rear_overhang, car.max_steer,
                                              car.max_backward, car.max_forward, car.observed_at)):
            return 'invalid_vehicle'
        if not (0 < car.wheelbase < car.length and 0 <= car.rear_overhang < car.length-car.wheelbase and
                0 < car.max_steer < math.pi/2 and car.max_backward >= 0 and car.max_forward >= 0):
            return 'invalid_vehicle'
        if not car.connected:
            return 'communication_unavailable'
        if not 0 <= scene.observed_at-car.observed_at <= p.max_state_age:
            return 'stale_state'
        if abs(car.pose.yaw) > EPS and not (continuation and car.id==scene.incoming.id):
            return 'unsupported_initial_heading'
    if any(abs(c.pose.y-scene.parking_y) > EPS for c in scene.parked):
        return 'unsupported_parked_alignment'
    if not continuation and scene.incoming.pose.y <= scene.parking_y+EPS:
        return 'unsupported_approach'
    return None


def clearance_state(scene: Scene, poses: dict[str, Pose], obstacles: tuple[Obstacle, ...] | None = None) -> tuple[float, float]:
    bodies = _bodies(scene)
    polygons = {i: corners(c, poses[i]) for i, c in bodies.items()}
    values = list(polygons.values())
    fixed = [corners(o) for o in (scene.obstacles if obstacles is None else obstacles)]
    pair_distances = [distance(a, b) for i, a in enumerate(values) for b in values[i+1:]]
    pair_distances += [distance(a, b) for a in values for b in fixed]
    wall = min(min(x, scene.length-x, y, scene.road_width-y) for poly in polygons.values() for x, y in poly)
    return min(pair_distances, default=math.inf), wall


def step_certificate(scene: Scene, car: Car, poses: dict[str, Pose], command: Command,
                     obstacles: tuple[Obstacle, ...]) -> tuple[float, float]:
    """Conservative interval certificate, not just endpoint/sample collision checking.

    Straight translation uses its exact swept rectangle. For curved motion,
    distances are 1-Lipschitz under maximum body-point displacement. Checking
    start separation >= clearance + bound protects the complete command interval.
    Only one car moves at a time. This can reject valid paths conservatively.
    """
    start = poses[car.id]
    if abs(command.curvature) < EPS:
        # Straight translation sweeps an exact longer rectangle. Unlike an
        # isotropic bound, this also certifies motion away from a close neighbour.
        end = advance(car, start, command.distance, 0)
        swept = Obstacle('_sweep', Pose((start.x+end.x)/2, (start.y+end.y)/2, start.yaw),
                         car.length+abs(command.distance), car.width)
        poly, bound = corners(swept), 0.0
    else:
        poly, bound = corners(car, start), motion_bound(car, command)
    wall = min(min(x, scene.length-x, y, scene.road_width-y) for x, y in poly)-bound
    other = [corners(c, poses[c.id]) for c in _bodies(scene).values() if c.id != car.id]
    other += [corners(o) for o in obstacles]
    return min((distance(poly, q)-bound for q in other), default=math.inf), wall


def _step_clear(scene: Scene, car: Car, poses: dict[str, Pose], command: Command,
                obstacles: tuple[Obstacle, ...]) -> bool:
    separation, wall = step_certificate(scene, car, poses, command, obstacles)
    return separation >= scene.policy.separation-EPS and wall >= scene.policy.boundary-EPS


def _commands(car: Car, travel: float, curvature: float, speed: float, stage: str, p: Policy) -> list[Command]:
    if abs(travel) < EPS:
        return []
    count = max(1, math.ceil(abs(travel)/(speed*p.dt)))
    ds = travel/count
    return [Command(car.id, ds, curvature, abs(ds)/speed, stage) for _ in range(count)]


def _redistribute(scene: Scene, cars: tuple[Car, ...], gap: int, required: float, opening: float, cooperative: bool) -> dict[str, float] | None:
    p = scene.policy
    result = {c.id: c.pose.x for c in cars}
    edge = opening
    for car in reversed(cars[:gap]):
        x = min(car.pose.x, edge-car.length/2)
        limit = car.max_backward if cooperative and car.consent else 0.0
        if x < max(car.length/2+p.boundary, car.pose.x-limit)-EPS:
            return None
        result[car.id] = x
        edge = x-car.length/2-p.separation-0.02
    edge = opening+required
    for car in cars[gap:]:
        x = max(car.pose.x, edge+car.length/2)
        limit = car.max_forward if cooperative and car.consent else 0.0
        if x > min(scene.length-car.length/2-p.boundary, car.pose.x+limit)+EPS:
            return None
        result[car.id] = x
        edge = x+car.length/2+p.separation+0.02
    return result


def _openings(scene: Scene, cars: tuple[Car, ...], gap: int, required: float, cooperative: bool) -> list[float]:
    left = cars[gap-1].pose.x+cars[gap-1].length/2 if gap else scene.policy.boundary
    right = cars[gap].pose.x-cars[gap].length/2 if gap < len(cars) else scene.length-scene.policy.boundary
    # Reachable envelopes include the full chain and asymmetric movement limits.
    p = scene.policy
    low = p.boundary
    edge = p.boundary
    for car in cars[:gap]:
        limit = car.max_backward if cooperative and car.consent else 0.0
        x = max(car.pose.x-limit, edge+car.length/2)
        low = x+car.length/2
        edge = low+p.separation+0.02
    high = scene.length-p.boundary-required
    edge = scene.length-p.boundary
    for car in reversed(cars[gap:]):
        limit = car.max_forward if cooperative and car.consent else 0.0
        x = min(car.pose.x+limit, edge-car.length/2)
        high = x-car.length/2-required
        edge = x-car.length/2-p.separation-0.02
    if low > high+EPS:
        return []
    # Bounded anchors, not a claim of globally optimal redistribution.
    return sorted({max(low, min(high, b)) for b in ((left+right-required)/2, left, right-required)})


def _maneuver(scene: Scene, target: Pose) -> tuple[list[Command], list[Command]] | None:
    car, p = scene.incoming, scene.policy
    dy = car.pose.y-target.y
    r = car.radius
    if not 0 < dy < 2*r:
        return None
    alpha = math.acos(1-dy/(2*r))
    staging_x = target.x+2*r*math.sin(alpha)
    approach = _commands(car, staging_x-car.pose.x, 0, p.approach_speed, 'approach', p)
    turn1 = _commands(car, -r*alpha, -1/r, p.reverse_speed, 'reverse_in', p)
    turn2 = _commands(car, -r*alpha, 1/r, p.reverse_speed, 'reverse_align', p)
    # Steering changes occur at rest; no instantaneous moving change of steering.
    steering = lambda: Command(car.id, 0, 0, 0.5, 'steering_at_rest')
    full = approach+[steering()]+turn1+[steering()]+turn2+[steering()]
    return full, turn1+turn2


def plan(scene: Scene, cooperative: bool = True) -> Decision:
    error = _configuration_error(scene)
    if error:
        return Decision('blocked', error)
    initial = {c.id: c.pose for c in _bodies(scene).values()}
    minimum, wall = clearance_state(scene, initial)
    if minimum < scene.policy.separation-EPS or wall < scene.policy.boundary-EPS:
        return Decision('blocked', 'initial_clearance_violation')
    if not scene.incoming.consent:
        return Decision('blocked', 'incoming_consent_required')
    cars = tuple(sorted(scene.parked, key=lambda c: c.pose.x))
    p = scene.policy
    required_min = scene.incoming.length+2*(p.separation+0.02)
    candidates = {}
    for k in range(math.ceil(p.search_extra/p.opening_step)+1):
        required = required_min+k*p.opening_step
        for gap in range(len(cars)+1):
            for opening in _openings(scene, cars, gap, required, cooperative):
                if opening+required > scene.length-p.boundary+EPS:
                    continue
                shifted = _redistribute(scene, cars, gap, required, opening, cooperative)
                if shifted is None:
                    continue
                left = shifted[cars[gap-1].id]+cars[gap-1].length/2 if gap else p.boundary
                right = shifted[cars[gap].id]-cars[gap].length/2 if gap < len(cars) else scene.length-p.boundary
                spare = right-left-required_min
                if spare < -EPS:
                    continue
                for bias in (0.0, -0.25, 0.25, -0.45, 0.45):
                    target_x = (left+right)/2 + bias*max(0.0, spare)
                    key = (tuple(round(shifted[c.id], 7) for c in cars), round(target_x, 7))
                    movement = [abs(shifted[c.id]-c.pose.x) for c in cars]
                    candidates[key] = (sum(movement), max(movement, default=0), gap, left, right, shifted, target_x)
    trials = 0
    for _, _, gap, left, right, shifted, target_x in sorted(candidates.values(), key=lambda c: (round(c[0], 7), round(c[1], 7), c[2], c[6])):
        if trials >= p.max_route_trials:
            return Decision('blocked', 'search_budget_exhausted', route_trials=trials)
        trials += 1
        target = Pose(target_x, scene.parking_y)
        maneuver = _maneuver(scene, target)
        if maneuver is None:
            continue
        full, arcs = maneuver
        final_poses = {c.id: replace(c.pose, x=shifted[c.id]) for c in cars}
        final_poses[scene.incoming.id] = scene.incoming.pose
        # Cheaply reject a colliding turn before checking the entire approach.
        staging = Pose(target.x+2*scene.incoming.radius*math.sin(math.acos(1-(scene.incoming.pose.y-target.y)/(2*scene.incoming.radius))), scene.incoming.pose.y)
        def clear(commands: Iterable[Command], start: Pose) -> bool:
            poses = dict(final_poses)
            poses[scene.incoming.id] = start
            for command in commands:
                if not _step_clear(scene, scene.incoming, poses, command, scene.obstacles):
                    return False
                poses[scene.incoming.id] = advance(scene.incoming, poses[scene.incoming.id], command.distance, command.curvature)
            return True
        if not clear(arcs, staging) or not clear(full, scene.incoming.pose):
            continue
        commands = []
        # Move the outside cars first, then propagate room toward the chosen gap.
        for car in (*cars[:gap], *reversed(cars[gap:])):
            commands += _commands(car, shifted[car.id]-car.pose.x, 0, p.shift_speed, 'create_space', p)
        commands += full
        # The reverse arcs need rear-biased placement. Once aligned, move straight
        # to the centre of the opening, then return excess manoeuvring room.
        final_target = Pose((left+right)/2, scene.parking_y)
        commands += _commands(scene.incoming, final_target.x-target.x, 0, p.reverse_speed, 'final_position', p)
        compacted = dict(shifted)
        if cooperative:
            edge = final_target.x-scene.incoming.length/2-p.separation-0.02
            for car in reversed(cars[:gap]):
                limit = car.max_forward if car.consent else 0.0
                x = max(shifted[car.id], min(edge-car.length/2, car.pose.x+limit))
                compacted[car.id] = x
                commands += _commands(car, x-shifted[car.id], 0, p.shift_speed, 'settle_spacing', p)
                edge = x-car.length/2-p.separation-0.02
            edge = final_target.x+scene.incoming.length/2+p.separation+0.02
            for car in cars[gap:]:
                limit = car.max_backward if car.consent else 0.0
                x = min(shifted[car.id], max(edge+car.length/2, car.pose.x-limit))
                compacted[car.id] = x
                commands += _commands(car, x-shifted[car.id], 0, p.shift_speed, 'settle_spacing', p)
                edge = x+car.length/2+p.separation+0.02
        original_left = cars[gap-1].pose.x+cars[gap-1].length/2 if gap else p.boundary
        original_right = cars[gap].pose.x-cars[gap].length/2 if gap < len(cars) else scene.length-p.boundary
        proposal = Plan(signature(scene), scene.observed_at, scene.observed_at+p.plan_ttl, gap,
                        original_right-original_left, right-left, final_target, target,
                        tuple((c.id, compacted[c.id]) for c in cars),
                        tuple((c.id, shifted[c.id]) for c in cars), tuple(commands), trials)
        # Revalidate the complete multi-car schedule independently of the 1-D fit.
        outcome = execute(scene, proposal, include_frames=False)
        if outcome['status'] == 'parked':
            return Decision('ready', 'cooperative_plan' if cooperative else 'existing_gap', proposal, trials)
    return Decision('blocked', 'no_redistribution_capacity' if not candidates else 'maneuver_not_found', route_trials=trials)


def execute(scene: Scene, proposal: Plan, faults: tuple[Fault, ...] = (), *, now: float | None = None,
            include_frames: bool = True, continuation: bool = False) -> dict:
    now = scene.observed_at if now is None else now
    cars = _bodies(scene)
    poses = {i: c.pose for i, c in cars.items()}
    obstacles = scene.obstacles
    frames, events = [], []
    elapsed = 0.0
    minimum, wall_min = clearance_state(scene, poses)
    moved = {i: 0.0 for i in cars}
    certified, certified_wall = minimum, wall_min
    status, reason = 'parked', 'completed'
    if any(f.vehicle_id is not None and f.vehicle_id not in cars for f in faults):
        raise ValueError('unknown fault participant')
    faults = tuple(sorted(faults, key=lambda f: f.at))
    def frame(stage: str, actor: str | None = None):
        if include_frames:
            frames.append({'time': round(elapsed, 5), 'stage': stage, 'actor': actor,
                           'poses': {i: asdict(p) for i, p in poses.items()},
                           'obstacles': [asdict(o) for o in obstacles]})
    frame('waiting')
    participants = {c.actor for c in proposal.commands if abs(c.distance) > EPS}
    if _configuration_error(scene, continuation) or proposal.scene_digest != signature(scene):
        status, reason = 'blocked', 'snapshot_changed_or_invalid'
    elif not math.isfinite(now) or now < proposal.created_at or now >= proposal.expires_at:
        status, reason = 'blocked', 'plan_expired'
    elif any(not 0 <= now-c.observed_at <= scene.policy.max_state_age for c in cars.values()):
        status, reason = 'blocked', 'stale_state'
    elif any(not cars[i].consent or not cars[i].connected for i in participants):
        status, reason = 'blocked', 'acknowledgement_missing'
    def apply(command: Command) -> str | None:
        nonlocal elapsed, minimum, wall_min, certified, certified_wall
        car = cars.get(command.actor)
        if car is None or not all(math.isfinite(v) for v in (command.distance, command.curvature, command.duration)) or command.duration <= 0:
            return 'invalid_control'
        speeds = {'create_space': scene.policy.shift_speed, 'settle_spacing': scene.policy.shift_speed,
                  'final_position': scene.policy.reverse_speed, 'approach': scene.policy.approach_speed,
                  'reverse_in': scene.policy.reverse_speed, 'reverse_align': scene.policy.reverse_speed,
                  'steering_at_rest': 0.0, 'replan_move': scene.policy.reverse_speed}
        if command.stage not in speeds or abs(command.distance)/command.duration > speeds[command.stage]+EPS:
            return 'invalid_control'
        if abs(command.curvature) > 1/car.radius+EPS:
            return 'invalid_control'
        if car.id != scene.incoming.id and (command.stage not in ('create_space', 'settle_spacing') or abs(command.curvature) > EPS):
            return 'invalid_control'
        if command.stage.startswith('reverse') and command.distance > EPS:
            return 'invalid_control'
        separation, wall = step_certificate(scene, car, poses, command, obstacles)
        if separation < scene.policy.separation-EPS or wall < scene.policy.boundary-EPS:
            return 'clearance_guard'
        candidate = advance(car, poses[car.id], command.distance, command.curvature)
        if car.id != scene.incoming.id and not (car.pose.x-car.max_backward-EPS <= candidate.x <= car.pose.x+car.max_forward+EPS):
            return 'movement_limit'
        certified, certified_wall = min(certified, separation), min(certified_wall, wall)
        poses[car.id] = candidate
        elapsed += command.duration
        moved[car.id] += abs(command.distance)
        minimum_now, wall_now = clearance_state(scene, poses, obstacles)
        minimum, wall_min = min(minimum, minimum_now), min(wall_min, wall_now)
        frame(command.stage, car.id)
        return None
    if status == 'parked':
        for command in proposal.commands:
            if now+elapsed+command.duration >= proposal.expires_at:
                status, reason = 'stopped', 'plan_expired';break
            if faults and faults[0].at <= elapsed+command.duration+EPS:
                fault, faults = faults[0], faults[1:]
                duration = max(0.0, fault.at-elapsed)
                if duration > EPS:
                    partial = replace(command, distance=command.distance*duration/command.duration, duration=duration)
                    error = apply(partial)
                    if error:
                        status, reason = 'stopped', error;break
                events.append({'time': elapsed, 'event': fault.kind, 'vehicle_id': fault.vehicle_id})
                if fault.kind == 'obstacle' and fault.obstacle:
                    obstacles += (fault.obstacle,)
                    value, wall = clearance_state(scene, poses, obstacles)
                    minimum, wall_min = min(minimum, value), min(wall_min, wall)
                    certified = min(certified, value)
                status, reason = 'stopped', fault.kind
                if minimum < scene.policy.separation-EPS:
                    reason = 'external_obstacle_clearance_violation'
                break
            error = apply(command)
            if error:
                status, reason = 'stopped', error;break
        if status == 'parked':
            end = poses[scene.incoming.id]
            if math.hypot(end.x-proposal.target.x, end.y-proposal.target.y) > 1e-6 or abs(end.yaw) > 1e-6 or any(abs(poses[i].x-x) > 1e-6 for i, x in proposal.final_parked):
                status, reason = 'stopped', 'target_not_reached'
    frame(status)
    return {'status': status, 'reason': reason, 'elapsed_seconds': elapsed,
            'minimum_sampled_body_distance': minimum, 'minimum_sampled_wall_distance': wall_min,
            'certified_body_clearance': certified, 'certified_wall_clearance': certified_wall,
            'travel_by_vehicle': moved, 'final_poses': {i: asdict(p) for i, p in poses.items()},
            'events': events, 'frames': frames, 'obstacles': [asdict(o) for o in obstacles]}


def simulate(scene: Scene, cooperative: bool = True, faults: tuple[Fault, ...] = ()) -> dict:
    decision = plan(scene, cooperative)
    result = execute(scene, decision.plan, faults) if decision.plan else {
        'status': 'blocked', 'reason': decision.reason, 'elapsed_seconds': 0,
        'travel_by_vehicle': {i: 0.0 for i in _bodies(scene)},
        'frames': [{'time': 0, 'stage': 'blocked', 'actor': None, 'poses': {i: asdict(c.pose) for i, c in _bodies(scene).items()}}],
        'events': [], 'obstacles': [asdict(o) for o in scene.obstacles]}
    plan_data = asdict(decision.plan) if decision.plan else None
    if plan_data:
        del plan_data['commands']
    return {'scene': asdict(scene), 'cooperative': cooperative, 'decision': decision.reason,
            'route_trials': decision.route_trials, 'plan': plan_data, 'result': result}


def demo_scene() -> Scene:
    parked = tuple(Car(f'P-{i+1}', Pose(x, 1.5)) for i, x in enumerate((3.8, 9.0, 19.0, 24.2)))
    incoming = Car('NEW', Pose(5.0, 4.55), length=4.8, width=1.85, wheelbase=2.8, rear_overhang=1.0)
    return Scene('앞뒤 차량 협력 평행주차', parked, incoming)


def scenarios() -> dict[str, tuple[Scene, bool, tuple[Fault, ...]]]:
    scene = demo_scene()
    locked = replace(scene, name='앞뒤 차량 협조 거부', parked=tuple(replace(c, consent=False) for c in scene.parked))
    insufficient = replace(scene, name='이동 여유 부족', parked=tuple(replace(c, max_backward=0.1, max_forward=0.1) for c in scene.parked))
    narrow = replace(scene, name='회전 통로 부족', road_width=6.0)
    stale = replace(scene, name='오래된 위치', observed_at=3.0)
    return {'cooperative': (scene, True, ()), 'independent': (scene, False, ()),
            'consent_denied': (locked, True, ()), 'insufficient_space': (insufficient, True, ()),
            'narrow_lane': (narrow, True, ()), 'stale_state': (stale, True, ()),
            'communication_loss': (scene, True, (Fault(2.0, 'communication_lost', 'P-2'),)),
            'consent_withdrawn': (scene, True, (Fault(2.0, 'consent_withdrawn', 'P-2'),)),
            'new_obstacle': (scene, True, (Fault(2.0, 'obstacle', obstacle=Obstacle('PED', Pose(14.0, 3.6), 0.5, 0.5)),))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=tuple(scenarios()), default='cooperative')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--max-shift', type=float, help='override parked-car forward/backward limits in metres')
    parser.add_argument('--incoming-length', type=float, help='override incoming body length in metres')
    parser.add_argument('--road-width', type=float, help='override available road width in metres')
    args = parser.parse_args()
    for name in ('max_shift', 'incoming_length', 'road_width'):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value < 0 or (name != 'max_shift' and value == 0)):
            parser.error(f'{name} must be a finite {"nonnegative" if name == "max_shift" else "positive"} number')
    entries = scenarios() if args.all else {args.scenario: scenarios()[args.scenario]}
    for name, (scene, cooperative, faults) in tuple(entries.items()):
        if args.max_shift is not None:
            scene = replace(scene, parked=tuple(replace(c, max_backward=args.max_shift, max_forward=args.max_shift) for c in scene.parked))
        if args.incoming_length is not None:
            scene = replace(scene, incoming=replace(scene.incoming, length=args.incoming_length))
        if args.road_width is not None:
            scene = replace(scene, road_width=args.road_width)
        entries[name] = (scene, cooperative, faults)
    result = {name: simulate(*entry) for name, entry in entries.items()}
    raw = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw)
    for name, value in result.items():
        r, p = value['result'], value['plan']
        print(json.dumps({'scenario': name, 'status': r['status'], 'reason': r['reason'],
                          'original_gap': p['original_gap'] if p else None,
                          'expanded_gap': p['expanded_gap'] if p else None,
                          'travel': r['travel_by_vehicle'], 'route_trials': value['route_trials']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
