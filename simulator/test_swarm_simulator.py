import unittest
from dataclasses import replace

from swarm_simulator import (
    MODES, Assignment, Attempt, RunConfig, audit_crossings, audit_reservations,
    compare, demo_scenario, experiment, run, seeded_scenario,
)


class SwarmSimulatorTests(unittest.TestCase):
    def test_demo_scenario_has_ten_vehicles(self):
        vehicles, spaces = demo_scenario()
        self.assertEqual(len(vehicles), 10)
        self.assertGreaterEqual(len(spaces), len(vehicles))

    def test_every_mode_assigns_all_vehicles(self):
        for mode, metrics in compare().items():
            with self.subTest(mode=mode):
                self.assertEqual(metrics["vehicle_count"], 10)
                self.assertEqual(metrics["assignments"], 10)
                self.assertEqual(metrics["unassigned"], 0)

    def test_audit_detects_corrupted_hybrid_plan(self):
        # Mutate the output of the scheduler: the auditor must catch an overlap
        # even when that output claims to be hybrid.
        result = run("hybrid")
        a, b = result.assignments[:2]
        bad = replace(b, conflict_zone=a.conflict_zone,
                      crossing_start=a.crossing_start, crossing_end=a.crossing_end)
        self.assertEqual(audit_crossings((a, bad)), 1)
        self.assertEqual(audit_crossings(result.assignments), 0)

    def test_crossing_audit_uses_half_open_intervals_and_zone(self):
        a = Assignment("v1", "s1", 0, 1, 2, 4, 1, 1, 2, "cross-a")
        adjacent = replace(a, vehicle_id="v2", crossing_start=4, crossing_end=5)
        other_zone = replace(a, vehicle_id="v3", conflict_zone="cross-b")
        nested = replace(a, vehicle_id="v4", crossing_start=3)
        self.assertEqual(audit_crossings((a, adjacent, other_zone)), 0)
        self.assertEqual(audit_crossings((a, nested)), 1)

    def test_reservation_audit_detects_duplicate_acceptance_and_rejection(self):
        attempts = (
            Attempt("a", 1, "s1", "accepted"),
            Attempt("b", 1, "s1", "conflict"),
            Attempt("b", 3, None, "dropped"),
            Attempt("c", 4, "s1", "accepted"),
        )
        self.assertEqual(audit_reservations(attempts), 2)

    def test_mode_differences_are_measured_from_records(self):
        independent = run("independent")
        p2p = run("p2p-only")
        hybrid = run("hybrid")
        self.assertGreater(independent.metrics.reservation_conflicts, 0)
        self.assertGreater(p2p.metrics.crossing_conflicts, 0)
        self.assertEqual(hybrid.metrics.crossing_conflicts, 0)
        self.assertEqual(hybrid.metrics.reservation_conflicts, 0)
        self.assertGreater(hybrid.metrics.total_wait_ticks, 0)
        for result in (independent, p2p, hybrid):
            self.assertEqual(result.metrics.crossing_conflicts, audit_crossings(result.assignments))
            self.assertEqual(result.metrics.reservation_conflicts, audit_reservations(result.attempts))
            self.assertIsNone(result.metrics.deadlocks)

    def test_empty_inputs_do_not_fall_back_to_demo(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(run(mode, vehicles=()).metrics.vehicle_count, 0)
                result = run(mode, spaces=())
                self.assertEqual(result.metrics.assignments, 0)
                self.assertEqual(result.metrics.capacity_failures, 10)

    def test_space_shortage_finishes_without_duplicate_assignment(self):
        vehicles, spaces = demo_scenario()
        vehicles = tuple(replace(v, arrival_time=0) for v in vehicles)
        for mode in MODES:
            with self.subTest(mode=mode):
                result = run(mode, vehicles, spaces[:2])
                self.assertEqual(result.metrics.assignments, 2)
                self.assertEqual(result.metrics.capacity_failures, 8)
                self.assertEqual(result.metrics.duplicate_assignments, 0)
                self.assertEqual(result.metrics.timed_out, 0)

    def test_complete_network_loss_times_out_without_assignment(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                result = run(mode, config=RunConfig(packet_loss_rate=1, max_ticks=10))
                self.assertEqual(result.metrics.assignments, 0)
                self.assertEqual(result.metrics.timed_out, 10)
                self.assertGreater(result.metrics.dropped_messages, 0)
                self.assertEqual(result.metrics.capacity_failures, 0)

    def test_delayed_intents_can_conflict_but_hybrid_stays_atomic(self):
        config = RunConfig(message_delay_ticks=3)
        self.assertGreater(run("p2p-only", config=config).metrics.reservation_conflicts, 0)
        hybrid = run("hybrid", config=config)
        self.assertEqual(hybrid.metrics.reservation_conflicts, 0)
        self.assertEqual(hybrid.metrics.crossing_conflicts, 0)
        self.assertEqual(hybrid.metrics.assignments, 10)

    def test_short_decision_horizon_reports_unassigned(self):
        result = run("hybrid", config=RunConfig(message_delay_ticks=20, max_ticks=5))
        self.assertEqual(result.metrics.assignments, 0)
        self.assertEqual(result.metrics.timed_out, 10)

    def test_seeded_fault_runs_conserve_vehicles_and_unique_spaces(self):
        config = RunConfig(message_delay_ticks=2, packet_loss_rate=0.25)
        for seed in range(30):
            vehicles, spaces = seeded_scenario(seed)
            for mode in MODES:
                with self.subTest(seed=seed, mode=mode):
                    result = run(mode, vehicles, spaces, replace(config, seed=seed))
                    self.assertEqual(result.metrics.assignments + result.metrics.unassigned, 10)
                    self.assertEqual(len({a.vehicle_id for a in result.assignments}), result.metrics.assignments)
                    self.assertEqual(result.metrics.duplicate_assignments, 0)
                    if mode == "hybrid":
                        self.assertEqual(audit_crossings(result.assignments), 0)
                        self.assertEqual(audit_reservations(result.attempts), 0)

    def test_seeded_results_are_reproducible_and_inputs_order_independent(self):
        vehicles, spaces = seeded_scenario(7)
        config = RunConfig(seed=7, packet_loss_rate=0.3, message_delay_ticks=2)
        self.assertEqual(experiment(3, config), experiment(3, config))
        self.assertEqual(run("hybrid", vehicles, spaces, config).as_dict(),
                         run("hybrid", tuple(reversed(vehicles)), tuple(reversed(spaces)), config).as_dict())
        self.assertNotEqual(seeded_scenario(7), seeded_scenario(8))

    def test_invalid_inputs_fail_explicitly(self):
        for kwargs in ({"packet_loss_rate": -0.1}, {"packet_loss_rate": 1.1},
                       {"message_delay_ticks": -1}, {"retry_ticks": 0},
                       {"max_ticks": 0}, {"max_ticks": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunConfig(**kwargs)
        vehicles, spaces = demo_scenario()
        with self.assertRaises(ValueError):
            run("hybrid", vehicles=(vehicles[0], vehicles[0]))
        with self.assertRaises(ValueError):
            run("hybrid", spaces=(spaces[0], spaces[0]))
        with self.assertRaises(ValueError):
            run("invalid")
        with self.assertRaises(ValueError):
            experiment(0, RunConfig())


if __name__ == "__main__":
    unittest.main()
