#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Fail-closed legality, profitability, and selection model for CIRA hoists."""

from dataclasses import dataclass, fields
import math
from typing import Mapping, Sequence


class PolicyError(RuntimeError):
    """A CIRA policy input is incomplete, non-causal, or invalid."""


@dataclass(frozen=True)
class HoistCandidate:
    name: str
    operands_dominate: bool
    guards_available: bool
    alias_safe: bool
    invalidation_safe: bool
    lifetime_safe: bool
    available_slack_ns: float
    issue_ns: float
    index_walk_ns: float
    queue_wait_ns: float
    cxl_memory_ns: float
    cache_install_ns: float
    expected_saved_stall_ns: float
    usefulness_probability: float
    descriptor_formation_ns: float
    runtime_guards_ns: float
    selection_cost_ns: float
    extra_traffic_ns: float
    cache_pollution_ns: float
    late_request_ns: float
    lead_rows: int
    descriptor_entries: int = 1
    csr_walk_entries: int = 1
    outstanding_reads: int = 1
    destination_ports: int = 1
    mshrs: int = 1


@dataclass(frozen=True)
class ResourceState:
    descriptor_queue_free: int
    csr_walk_queue_free: int
    outstanding_reads_free: int
    destination_ports_free: int
    mshrs_free: int
    max_lead_rows: int


@dataclass(frozen=True)
class HoistDecision:
    legal: bool
    profitable: bool
    emit_prefetch: bool
    reason: str
    available_slack_ns: float
    issue_ns: float
    index_walk_ns: float
    queue_wait_ns: float
    cxl_memory_ns: float
    cache_install_ns: float
    required_slack_ns: float
    expected_saved_stall_ns: float
    usefulness_probability: float
    effective_saved_stall_ns: float
    descriptor_formation_ns: float
    runtime_guards_ns: float
    selection_cost_ns: float
    extra_traffic_ns: float
    cache_pollution_ns: float
    late_request_ns: float
    total_overhead_ns: float
    net_benefit_ns: float


_DURATION_FIELDS = (
    "available_slack_ns",
    "issue_ns",
    "index_walk_ns",
    "queue_wait_ns",
    "cxl_memory_ns",
    "cache_install_ns",
    "expected_saved_stall_ns",
    "descriptor_formation_ns",
    "runtime_guards_ns",
    "selection_cost_ns",
    "extra_traffic_ns",
    "cache_pollution_ns",
    "late_request_ns",
)
_CANDIDATE_CAPACITY_FIELDS = (
    "lead_rows",
    "descriptor_entries",
    "csr_walk_entries",
    "outstanding_reads",
    "destination_ports",
    "mshrs",
)
_RESOURCE_FIELDS = tuple(field.name for field in fields(ResourceState))


def _finite_nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{name} must be numeric")
    if not math.isfinite(value) or value < 0:
        raise PolicyError(f"{name} must be finite and non-negative")


