#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Deterministic paired timing windows and bootstrap reconstruction."""

import dataclasses
import hashlib
import random
import re
from decimal import Decimal, ROUND_FLOOR

try:
    from scripts import cross_system_contract as contract
except ImportError:
    import cross_system_contract as contract


LEVELS = (8, 16, 32, 64)
BOOTSTRAP_RESAMPLES = 10_000
GAP_BC_VERTEX_WINDOW = 4
GAP_BC_PHASES = frozenset(("bc_bfs", "bc_reverse"))
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TimingError(RuntimeError):
    """A timing sample violates the paired deterministic contract."""


@dataclasses.dataclass(frozen=True)
class TimingWindow:
    stratum: int
    warmup_start: int
    measure_start: int
    measure_stop: int


@dataclasses.dataclass(frozen=True)
class SamplingPlan:
    trace_sha256: str
    phase: str
    work_items: int
    length: int
    full_phase: bool
    seed: int
    windows: tuple[TimingWindow, ...]

    def coordinates(self, level):
        if level not in LEVELS:
            raise TimingError(f"sampling level must be one of {LEVELS}")
        if self.full_phase:
            return self.windows
        return self.windows[:level]


@dataclasses.dataclass(frozen=True)
class PhaseEstimate:
    full_work_items: int
    seconds_per_item: tuple[Decimal, ...]

    def __post_init__(self):
        if (
            not isinstance(self.full_work_items, int)
            or isinstance(self.full_work_items, bool)
            or self.full_work_items <= 0
        ):
            raise TimingError("phase work-item count must be positive")
        if not self.seconds_per_item:
            raise TimingError("phase timing samples are empty")
        for value in self.seconds_per_item:
            if _decimal(value, "phase seconds per item") <= 0:
                raise TimingError("phase seconds per item must be positive")


@dataclasses.dataclass(frozen=True)
class BootstrapResult:
    speedup: Decimal
    ci_low: Decimal
    ci_high: Decimal
    relative_half_width: Decimal
    publishable: bool
    resamples: int = BOOTSTRAP_RESAMPLES


def _require_sha256(value):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TimingError("trace SHA-256 is invalid")


def _require_positive_count(value):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise TimingError("phase work-item count must be positive")


def _reverse_six_bits(value):
    return int(f"{value:06b}"[::-1], 2)


