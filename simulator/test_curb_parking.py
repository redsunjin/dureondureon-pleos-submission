"""Geometry/kinematics, independent replay audit, planner and runtime failure tests."""
from dataclasses import replace
import math
import random
import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import curb_parking as cp


def independent_overlap(a, b):
    # Independent containment/edge-crossing audit, not the planner's SAT function.
    def cross(o, p, q):
        return (p[0]-o[0])*(q[1]-o[1])-(p[1]-o[1])*(q[0]-o[0])
    def inside(p, polygon):
        values = [cross(polygon[i], polygon[(i+1) % 4], p) for i in range(4)]
        return all(v >= 0 for v in values) or all(v <= 0 for v in values)
    if any(inside(p, b) for p in a) or any(inside(p, a) for p in b):
        return True
    for i in range(4):
        p, q = a[i], a[(i+1) % 4]
        for j in range(4):
            r, s = b[j], b[(j+1) % 4]
            if cross(p, q, r)*cross(p, q, s) < 0 and cross(r, s, p)*cross(r, s, q) < 0:
                return True
    return False


class GeometryTests(unittest.TestCase):
    def test_rotated_rectangles_and_edge_crossing(self):
        a = cp.Obstacle('a', cp.Pose(0, 0, math.pi/4), 6, 0.2)
        b = cp.Obstacle('b', cp.Pose(0, 0, -math.pi/4), 6, 0.2)
        self.assertTrue(cp.overlaps(cp.corners(a), cp.corners(b)))
        self.assertTrue(independent_overlap(cp.corners(a), cp.corners(b)))
        self.assertEqual(cp.distance(cp.corners(a), cp.corners(b)), 0)
        c = replace(b, pose=cp.Pose(10, 0))
        self.assertFalse(cp.overlaps(cp.corners(a), cp.corners(c)))

    def test_euclidean_distance_between_known_boxes(self):
        a = cp.Obstacle('a', cp.Pose(0, 0), 2, 2)
        b = cp.Obstacle('b', cp.Pose(5, 6), 2, 2)
        self.assertAlmostEqual(cp.distance(cp.corners(a), cp.corners(b)), 5.0)

    def test_bicycle_reverse_arc_matches_rear_axle_geometry(self):
        car = cp.demo_scene().incoming
        r, angle = car.radius, 0.5
        end = cp.advance(car, cp.Pose(10, 4), -r*angle, -1/r)
        rear_x = end.x-car.centre_offset*math.cos(end.yaw)
        rear_y = end.y-car.centre_offset*math.sin(end.yaw)
        self.assertAlmostEqual(end.yaw, angle)
        self.assertAlmostEqual(rear_x, 10-car.centre_offset-r*math.sin(angle))
        self.assertAlmostEqual(rear_y, 4-r*(1-math.cos(angle)))

    def test_motion_bound_contains_all_corner_paths(self):
        car = cp.demo_scene().incoming
        start = cp.Pose(10, 4, 0.4)
        for ds, curvature in ((0.6, 0), (-0.4, 1/car.radius), (-0.4, -1/car.radius)):
            command = cp.Command(car.id, ds, curvature, 2, 'reverse_in')
            bound = cp.motion_bound(car, command)
            before = cp.corners(car, start)
            for k in range(21):
                after = cp.corners(car, cp.advance(car, start, ds*k/20, curvature))
                self.assertTrue(all(math.dist(a, b) <= bound+1e-9 for a, b in zip(before, after)))

    def test_straight_sweep_allows_motion_away_from_close_neighbor(self):
        scene = cp.demo_scene()
        a = replace(scene.parked[0], pose=cp.Pose(3, 1.5))
        b = replace(scene.parked[1], pose=cp.Pose(8.02, 1.5))
        scene = replace(scene, parked=(a, b))
        poses = {c.id: c.pose for c in (*scene.parked, scene.incoming)}
        command = cp.Command(b.id, 0.03, 0, 0.1, 'settle_spacing')
        self.assertTrue(cp._step_clear(scene, b, poses, command, ()))

    def test_swept_guard_rejects_tunneling_past_thin_obstacle(self):
        car = replace(cp.demo_scene().incoming, pose=cp.Pose(5, 4.5))
        obstacle = cp.Obstacle('thin', cp.Pose(12, 4.5), 0.05, 0.3)
        scene = replace(cp.demo_scene(), parked=(), incoming=car, obstacles=(obstacle,))
        end = cp.advance(car, car.pose, 14, 0)
        self.assertFalse(cp.overlaps(cp.corners(car), cp.corners(obstacle)))
        self.assertFalse(cp.overlaps(cp.corners(car, end), cp.corners(obstacle)))
        command = cp.Command(car.id, 14, 0, 30, 'approach')
        self.assertFalse(cp._step_clear(scene, car, {car.id: car.pose}, command, scene.obstacles))


class CooperativeParkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = cp.demo_scene()
        cls.decision = cp.plan(cls.scene)
        assert cls.decision.plan is not None, cls.decision
        cls.proposal = cls.decision.plan
        cls.result = cp.execute(cls.scene, cls.proposal)

    def test_both_sides_and_outer_chain_create_space(self):
        p = self.proposal
        self.assertAlmostEqual(p.original_gap, 5.4)
        self.assertGreater(p.expanded_gap, p.original_gap)
        shifted = dict(p.shifted)
        self.assertLess(shifted['P-2'], self.scene.parked[1].pose.x)
        self.assertGreater(shifted['P-3'], self.scene.parked[2].pose.x)
        self.assertLess(shifted['P-1'], self.scene.parked[0].pose.x)
        self.assertGreater(shifted['P-4'], self.scene.parked[3].pose.x)
        actors = list(dict.fromkeys(c.actor for c in p.commands))
        self.assertEqual(actors, ['P-1', 'P-2', 'P-4', 'P-3', 'NEW'])

    def test_same_scene_cannot_fit_without_cooperation(self):
        result = cp.simulate(self.scene, cooperative=False)
        self.assertEqual(result['result']['status'], 'blocked')
        self.assertTrue(all(v == 0 for v in result['result']['travel_by_vehicle'].values()))

    def test_final_pose_and_distinct_approach_reverse_stages(self):
        self.assertEqual(self.result['status'], 'parked')
        final = cp.Pose(**self.result['final_poses']['NEW'])
        self.assertAlmostEqual(final.x, self.proposal.target.x)
        self.assertAlmostEqual(final.y, self.scene.parking_y)
        self.assertAlmostEqual(final.yaw, 0)
        stages = {c.stage for c in self.proposal.commands}
        self.assertTrue({'approach', 'reverse_in', 'reverse_align', 'steering_at_rest'} <= stages)
        self.assertGreater(max(f['poses']['NEW']['yaw'] for f in self.result['frames']), 0.5)

    def test_final_spacing_is_restored_after_wide_entry_opening(self):
        poses = self.result['final_poses']
        rear, front, new = poses['P-2'], poses['P-3'], poses['NEW']
        back_gap = new['x']-self.scene.incoming.length/2-(rear['x']+self.scene.parked[1].length/2)
        front_gap = front['x']-self.scene.parked[2].length/2-(new['x']+self.scene.incoming.length/2)
        self.assertAlmostEqual(back_gap, 0.42)
        self.assertAlmostEqual(front_gap, 0.42)
        self.assertTrue(any(c.stage == 'settle_spacing' for c in self.proposal.commands))
        self.assertLess(front['x']-rear['x']-4.6, self.proposal.expanded_gap)

    def test_settling_never_moves_a_nonconsenting_parked_vehicle(self):
        large = replace(self.scene, parked=(replace(self.scene.parked[1], pose=cp.Pose(8, 1.5), consent=False),
                                           replace(self.scene.parked[2], pose=cp.Pose(22, 1.5), consent=False)))
        value = cp.simulate(large)
        self.assertEqual(value['result']['status'], 'parked')
        self.assertEqual(value['result']['travel_by_vehicle']['P-2'], 0)
        self.assertEqual(value['result']['travel_by_vehicle']['P-3'], 0)

    def test_all_controls_obey_steering_speed_and_movement_limits(self):
        for c in self.proposal.commands:
            car = cp._bodies(self.scene)[c.actor]
            self.assertLessEqual(abs(math.atan(car.wheelbase*c.curvature)), car.max_steer+1e-9)
            if c.stage.startswith('reverse'):
                self.assertLess(c.distance, 0)
            if c.stage == 'steering_at_rest':
                self.assertEqual(c.distance, 0)
        for car in self.scene.parked:
            end = self.result['final_poses'][car.id]
            self.assertLessEqual(abs(end['x']-car.pose.x), max(car.max_backward, car.max_forward)+1e-9)
            self.assertAlmostEqual(end['y'], car.pose.y)

    def test_independent_replay_audit_finds_no_intersections_or_out_of_bounds(self):
        cars = cp._bodies(self.scene)
        for frame in self.result['frames']:
            polygons = [cp.corners(cars[i], cp.Pose(**p)) for i, p in frame['poses'].items()]
            for i, a in enumerate(polygons):
                for b in polygons[i+1:]:
                    self.assertFalse(independent_overlap(a, b), frame['time'])
                for x, y in a:
                    self.assertTrue(0 <= x <= self.scene.length and 0 <= y <= self.scene.road_width)
        self.assertGreaterEqual(self.result['certified_body_clearance']+1e-8, self.scene.policy.separation)
        self.assertGreaterEqual(self.result['certified_wall_clearance']+1e-8, self.scene.policy.boundary)

    def test_audit_rejects_deliberately_overlapping_replay(self):
        cars = cp._bodies(self.scene)
        self.assertTrue(independent_overlap(cp.corners(cars['P-1']), cp.corners(cars['P-2'], cars['P-1'].pose)))

    def test_large_existing_gap_needs_no_neighbor_movement(self):
        parked = (replace(self.scene.parked[1], pose=cp.Pose(8, 1.5)), replace(self.scene.parked[2], pose=cp.Pose(22, 1.5)))
        value = cp.simulate(replace(self.scene, parked=parked), cooperative=False)
        self.assertEqual(value['result']['status'], 'parked')
        self.assertTrue(all(value['result']['travel_by_vehicle'][c.id] == 0 for c in parked))

    def test_asymmetric_limits_still_find_a_feasible_opening(self):
        # Balanced anchors alone miss this interval. Rear may move only 0.8m;
        # front/outer chain may move 2.5m, so the gap must shift forward.
        cars = tuple(replace(c, max_backward=0.8, max_forward=2.5) for c in self.scene.parked)
        value = cp.simulate(replace(self.scene, parked=cars))
        self.assertEqual(value['result']['status'], 'parked', value['decision'])
        for c in cars:
            dx = value['result']['final_poses'][c.id]['x']-c.pose.x
            self.assertTrue(-0.8-1e-8 <= dx <= 2.5+1e-8)

    def test_consent_denial_and_insufficient_motion_capacity_do_not_move(self):
        for name in ('consent_denied', 'insufficient_space'):
            scene, mode, faults = cp.scenarios()[name]
            value = cp.simulate(scene, mode, faults)
            self.assertEqual(value['result']['status'], 'blocked')
            self.assertEqual(len(value['result']['frames']), 1)

    def test_narrow_road_fails_trajectory_even_with_enough_longitudinal_space(self):
        scene = cp.scenarios()['narrow_lane'][0]
        self.assertIsNone(cp._configuration_error(scene))
        value = cp.plan(scene)
        self.assertEqual(value.status, 'blocked')
        self.assertIn(value.reason, ('maneuver_not_found', 'search_budget_exhausted'))
        self.assertGreater(value.route_trials, 0)

    def test_stale_missing_communication_and_nonparallel_inputs_are_rejected(self):
        for scene, reason in ((replace(self.scene, observed_at=3), 'stale_state'),
            (replace(self.scene, incoming=replace(self.scene.incoming, connected=False)), 'communication_unavailable'),
            (replace(self.scene, incoming=replace(self.scene.incoming, pose=cp.Pose(5, 4.55, 0.2))), 'unsupported_initial_heading')):
            self.assertEqual(cp.plan(scene).reason, reason)

    def test_obstacle_in_approach_corridor_cannot_be_ignored(self):
        obstacle = cp.Obstacle('blocked-lane', cp.Pose(15, 4.55), 1.0, 2.2)
        decision = cp.plan(replace(self.scene, obstacles=(obstacle,)))
        self.assertEqual(decision.status, 'blocked')

    def test_plan_snapshot_changes_and_expiry_prevent_motion(self):
        changed = replace(self.scene, parked=(replace(self.scene.parked[0], consent=False), *self.scene.parked[1:]))
        for scene, now, reason in ((changed, 0, 'snapshot_changed_or_invalid'), (self.scene, 121, 'plan_expired'), (self.scene, 3, 'stale_state')):
            value = cp.execute(scene, self.proposal, now=now)
            self.assertEqual(value['reason'], reason)
            self.assertTrue(all(v == 0 for v in value['travel_by_vehicle'].values()))

    def test_runtime_loss_and_withdrawal_stop_at_exact_event_without_reset(self):
        for kind in ('communication_lost', 'consent_withdrawn'):
            value = cp.execute(self.scene, self.proposal, (cp.Fault(2.035, kind, 'P-2'),))
            self.assertEqual(value['status'], 'stopped')
            self.assertEqual(value['reason'], kind)
            self.assertAlmostEqual(value['elapsed_seconds'], 2.035)
            self.assertGreater(value['travel_by_vehicle']['P-1'], 0)
            self.assertEqual(value['travel_by_vehicle']['NEW'], 0)
            self.assertNotEqual(value['final_poses']['P-1']['x'], self.scene.parked[0].pose.x)
            self.assertEqual(value['frames'][-1]['poses'], value['frames'][-2]['poses'])

    def test_new_obstacle_stops_without_executing_remaining_motion(self):
        obstacle = cp.Obstacle('person', cp.Pose(14, 3.6), 0.5, 0.5)
        value = cp.execute(self.scene, self.proposal, (cp.Fault(10, 'obstacle', obstacle=obstacle),))
        self.assertEqual(value['status'], 'stopped')
        self.assertAlmostEqual(value['elapsed_seconds'], 10)
        self.assertEqual(value['travel_by_vehicle']['NEW'], 0)
        self.assertEqual(len(value['frames'][0]['obstacles']), 0)
        self.assertEqual(len(value['frames'][-1]['obstacles']), 1)

    def test_external_obstacle_overlap_is_reported_not_hidden_as_safe(self):
        obstacle = cp.Obstacle('external', self.scene.parked[2].pose, 0.5, 0.5)
        value = cp.execute(self.scene, self.proposal, (cp.Fault(0, 'obstacle', obstacle=obstacle),))
        self.assertEqual(value['reason'], 'external_obstacle_clearance_violation')
        self.assertEqual(value['minimum_sampled_body_distance'], 0)

    def test_tampered_control_and_timeout_are_stopped(self):
        bad = replace(self.proposal.commands[0], curvature=100)
        value = cp.execute(self.scene, replace(self.proposal, commands=(bad,)))
        self.assertEqual(value['reason'], 'invalid_control')
        value = cp.execute(self.scene, replace(self.proposal, expires_at=1.95), (cp.Fault(2, 'communication_lost', 'P-2'),))
        self.assertEqual(value['reason'], 'plan_expired')
        self.assertLess(value['elapsed_seconds'], 1.95)

    def test_invalid_dimensions_duplicate_ids_nan_and_search_bounds(self):
        invalid = (replace(self.scene, incoming=replace(self.scene.incoming, length=-1)),
                   replace(self.scene, incoming=replace(self.scene.incoming, id='P-1')),
                   replace(self.scene, incoming=replace(self.scene.incoming, max_backward=float('nan'))),
                   replace(self.scene, policy=replace(self.scene.policy, dt=0)),
                   replace(self.scene, policy=replace(self.scene.policy, opening_step=1e-10)))
        for scene in invalid:
            self.assertEqual(cp.plan(scene).status, 'blocked')
        with self.assertRaises(ValueError):
            cp.Fault(float('nan'), 'communication_lost', 'P-1')

    def test_fixed_seed_variations_are_audited_whether_success_or_blocked(self):
        successes = 0
        for seed in range(8):
            rng = random.Random(seed)
            gap = rng.uniform(4.9, 6.3)
            xs = (3.8, 9, 13.6+gap, 18.8+gap)
            cars = tuple(replace(c, pose=cp.Pose(x, 1.5), max_backward=1.8, max_forward=1.8) for c, x in zip(self.scene.parked, xs))
            incoming = replace(self.scene.incoming, length=rng.uniform(4.4, 5.0), width=rng.uniform(1.7, 1.95))
            scene = replace(self.scene, parked=cars, incoming=incoming, length=32)
            proposal = cp.plan(scene)
            if proposal.plan:
                successes += 1
                value = cp.execute(scene, proposal.plan, include_frames=False)
                self.assertEqual(value['status'], 'parked')
                self.assertGreaterEqual(value['certified_body_clearance']+1e-8, scene.policy.separation)
            else:
                self.assertIn(proposal.reason, ('no_redistribution_capacity', 'maneuver_not_found', 'search_budget_exhausted'))
        self.assertGreater(successes, 0)