def _validate(candidate, resources):
    if not isinstance(candidate, HoistCandidate):
        raise PolicyError("candidate must be a HoistCandidate")
    if not isinstance(resources, ResourceState):
        raise PolicyError("resources must be a ResourceState")
    if not candidate.name:
        raise PolicyError("candidate name is empty")
    for name in _DURATION_FIELDS:
        _finite_nonnegative(getattr(candidate, name), name)
    probability = candidate.usefulness_probability
    _finite_nonnegative(probability, "usefulness_probability")
    if probability > 1:
        raise PolicyError("usefulness_probability must be at most one")
    for name in _CANDIDATE_CAPACITY_FIELDS:
        value = getattr(candidate, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError(f"{name} must be a non-negative integer")
    for name in _RESOURCE_FIELDS:
        value = getattr(resources, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError(f"{name} must be a non-negative integer")


def _make_decision(candidate, *, legal, profitable, emit, reason):
    required_slack = math.fsum(
        (
            candidate.issue_ns,
            candidate.index_walk_ns,
            candidate.queue_wait_ns,
            candidate.cxl_memory_ns,
            candidate.cache_install_ns,
        )
    )
    effective_saved = (
        candidate.expected_saved_stall_ns * candidate.usefulness_probability
    )
    overhead = math.fsum(
        (
            candidate.descriptor_formation_ns,
            candidate.runtime_guards_ns,
            candidate.selection_cost_ns,
            candidate.extra_traffic_ns,
            candidate.cache_pollution_ns,
            candidate.late_request_ns,
        )
    )
    return HoistDecision(
        legal=legal,
        profitable=profitable,
        emit_prefetch=emit,
        reason=reason,
        available_slack_ns=candidate.available_slack_ns,
        issue_ns=candidate.issue_ns,
        index_walk_ns=candidate.index_walk_ns,
        queue_wait_ns=candidate.queue_wait_ns,
        cxl_memory_ns=candidate.cxl_memory_ns,
        cache_install_ns=candidate.cache_install_ns,
        required_slack_ns=required_slack,
        expected_saved_stall_ns=candidate.expected_saved_stall_ns,
        usefulness_probability=candidate.usefulness_probability,
        effective_saved_stall_ns=effective_saved,
        descriptor_formation_ns=candidate.descriptor_formation_ns,
        runtime_guards_ns=candidate.runtime_guards_ns,
        selection_cost_ns=candidate.selection_cost_ns,
        extra_traffic_ns=candidate.extra_traffic_ns,
        cache_pollution_ns=candidate.cache_pollution_ns,
        late_request_ns=candidate.late_request_ns,
        total_overhead_ns=overhead,
        net_benefit_ns=effective_saved - overhead,
    )


def evaluate(candidate: HoistCandidate, resources: ResourceState) -> HoistDecision:
    """Evaluate one candidate in frozen fail-first order."""

    _validate(candidate, resources)
    failures = (
        (not candidate.operands_dominate, "non-dominating-operands"),
        (not candidate.guards_available, "guard-unavailable"),
        (not candidate.alias_safe, "unsafe-alias"),
        (not candidate.invalidation_safe, "unsafe-invalidation"),
        (not candidate.lifetime_safe, "expired-lifetime"),
    )
    for failed, reason in failures:
        if failed:
            return _make_decision(
                candidate, legal=False, profitable=False, emit=False, reason=reason
            )

    capacity_ok = all(
        (
            candidate.descriptor_entries <= resources.descriptor_queue_free,
            candidate.csr_walk_entries <= resources.csr_walk_queue_free,
            candidate.outstanding_reads <= resources.outstanding_reads_free,
            candidate.destination_ports <= resources.destination_ports_free,
            candidate.mshrs <= resources.mshrs_free,
            candidate.lead_rows <= resources.max_lead_rows,
        )
    )
    if not capacity_ok:
        return _make_decision(
            candidate, legal=True, profitable=False, emit=False, reason="capacity"
        )

    decision = _make_decision(
        candidate, legal=True, profitable=False, emit=False, reason="pending"
    )
    if candidate.available_slack_ns < decision.required_slack_ns:
        return _make_decision(
            candidate,
            legal=True,
            profitable=False,
            emit=False,
            reason="insufficient-slack",
        )
    if decision.net_benefit_ns <= 0:
        return _make_decision(
            candidate,
            legal=True,
            profitable=False,
            emit=False,
            reason="non-positive-benefit",
        )
    return _make_decision(
        candidate, legal=True, profitable=True, emit=True, reason="profitable"
    )


def select_static(declared: HoistCandidate) -> HoistCandidate:
    if not isinstance(declared, HoistCandidate):
        raise PolicyError("static policy requires one declared candidate")
    return declared


def select_pgo(
    candidates: Mapping[str, HoistCandidate],
    completed_rows: Mapping[str, Mapping[str, object]],
) -> HoistCandidate:
    """Select from completed A/B/C source rows without inspecting ABC."""

    required = ("A", "B", "C")
    if set(candidates) != set(required):
        raise PolicyError("PGO candidates must be exactly A, B, and C")
    scored = []
    for order, name in enumerate(required):
        try:
            row = completed_rows[name]
            verification = row["verification"]
            return_code = row["return_code"]
            mean = row["mean_time_ms"]
        except (KeyError, TypeError) as error:
            raise PolicyError(f"PGO row {name} is incomplete") from error
        if verification != "PASS" or return_code != 0:
            raise PolicyError(f"PGO row {name} is not completed and verified")
        _finite_nonnegative(mean, f"PGO row {name} mean_time_ms")
        if mean == 0:
            raise PolicyError(f"PGO row {name} mean_time_ms must be positive")
        scored.append((float(mean), order, name))
    return candidates[min(scored)[2]]


class FewShotSelector:
    """Sequential, charged online sampling with one irreversible freeze."""

    def __init__(
        self,
        candidates: Sequence[str],
        *,
        samples_per_candidate: int,
        profiling_cost_ns_per_sample: float = 0,
        reconfiguration_cost_ns: float = 0,
    ):
        self._candidates = tuple(candidates)
        if not self._candidates or any(not name for name in self._candidates):
            raise PolicyError("few-shot candidates must be non-empty names")
        if len(set(self._candidates)) != len(self._candidates):
            raise PolicyError("few-shot candidates must be unique")
        if (
            isinstance(samples_per_candidate, bool)
            or not isinstance(samples_per_candidate, int)
            or samples_per_candidate <= 0
        ):
            raise PolicyError("samples_per_candidate must be positive")
        _finite_nonnegative(
            profiling_cost_ns_per_sample, "profiling_cost_ns_per_sample"
        )
        _finite_nonnegative(reconfiguration_cost_ns, "reconfiguration_cost_ns")
        self._samples_per_candidate = samples_per_candidate
        self._profiling_cost = float(profiling_cost_ns_per_sample)
        self._reconfiguration_cost = float(reconfiguration_cost_ns)
        self._samples = {name: [] for name in self._candidates}
        self._selected = None
        self.charged_profiling_ns = 0.0
        self.charged_reconfiguration_ns = 0.0

    @property
    def total_charged_ns(self):
        return self.charged_profiling_ns + self.charged_reconfiguration_ns

    def observe(self, candidate: str, latency_ns: float):
        if self._selected is not None:
            raise PolicyError("cannot observe after freeze")
        if candidate not in self._samples:
            raise PolicyError(f"unknown few-shot candidate {candidate}")
        if len(self._samples[candidate]) >= self._samples_per_candidate:
            raise PolicyError(f"too many samples for {candidate}")
        _finite_nonnegative(latency_ns, "few-shot latency_ns")
        if latency_ns == 0:
            raise PolicyError("few-shot latency_ns must be positive")
        self._samples[candidate].append(float(latency_ns))
        self.charged_profiling_ns += self._profiling_cost

    def freeze(self):
        if self._selected is not None:
            raise PolicyError("few-shot policy is already frozen")
        missing = [
            name
            for name in self._candidates
            if len(self._samples[name]) != self._samples_per_candidate
        ]
        if missing:
            raise PolicyError("missing samples for " + ", ".join(missing))
        means = (
            (
                math.fsum(self._samples[name]) / self._samples_per_candidate,
                order,
                name,
            )
            for order, name in enumerate(self._candidates)
        )
        self._selected = min(means)[2]
        self.charged_reconfiguration_ns = self._reconfiguration_cost
        return self._selected

    def select(self):
        if self._selected is None:
            raise PolicyError("cannot select before freeze")
        return self._selected
