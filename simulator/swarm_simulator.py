"""Discrete-tick parking model; no vehicle control or physical collision claims.

A conflict zone is at the entrance to a route. Parking travel follows its one-tick
crossing. Spaces stay occupied for the whole run. The ground truth rejects a
second claim (representing discovery at the space); rejected vehicles retry.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from itertools import combinations
import json
import math
import random
from typing import Literal

Mode = Literal["independent", "p2p-only", "hybrid"]
MODES: tuple[Mode, ...] = ("independent", "p2p-only", "hybrid")


@dataclass(frozen=True)
class ParkingSpace:
    space_id: str
    zone_id: str
    distance_from_gate_a: int
    distance_from_gate_b: int
    conflict_zone: str
    exit_direction: str

    def distance_from(self, gate: str) -> int:
        return self.distance_from_gate_a if gate == "A" else self.distance_from_gate_b


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    arrival_time: int
    gate: str
    preferred_zone: str
    exit_direction: str


@dataclass(frozen=True)
class Assignment:
    vehicle_id: str
    space_id: str
    arrival_time: int
    assigned_time: int
    crossing_start: int
    crossing_end: int
    exploration_ticks: int
    wait_ticks: int
    distance_ticks: int
    conflict_zone: str


@dataclass(frozen=True)
class Attempt:
    vehicle_id: str
    tick: int
    space_id: str | None
    outcome: Literal["accepted", "conflict", "dropped", "no-space"]


@dataclass(frozen=True)
class RunConfig:
    seed: int = 0
    message_delay_ticks: int = 0
    packet_loss_rate: float = 0.0
    max_ticks: int = 100
    retry_ticks: int = 2

    def __post_init__(self) -> None:
        for name in ("seed", "message_delay_ticks", "max_ticks", "retry_ticks"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
        if self.message_delay_ticks < 0 or self.max_ticks < 1 or self.retry_ticks < 1:
            raise ValueError("delay must be nonnegative; horizon and retry must be positive")
        if not 0 <= self.packet_loss_rate <= 1:
            raise ValueError("packet_loss_rate must be between 0 and 1")


@dataclass(frozen=True)
class RunMetrics:
    mode: str
    vehicle_count: int
    assignments: int
    unassigned: int
    capacity_failures: int
    timed_out: int
    reservation_conflicts: int
    duplicate_assignments: int
    crossing_conflicts: int
    deadlocks: int | None
    dropped_messages: int
    average_exploration_ticks: float
    p95_exploration_ticks: int
    average_completion_ticks: float
    total_distance_ticks: int
    total_wait_ticks: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SimulationResult:
    metrics: RunMetrics
    assignments: tuple[Assignment, ...]
    attempts: tuple[Attempt, ...]
    unassigned: dict[str, str]
    config: RunConfig

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def demo_scenario() -> tuple[tuple[Vehicle, ...], tuple[ParkingSpace, ...]]:
    """Return a fixed 10-vehicle scenario with shared entry conflict zones."""

    spaces = (
        ParkingSpace("space-a1", "zone-a", 4, 10, "cross-a", "north"),
        ParkingSpace("space-a2", "zone-a", 5, 9, "cross-a", "north"),
        ParkingSpace("space-a3", "zone-a", 6, 8, "cross-a", "south"),
        ParkingSpace("space-a4", "zone-a", 7, 7, "cross-a", "south"),
        ParkingSpace("space-b1", "zone-b", 10, 4, "cross-b", "north"),
        ParkingSpace("space-b2", "zone-b", 9, 5, "cross-b", "north"),
        ParkingSpace("space-b3", "zone-b", 8, 6, "cross-b", "south"),
        ParkingSpace("space-b4", "zone-b", 7, 7, "cross-b", "south"),
        ParkingSpace("space-c1", "zone-c", 12, 12, "cross-c", "north"),
        ParkingSpace("space-c2", "zone-c", 13, 11, "cross-c", "south"),
        ParkingSpace("space-c3", "zone-c", 14, 10, "cross-c", "north"),
        ParkingSpace("space-c4", "zone-c", 15, 9, "cross-c", "south"),
    )
    vehicles = tuple(
        Vehicle(
            vehicle_id=f"v-{index:02d}",
            arrival_time=(index - 1) // 2,
            # Pair arrivals at the same gate so stale independent snapshots can
            # reproduce duplicate target selection.
            gate="A" if index % 4 in (1, 2) else "B",
            preferred_zone="zone-a" if index % 4 in (1, 2) else "zone-b",
            exit_direction="north" if index % 3 else "south",
        )
        for index in range(1, 11)
    )
    return vehicles, spaces


def _space_sort_key(vehicle: Vehicle, space: ParkingSpace) -> tuple[int, int, int, str]:
    preferred = 0 if space.zone_id == vehicle.preferred_zone else 1
    exit_penalty = 0 if space.exit_direction == vehicle.exit_direction else 1
    return (
        preferred,
        space.distance_from(vehicle.gate),
        exit_penalty,
        space.space_id,
    )


def seeded_scenario(seed: int) -> tuple[tuple[Vehicle, ...], tuple[ParkingSpace, ...]]:
    rng = random.Random(seed)
    _, spaces = demo_scenario()
    vehicles = tuple(
        Vehicle(f"v-{i:02d}", rng.randrange(6), rng.choice(("A", "B")),
                rng.choice(("zone-a", "zone-b", "zone-c")),
                rng.choice(("north", "south")))
        for i in range(1, 11)
    )
    return vehicles, spaces


def audit_crossings(assignments: tuple[Assignment, ...] | list[Assignment]) -> int:
    """Count unordered pairs of overlapping [start, end) plans in one zone."""
    return sum(
        a.conflict_zone == b.conflict_zone
        and max(a.crossing_start, b.crossing_start) < min(a.crossing_end, b.crossing_end)
        for a, b in combinations(assignments, 2)
    )


def audit_reservations(attempts: tuple[Attempt, ...] | list[Attempt]) -> int:
    """Count claims against an already accepted space in recorded event order."""
    accepted: set[str] = set()
    conflicts = 0
    for attempt in attempts:
        if attempt.space_id is None or attempt.outcome == "dropped":
            continue
        if attempt.space_id in accepted:
            conflicts += 1
        if attempt.outcome == "accepted":
            accepted.add(attempt.space_id)
    return conflicts


def _p95(values: list[int]) -> int:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else 0


def _metrics(mode: Mode, assignments: list[Assignment], attempts: list[Attempt],
             unassigned: dict[str, str], vehicle_count: int) -> RunMetrics:
    exploration = [a.exploration_ticks for a in assignments]
    completion = [a.crossing_end + a.distance_ticks - a.arrival_time for a in assignments]
    return RunMetrics(
        mode=mode, vehicle_count=vehicle_count, assignments=len(assignments),
        unassigned=len(unassigned),
        capacity_failures=sum(reason == "no-space" for reason in unassigned.values()),
        timed_out=sum(reason == "timeout" for reason in unassigned.values()),
        reservation_conflicts=audit_reservations(attempts),
        duplicate_assignments=sum(a.space_id == b.space_id for a, b in combinations(assignments, 2)),
        crossing_conflicts=audit_crossings(assignments),
        # A single-resource route has no wait-for graph. Do not report a fake zero.
        deadlocks=None,
        dropped_messages=sum(a.outcome == "dropped" for a in attempts),
        average_exploration_ticks=round(sum(exploration) / len(exploration), 2) if exploration else 0.0,
        p95_exploration_ticks=_p95(exploration),
        average_completion_ticks=round(sum(completion) / len(completion), 2) if completion else 0.0,
        total_distance_ticks=sum(a.distance_ticks for a in assignments),
        total_wait_ticks=sum(a.wait_ticks for a in assignments),
    )


def run(mode: Mode, vehicles: tuple[Vehicle, ...] | None = None,
        spaces: tuple[ParkingSpace, ...] | None = None,
        config: RunConfig | None = None) -> SimulationResult:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    default_vehicles, default_spaces = demo_scenario()
    vehicles = default_vehicles if vehicles is None else tuple(vehicles)
    spaces = default_spaces if spaces is None else tuple(spaces)
    config = config or RunConfig()
    if len({v.vehicle_id for v in vehicles}) != len(vehicles):
        raise ValueError("vehicle IDs must be unique")
    if len({s.space_id for s in spaces}) != len(spaces):
        raise ValueError("space IDs must be unique")
    if any(v.arrival_time < 0 or v.gate not in ("A", "B") for v in vehicles):
        raise ValueError("vehicles require a nonnegative arrival and gate A or B")
    if any(min(s.distance_from_gate_a, s.distance_from_gate_b) < 0 for s in spaces):
        raise ValueError("distances must be nonnegative")

    # Each vehicle gets the same network-loss sequence across modes, independently
    # of how many retries other vehicles need. Scheduling order is canonical.
    rngs = {v.vehicle_id: random.Random(f"{config.seed}:{v.vehicle_id}") for v in vehicles}
    pending = {v.vehicle_id: v.arrival_time + 1 + config.message_delay_ticks for v in vehicles}
    assigned: dict[str, int] = {}  # space -> accepted tick, permanent within a run
    pass_available: dict[str, int] = {}
    assignments: list[Assignment] = []
    attempts: list[Attempt] = []
    unassigned: dict[str, str] = {}
    ordered = sorted(vehicles, key=lambda v: v.vehicle_id)

    for tick in range(config.max_ticks + 1):
        # Independent cars share a batch snapshot. Ideal zero-delay P2P sees
        # earlier intents in the same tick; delay makes those intents stale.
        snapshot = {s for s, t in assigned.items() if t < tick - config.message_delay_ticks}
        for vehicle in ordered:
            if pending.get(vehicle.vehicle_id) != tick:
                continue
            if rngs[vehicle.vehicle_id].random() < config.packet_loss_rate:
                attempts.append(Attempt(vehicle.vehicle_id, tick, None, "dropped"))
                pending[vehicle.vehicle_id] = tick + config.retry_ticks + config.message_delay_ticks
                continue
            if mode == "hybrid":
                known = set(assigned)
            elif mode == "p2p-only":
                known = {s for s, t in assigned.items() if t <= tick - config.message_delay_ticks}
            else:
                known = snapshot
            candidates = [s for s in spaces if s.space_id not in known]
            if not candidates:
                attempts.append(Attempt(vehicle.vehicle_id, tick, None, "no-space"))
                unassigned[vehicle.vehicle_id] = "no-space"
                del pending[vehicle.vehicle_id]
                continue
            chosen = min(candidates, key=lambda s: _space_sort_key(vehicle, s))
            if chosen.space_id in assigned:
                attempts.append(Attempt(vehicle.vehicle_id, tick, chosen.space_id, "conflict"))
                pending[vehicle.vehicle_id] = tick + config.retry_ticks + config.message_delay_ticks
                continue
            attempts.append(Attempt(vehicle.vehicle_id, tick, chosen.space_id, "accepted"))
            assigned[chosen.space_id] = tick
            del pending[vehicle.vehicle_id]
            crossing_start = tick
            if mode == "hybrid":
                crossing_start = max(tick, pass_available.get(chosen.conflict_zone, 0))
                pass_available[chosen.conflict_zone] = crossing_start + 1
            assignments.append(Assignment(
                vehicle.vehicle_id, chosen.space_id, vehicle.arrival_time, tick,
                crossing_start, crossing_start + 1, tick - vehicle.arrival_time,
                crossing_start - tick, chosen.distance_from(vehicle.gate), chosen.conflict_zone,
            ))
        if not pending:
            break
    unassigned.update({vehicle_id: "timeout" for vehicle_id in pending})
    assignments.sort(key=lambda a: a.vehicle_id)
    return SimulationResult(_metrics(mode, assignments, attempts, unassigned, len(vehicles)),
                            tuple(assignments), tuple(attempts), unassigned, config)


def compare(vehicles: tuple[Vehicle, ...] | None = None,
            spaces: tuple[ParkingSpace, ...] | None = None,
            config: RunConfig | None = None) -> dict[str, dict[str, object]]:
    return {mode: run(mode, vehicles, spaces, config).metrics.as_dict() for mode in MODES}


def experiment(seeds: int, config: RunConfig) -> dict[str, object]:
    if seeds < 1:
        raise ValueError("seeds must be positive")
    runs = []
    exploration = {mode: [] for mode in MODES}
    completion = {mode: [] for mode in MODES}
    for seed in range(seeds):
        vehicles, spaces = seeded_scenario(seed)
        modes = {}
        for mode in MODES:
            result = run(mode, vehicles, spaces, replace(config, seed=seed))
            modes[mode] = result.metrics.as_dict()
            exploration[mode].extend(a.exploration_ticks for a in result.assignments)
            completion[mode].extend(a.crossing_end + a.distance_ticks - a.arrival_time
                                    for a in result.assignments)
        runs.append({"seed": seed, "modes": modes})
    summary = {}
    for mode in MODES:
        metrics = [row["modes"][mode] for row in runs]
        summary[mode] = {
            "runs": seeds,
            "average_exploration_ticks": round(sum(exploration[mode]) / len(exploration[mode]), 2) if exploration[mode] else None,
            "p95_exploration_ticks": _p95(exploration[mode]) if exploration[mode] else None,
            "average_completion_ticks": round(sum(completion[mode]) / len(completion[mode]), 2) if completion[mode] else None,
            "p95_completion_ticks": _p95(completion[mode]) if completion[mode] else None,
            "assignments": sum(m["assignments"] for m in metrics),
            "unassigned": sum(m["unassigned"] for m in metrics),
            "reservation_conflicts": sum(m["reservation_conflicts"] for m in metrics),
            "duplicate_assignments": sum(m["duplicate_assignments"] for m in metrics),
            "crossing_conflicts": sum(m["crossing_conflicts"] for m in metrics),
            "dropped_messages": sum(m["dropped_messages"] for m in metrics),
        }
    return {"config": asdict(config), "summary": summary, "runs": runs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, help="Run randomized scenarios for seeds 0..N-1")
    parser.add_argument("--delay", type=int, default=0, help="Message/observation delay in ticks")
    parser.add_argument("--loss", type=float, default=0, help="Request loss probability, 0..1")
    parser.add_argument("--max-ticks", type=int, default=100)
    parser.add_argument("--replay", choices=MODES, help="Include assignments and attempt ledger")
    args = parser.parse_args()
    try:
        config = RunConfig(message_delay_ticks=args.delay, packet_loss_rate=args.loss, max_ticks=args.max_ticks)
        if args.seeds is not None and args.replay:
            raise ValueError("--seeds and --replay are mutually exclusive")
        result = (experiment(args.seeds, config) if args.seeds is not None else
                  run(args.replay, config=config).as_dict() if args.replay else compare(config=config))
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