def make_plan(trace_sha256, phase, count):
    _require_sha256(trace_sha256)
    if not isinstance(phase, str) or not phase:
        raise TimingError("phase name is invalid")
    _require_positive_count(count)
    length = min(65_536, count // 128)
    bounded_gap_bc = phase in GAP_BC_PHASES and count >= 128 * (
        2 * GAP_BC_VERTEX_WINDOW
    )
    if bounded_gap_bc:
        length = GAP_BC_VERTEX_WINDOW
    seed = int(
        hashlib.sha256(f"{trace_sha256}:{phase}".encode("utf-8")).hexdigest()[:16],
        16,
    )
    if length < 1_024 and not bounded_gap_bc:
        return SamplingPlan(
            trace_sha256,
            phase,
            count,
            count,
            True,
            seed,
            (TimingWindow(0, 0, 0, count),),
        )
    rng = random.Random(seed)
    by_stratum = []
    for stratum in range(64):
        start = stratum * count // 64
        stop = (stratum + 1) * count // 64
        available = stop - start - 2 * length
        if available < 0:
            raise TimingError(
                f"stratum {stratum} cannot hold warmup and measured windows"
            )
        warmup_start = start + rng.randrange(available + 1)
        measure_start = warmup_start + length
        by_stratum.append(
            TimingWindow(
                stratum,
                warmup_start,
                measure_start,
                measure_start + length,
            )
        )
    windows = tuple(by_stratum[_reverse_six_bits(index)] for index in range(64))
    return SamplingPlan(
        trace_sha256, phase, count, length, False, seed, windows
    )


def write_plan(path, plan):
    if not isinstance(plan, SamplingPlan):
        raise TimingError("sampling plan has the wrong type")
    value = {
        "schema": 1,
        "trace_sha256": plan.trace_sha256,
        "phase": plan.phase,
        "work_items": plan.work_items,
        "length": plan.length,
        "full_phase": plan.full_phase,
        "seed": plan.seed,
        "windows": [dataclasses.asdict(window) for window in plan.windows],
    }
    return contract.atomic_write_json(path, value)


def read_plan(path):
    value = contract.load_json(path)
    required = {
        "schema",
        "trace_sha256",
        "phase",
        "work_items",
        "length",
        "full_phase",
        "seed",
        "windows",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise TimingError("sampling plan fields differ")
    if value["schema"] != 1:
        raise TimingError("sampling plan schema must be 1")
    windows = tuple(TimingWindow(**row) for row in value["windows"])
    expected = make_plan(
        value["trace_sha256"], value["phase"], value["work_items"]
    )
    observed = SamplingPlan(
        value["trace_sha256"],
        value["phase"],
        value["work_items"],
        value["length"],
        value["full_phase"],
        value["seed"],
        windows,
    )
    if observed != expected:
        raise TimingError("sampling plan coordinates are not canonical")
    return observed


def _decimal(value, label):
    if isinstance(value, bool) or isinstance(value, float):
        raise TimingError(f"{label} must use exact Decimal-compatible values")
    try:
        return value if isinstance(value, Decimal) else Decimal(value)
    except Exception as error:
        raise TimingError(f"{label} is not numeric") from error


def reconstruct(fixed_seconds, phases):
    result = _decimal(fixed_seconds, "fixed seconds")
    if result < 0:
        raise TimingError("fixed seconds must be nonnegative")
    for phase in phases:
        if not isinstance(phase, PhaseEstimate):
            raise TimingError("phase estimate has the wrong type")
        samples = tuple(
            _decimal(value, "phase seconds per item")
            for value in phase.seconds_per_item
        )
        mean = sum(samples, Decimal(0)) / Decimal(len(samples))
        result += Decimal(phase.full_work_items) * mean
    return result


def _percentile(sorted_values, probability):
    if not sorted_values:
        raise TimingError("bootstrap values are empty")
    position = Decimal(len(sorted_values) - 1) * probability
    lower_index = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - Decimal(lower_index)
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower + (upper - lower) * fraction


def bootstrap_speedup(vanilla, system, *, seed):
    vanilla = tuple(_decimal(value, "Vanilla window") for value in vanilla)
    system = tuple(_decimal(value, "system window") for value in system)
    if not vanilla or len(vanilla) != len(system):
        raise TimingError("paired window counts differ")
    if any(value <= 0 for value in vanilla + system):
        raise TimingError("paired window times must be positive")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TimingError("bootstrap seed must be an integer")
    estimate = sum(vanilla, Decimal(0)) / sum(system, Decimal(0))
    rng = random.Random(seed)
    samples = []
    count = len(vanilla)
    for _ in range(BOOTSTRAP_RESAMPLES):
        indices = tuple(rng.randrange(count) for _ in range(count))
        numerator = sum((vanilla[index] for index in indices), Decimal(0))
        denominator = sum((system[index] for index in indices), Decimal(0))
        samples.append(numerator / denominator)
    samples.sort()
    low = _percentile(samples, Decimal("0.025"))
    high = _percentile(samples, Decimal("0.975"))
    relative_half_width = (high - low) / (Decimal(2) * estimate)
    return BootstrapResult(
        estimate,
        low,
        high,
        relative_half_width,
        relative_half_width <= Decimal("0.05"),
    )


def final_status(result, *, level):
    if not isinstance(result, BootstrapResult):
        raise TimingError("bootstrap result has the wrong type")
    if level not in LEVELS:
        raise TimingError(f"sampling level must be one of {LEVELS}")
    if result.publishable:
        return "complete"
    return "inconclusive" if level == LEVELS[-1] else "expand"