class InterfaceTests(unittest.TestCase):
    def test_cli_override_changes_actual_movement_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'result.json'
            command = [sys.executable, str(Path(__file__).with_name('curb_parking.py')), '--max-shift', '0', '--output', str(output)]
            subprocess.run(command, check=True, capture_output=True)
            value = json.loads(output.read_text())['cooperative']
            self.assertEqual(value['result']['status'], 'blocked')
            self.assertTrue(all(c['max_backward'] == 0 for c in value['scene']['parked']))

    def test_cli_rejects_nonfinite_and_negative_parameters(self):
        script = str(Path(__file__).with_name('curb_parking.py'))
        for flag, value in (('--max-shift', '-1'), ('--incoming-length', 'nan'), ('--road-width', '0'), ('--max-shift', 'inf')):
            result = subprocess.run([sys.executable, script, flag, value], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_offline_replay_embeds_data_without_script_injection(self):
        from curb_replay import render
        data = {'name': '</script><script>alert(1)</script>', 'value': 7.89}
        html = render(data)
        embedded = html.split('<script id="simulation-data" type="application/json">')[1].split('</script>')[0]
        self.assertEqual(json.loads(embedded), data)
        self.assertNotIn('</script>', embedded)


if __name__ == '__main__':
    unittest.main()
