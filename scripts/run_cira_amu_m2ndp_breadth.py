#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Collect correctness-gated, paired Vanilla/AMU/CIRA/M2NDP breadth evidence.

This module owns the experiment state machine.  It deliberately checkpoints
only at reproducible boundaries; process lifetime is not evidence state.
"""

import argparse
import dataclasses
import hashlib
import json
import random
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import cxl_latency_spectrum as latency
    from scripts import run_pr_asymmetric_offload as offload
    from scripts import stratified_timing as timing
except ImportError:
    import cross_system_contract as contract
    import cxl_latency_spectrum as latency
    import run_pr_asymmetric_offload as offload
    import stratified_timing as timing


REPO = Path(__file__).resolve().parents[1]
WORKLOADS = (
    "pr_spmv",
    "mcf",
    "amg_gather",
    "lulesh_scatter",
    "npb_cg",
    "npb_mg",
)
FUNCTIONAL_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp-funcsim")
TIMING_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
CORRECTNESS_POLICIES = ("bit-exact", "native-verified")
LEVELS = timing.LEVELS
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BreadthError(RuntimeError):
    """Breadth evidence failed an identity, correctness, or pairing gate."""


class BreadthInputError(BreadthError):
    """A frozen formal input or prepared executable artifact is unavailable."""


@dataclasses.dataclass(frozen=True)
class Action:
    stage: str
    workload: str
    system: str | None = None
    phase: str | None = None
    window_index: int | None = None
    level: int | None = None
    stratum: int | None = None
    warmup_start: int | None = None
    measure_start: int | None = None
    measure_stop: int | None = None
    cxl_link_delay: str | None = None
    cxl_link_delay_ticks: int | None = None


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aggregate_files(records, label):
    if not contract.verify_named_hashes(records):
        raise BreadthError(f"prepared {label} file bindings are invalid")
    return hashlib.sha256(contract.canonical_json({
        name: record["sha256"] for name, record in sorted(records.items())
    })).hexdigest()


def _decimal(value, label):
    if isinstance(value, bool) or isinstance(value, float):
        raise BreadthError(f"{label} must be an exact decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BreadthError(f"{label} is not numeric") from error
    if not result.is_finite():
        raise BreadthError(f"{label} must be finite")
    return result


def _plan_dict(plan):
    return {
        "trace_sha256": plan.trace_sha256,
        "phase": plan.phase,
        "work_items": plan.work_items,
        "length": plan.length,
        "full_phase": plan.full_phase,
        "seed": plan.seed,
        "windows": [dataclasses.asdict(window) for window in plan.windows],
    }


def _validate_specs(specifications):
    if not isinstance(specifications, dict) or not specifications:
        raise BreadthError("workload specifications are empty")
    normalized = {}
    for workload, row in specifications.items():
        if not isinstance(workload, str) or not workload:
            raise BreadthError("workload name is invalid")
        if not isinstance(row, dict):
            raise BreadthError(f"{workload} specification is invalid")
        trace_hash = row.get("trace_sha256")
        if not isinstance(trace_hash, str) or _SHA256.fullmatch(trace_hash) is None:
            raise BreadthError(f"{workload} trace SHA-256 is invalid")
        phases = row.get("phases")
        if not isinstance(phases, dict) or not phases:
            raise BreadthError(f"{workload} phases are empty")
        normalized_phases = {}
        for phase, count in phases.items():
            if (
                not isinstance(phase, str)
                or not phase
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
            ):
                raise BreadthError(f"{workload} phase specification is invalid")
            normalized_phases[phase] = count
        normalized[workload] = {
            "trace_sha256": trace_hash,
            "phases": normalized_phases,
        }
    return normalized


def _canonical_latency(label):
    try:
        return label, latency.ticks(label)
    except latency.LatencyError as error:
        raise BreadthError(str(error)) from error


def _correctness_policy(value):
    if value not in CORRECTNESS_POLICIES:
        raise BreadthError(f"unsupported correctness policy: {value}")
    return value


def new_state(
    identity, workload_specs, *, g20_graph_sha256, cxl_link_delay="1us",
    correctness_policy="bit-exact",
):
    if not isinstance(identity, contract.ExperimentIdentity):
        raise BreadthError("experiment identity has the wrong type")
    if (
        not isinstance(g20_graph_sha256, str)
        or _SHA256.fullmatch(g20_graph_sha256) is None
    ):
        raise BreadthError("g20 graph SHA-256 is invalid")
    specifications = _validate_specs(workload_specs)
    correctness_policy = _correctness_policy(correctness_policy)
    cxl_link_delay, cxl_link_delay_ticks = _canonical_latency(cxl_link_delay)
    workloads = {}
    for workload, specification in specifications.items():
        phases = {}
        for phase, count in specification["phases"].items():
            plan = timing.make_plan(specification["trace_sha256"], phase, count)
            phases[phase] = {
                "work_items": count,
                "plan": _plan_dict(plan),
                "level": 8,
                "status": "pending",
                "windows": {},
            }
        workloads[workload] = {
            "trace_sha256": specification["trace_sha256"],
            "status": "reference_pending",
            "reference": {},
            "functional": {
                system: {"status": "pending"}
                for system in FUNCTIONAL_SYSTEMS
            },
            "fixed_seconds": {},
            "phases": phases,
        }
    return {
        "schema": 1,
        "status": "planned",
        "reason": "",
        "identity": dataclasses.asdict(identity),
        "identity_sha256": identity.digest(),
        "cxl_link_delay": cxl_link_delay,
        "cxl_link_delay_ticks": cxl_link_delay_ticks,
        "correctness_policy": correctness_policy,
        "g20_graph_sha256": g20_graph_sha256,
        "workload_order": list(specifications),
        "workloads": workloads,
        "results": {},
        "evidence_files": {},
    }


def _hash_map(value, label):
    if not isinstance(value, dict) or not value:
        raise BreadthError(f"{label} output hashes are missing")
    for name, digest in value.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise BreadthError(f"{label} output SHA-256 is invalid")
    return dict(sorted(value.items()))


def _boundary_records(value, label):
    if not isinstance(value, dict) or not value:
        raise BreadthError(f"{label} raw output boundaries are missing")
    normalized = {}
    for name, record in value.items():
        if not isinstance(name, str) or not name or not isinstance(record, dict):
            raise BreadthError(f"{label} raw output boundary is invalid")
        if set(record) != {"path", "sha256", "word_bits", "count"}:
            raise BreadthError(f"{label} raw output boundary fields differ")
        path = Path(record["path"])
        bits = record["word_bits"]
        count = record["count"]
        digest = record["sha256"]
        if (
            not path.is_absolute()
            or not path.is_file()
            or bits not in (32, 64)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or path.stat().st_size != count * (bits // 8)
            or _sha256_file(path) != digest
        ):
            raise BreadthError(f"{label} raw output boundary evidence differs")
        normalized[name] = {
            "sha256": digest, "word_bits": bits, "count": count,
        }
    return dict(sorted(normalized.items()))


def record_reference(state, workload, boundaries):
    row = _workload(state, workload)
    if state.get("status") != "planned" or row["status"] != "reference_pending":
        raise BreadthError(f"{workload} reference is out of order")
    row["reference"] = _boundary_records(boundaries, workload)
    row["status"] = "functional_pending"
    return state


def _counter_errors(row):
    counters = row.get("error_counters")
    if not isinstance(counters, dict):
        raise BreadthError("mechanism error counters are missing")
    total = 0
    for name, value in counters.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise BreadthError("mechanism error counters are invalid")
        total += value
    return total


def _boundary_layout(records):
    return {
        name: {
            "word_bits": record["word_bits"],
            "count": record["count"],
        }
        for name, record in sorted(records.items())
    }


def _valid_functional_row(row, correctness_policy="bit-exact"):
    try:
        correctness_policy = _correctness_policy(correctness_policy)
        compared = row["compared_words"]
        mismatched = row["mismatched_words"]
        common = (
            row.get("status") == "pass"
            and isinstance(compared, int)
            and not isinstance(compared, bool)
            and compared > 0
            and isinstance(mismatched, int)
            and not isinstance(mismatched, bool)
            and mismatched >= 0
            and _counter_errors(row) == 0
        )
        if correctness_policy == "bit-exact":
            return common and row.get("bit_exact") is True and mismatched == 0
        return (
            common
            and row.get("verification") == "pass"
            and row.get("numeric_verification") == "pass"
            and isinstance(row.get("nonfinite_words"), int)
            and not isinstance(row.get("nonfinite_words"), bool)
            and row.get("nonfinite_words") == 0
        )
    except (KeyError, BreadthError):
        return False


def _valid_numerical_evidence(row, correctness_policy):
    correctness_policy = _correctness_policy(correctness_policy)
    compared = row.get("compared_words")
    mismatched = row.get("mismatched_words")
    common = (
        row.get("verification") == "pass"
        and isinstance(compared, int)
        and not isinstance(compared, bool)
        and compared > 0
        and isinstance(mismatched, int)
        and not isinstance(mismatched, bool)
        and mismatched >= 0
    )
    if correctness_policy == "bit-exact":
        return common and row.get("bit_exact") is True and mismatched == 0
    return (
        common
        and row.get("numeric_verification") == "pass"
        and isinstance(row.get("nonfinite_words"), int)
        and not isinstance(row.get("nonfinite_words"), bool)
        and row.get("nonfinite_words") == 0
    )


def functional_complete(records, correctness_policy="bit-exact"):
    return (
        isinstance(records, dict)
        and set(records) == set(FUNCTIONAL_SYSTEMS)
        and all(
            _valid_functional_row(row, correctness_policy)
            for row in records.values()
        )
    )


def _positive_integer(row, field):
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BreadthError(f"{field} must be a positive integer")
    return value


def _zero_integer(row, field):
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value != 0:
        raise BreadthError(f"{field} must be zero")
    return value


def _validate_mechanism(system, row, *, timing_window=False):
    if system == "vanilla":
        if row.get("error_counters") != {}:
            raise BreadthError("Vanilla error counters must be explicitly empty")
        return
    if system == "amu":
        issued = _positive_integer(row, "issued_loads")
        if _positive_integer(row, "completed_loads") != issued:
            raise BreadthError("AMU issued/completed loads differ")
        drains = _positive_integer(row, "drains")
        phases = _positive_integer(row, "phases")
        if drains > phases * 4:
            raise BreadthError("AMU per-request drain is forbidden")
        required = {
            "queue_full", "spm_full", "translation", "pending",
            "far_spm_flag", "spm_missing_flag",
        }
        if set(row.get("error_counters", {})) != required:
            raise BreadthError("AMU error counter set differs")
        if _counter_errors(row):
            raise BreadthError("AMU error counters are nonzero")
        return
    if system == "cira":
        issued = _positive_integer(row, "issued_prefetches")
        if _positive_integer(row, "completed_prefetches") != issued:
            raise BreadthError("CIRA issued/completed prefetches differ")
        per_issued = row.get("issued_per_core")
        per_completed = row.get("completed_per_core")
        if (
            not isinstance(per_issued, list)
            or len(per_issued) != 4
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                   for value in per_issued)
            or per_completed != per_issued
            or sum(per_issued) != issued
        ):
            raise BreadthError("CIRA requires matching activity on four cores")
        required = {"queue_full", "csr_index_queue_full", "dropped_descriptors"}
        if set(row.get("error_counters", {})) != required:
            raise BreadthError("CIRA error counter set differs")
        if _counter_errors(row):
            raise BreadthError("CIRA error counters are nonzero")
        return
    if system in {"m2ndp-funcsim", "m2ndp"}:
        operations = _positive_integer(row, "expected_operations")
        if _positive_integer(row, "compared_operations") != operations:
            raise BreadthError("M2NDP operation cardinality differs")
        launches = _positive_integer(row, "expected_launches")
        if _positive_integer(row, "completed_launches") != launches:
            raise BreadthError("M2NDP launch cardinality differs")
        if row.get("funcsim_status") != "pass":
            raise BreadthError("M2NDP FuncSim gate failed")
        if row.get("error_counters") != {}:
            raise BreadthError("M2NDP error counters must be explicitly empty")
        if timing_window:
            if row.get("memory_match") != "pass":
                raise BreadthError("M2NDP memory-match gate failed")
            if row.get("calibration_pass") is not True:
                raise BreadthError("M2NDP calibration gate failed")
            residual = _decimal(row.get("calibration_residual_ns"), "calibration residual")
            period = _decimal(row.get("calibration_link_period_ns"), "link period")
            if residual < 0 or period <= 0 or residual > period:
                raise BreadthError("M2NDP calibration exceeds one link cycle")
        return
    raise BreadthError(f"unsupported mechanism system: {system}")


def record_functional(state, workload, system, record):
    row = _workload(state, workload)
    if system not in FUNCTIONAL_SYSTEMS:
        raise BreadthError(f"unsupported functional system: {system}")
    if state.get("status") != "planned" or not row["reference"]:
        raise BreadthError("functional replay requires a reference first")
    if not isinstance(record, dict):
        raise BreadthError("functional record is invalid")
    try:
        boundaries = _boundary_records(record.get("boundaries"), system)
        normalized = {
            **record,
            "outputs": _hash_map(record.get("outputs"), system),
            "boundary_hashes": boundaries,
        }
        correctness_policy = _correctness_policy(
            state.get("correctness_policy", "bit-exact")
        )
        if _boundary_layout(normalized["boundary_hashes"]) != _boundary_layout(
            row["reference"]
        ):
            raise BreadthError(f"{system} raw output boundary layout differs")
        if (
            correctness_policy == "bit-exact"
            and normalized["boundary_hashes"] != row["reference"]
        ):
            raise BreadthError(f"{system} raw output boundary hashes differ")
        exact_words = sum(record["count"] for record in boundaries.values())
        if normalized.get("compared_words") != exact_words:
            raise BreadthError(f"{system} compared-word cardinality differs")
        _validate_mechanism(system, normalized)
        if not _valid_functional_row(normalized, correctness_policy):
            if _counter_errors(normalized):
                raise BreadthError(f"{system} error counters are nonzero")
            if correctness_policy == "native-verified":
                raise BreadthError(
                    f"{system} workload-native numerical verification failed"
                )
            raise BreadthError(f"{system} functional bit-exact gate failed")
    except BreadthError as error:
        state["status"] = "failed"
        state["reason"] = str(error)
        raise
    row["functional"][system] = normalized
    if functional_complete(row["functional"], correctness_policy):
        row["status"] = "functional_pass"
    return state


def begin_timing(state):
    if state.get("status") != "planned":
        raise BreadthError("timing transition requires planned state")
    if not all(
        row.get("status") == "functional_pass"
        and functional_complete(
            row.get("functional"), state.get("correctness_policy", "bit-exact")
        )
        for row in state.get("workloads", {}).values()
    ):
        raise BreadthError(
            "all functional correctness gates must pass before timing"
        )
    try:
        transitioned = contract.transition(state, "functional_pass")
        transitioned = contract.transition(transitioned, "timing_in_progress")
    except contract.ContractError as error:
        raise BreadthError(str(error)) from error
    state.clear()
    state.update(transitioned)
    return state


def _workload(state, workload):
    try:
        row = state["workloads"][workload]
    except (KeyError, TypeError) as error:
        raise BreadthError(f"unknown workload: {workload}") from error
    return row


def next_action(state):
    status = state.get("status")
    if status == "planned":
        for workload in state.get("workload_order", ()):
            row = _workload(state, workload)
            if row["status"] == "reference_pending":
                return Action("reference", workload)
        for workload in state.get("workload_order", ()):
            row = _workload(state, workload)
            for system in FUNCTIONAL_SYSTEMS:
                if row["functional"][system].get("status") == "pending":
                    return Action("functional", workload, system=system)
        return None
    if status != "timing_in_progress":
        return None
    for workload in state.get("workload_order", ()):
        row = _workload(state, workload)
        if row["status"] in {"complete", "inconclusive"}:
            continue
        for phase in row["phases"]:
            actions = pending_window_actions(state, workload, phase)
            if actions:
                return actions[0]
    return None


def record_fixed(state, workload, system, seconds):
    if state.get("status") != "timing_in_progress":
        raise BreadthError("fixed timing requires timing_in_progress")
    if system not in TIMING_SYSTEMS:
        raise BreadthError(f"unsupported timing system: {system}")
    value = _decimal(seconds, "fixed seconds")
    if value < 0:
        raise BreadthError("fixed seconds must be nonnegative")
    _workload(state, workload)["fixed_seconds"][system] = str(value)
    return state


def _window_rows(phase_row, level):
    plan = phase_row["plan"]
    windows = plan["windows"]
    return windows if plan["full_phase"] else windows[:level]


def pending_window_actions(state, workload, phase, *, system=None):
    row = _workload(state, workload)
    if phase not in row["phases"]:
        raise BreadthError(f"unknown phase: {workload}:{phase}")
    if system is not None and system not in TIMING_SYSTEMS:
        raise BreadthError(f"unsupported timing system: {system}")
    phase_row = row["phases"][phase]
    level = phase_row["level"]
    systems = (system,) if system is not None else TIMING_SYSTEMS
    actions = []
    for index, coordinate in enumerate(_window_rows(phase_row, level)):
        observed = phase_row["windows"].get(str(index), {})
        for selected in systems:
            if selected in observed:
                continue
            actions.append(Action(
                "window",
                workload,
                system=selected,
                phase=phase,
                window_index=index,
                level=level,
                stratum=coordinate["stratum"],
                warmup_start=coordinate["warmup_start"],
                measure_start=coordinate["measure_start"],
                measure_stop=coordinate["measure_stop"],
            ))
    return tuple(actions)


def record_window(
    state, workload, phase, window_index, system, seconds_per_item
):
    if state.get("status") != "timing_in_progress":
        raise BreadthError("window timing requires timing_in_progress")
    if system not in TIMING_SYSTEMS:
        raise BreadthError(f"unsupported timing system: {system}")
    row = _workload(state, workload)["phases"].get(phase)
    if row is None:
        raise BreadthError(f"unknown phase: {workload}:{phase}")
    coordinates = _window_rows(row, row["level"])
    if (
        not isinstance(window_index, int)
        or isinstance(window_index, bool)
        or window_index < 0
        or window_index >= len(coordinates)
    ):
        raise BreadthError("window index is outside the active nested level")
    value = _decimal(seconds_per_item, "seconds per item")
    if value <= 0:
        raise BreadthError("seconds per item must be positive")
    samples = row["windows"].setdefault(str(window_index), {})
    if system in samples:
        raise BreadthError("paired window system was already recorded")
    samples[system] = str(value)
    if all(
        set(row["windows"].get(str(index), {})) == set(TIMING_SYSTEMS)
        for index in range(len(coordinates))
    ):
        row["status"] = "level_complete"
    return state


def _require_fixed(row):
    if set(row.get("fixed_seconds", {})) != set(TIMING_SYSTEMS):
        raise BreadthError("complete fixed costs for all systems are required")


def reconstruct_system(state, workload, system, *, level):
    if system not in TIMING_SYSTEMS:
        raise BreadthError(f"unsupported timing system: {system}")
    if level not in LEVELS:
        raise BreadthError(f"timing level must be one of {LEVELS}")
    row = _workload(state, workload)
    _require_fixed(row)
    phases = []
    for phase, phase_row in row["phases"].items():
        coordinates = _window_rows(phase_row, level)
        samples = []
        for index in range(len(coordinates)):
            try:
                samples.append(
                    _decimal(
                        phase_row["windows"][str(index)][system],
                        f"{workload}:{phase}:{system} sample",
                    )
                )
            except KeyError as error:
                raise BreadthError(
                    f"paired timing is incomplete for {workload}:{phase}:{system}"
                ) from error
        phases.append(timing.PhaseEstimate(phase_row["work_items"], tuple(samples)))
    try:
        return timing.reconstruct(row["fixed_seconds"][system], phases)
    except timing.TimingError as error:
        raise BreadthError(str(error)) from error


def finish_timing(result, *, level):
    if level not in LEVELS:
        raise BreadthError(f"timing level must be one of {LEVELS}")
    if not isinstance(result, dict):
        raise BreadthError("timing result is invalid")
    relative = str(_decimal(result.get("relative_half_width"), "relative half width"))
    publishable = result.get("publishable") is True
    if publishable:
        status = "complete"
    elif level == LEVELS[-1]:
        status = "inconclusive"
    else:
        status = "expand"
    finished = {**result, "relative_half_width": relative, "status": status}
    if status == "expand":
        finished["next_level"] = LEVELS[LEVELS.index(level) + 1]
    return finished


def bootstrap_workload(state, workload, *, level, resamples=10_000):
    """Bootstrap complete E2E time with paired, phase-local resampling."""
    if level not in LEVELS:
        raise BreadthError(f"timing level must be one of {LEVELS}")
    if (
        not isinstance(resamples, int)
        or isinstance(resamples, bool)
        or resamples <= 0
    ):
        raise BreadthError("bootstrap resamples must be positive")
    row = _workload(state, workload)
    _require_fixed(row)
    for system in TIMING_SYSTEMS:
        reconstruct_system(state, workload, system, level=level)
    point = {
        system: reconstruct_system(state, workload, system, level=level)
        for system in TIMING_SYSTEMS
    }
    results = {}
    for system in TIMING_SYSTEMS[1:]:
        seed = int(hashlib.sha256(
            f"{row['trace_sha256']}:{workload}:{system}:{level}".encode()
        ).hexdigest()[:16], 16)
        rng = random.Random(seed)
        samples = []
        for _ in range(resamples):
            totals = {
                selected: _decimal(
                    row["fixed_seconds"][selected], "fixed seconds"
                )
                for selected in ("vanilla", system)
            }
            for phase_row in row["phases"].values():
                count = len(_window_rows(phase_row, level))
                indices = tuple(rng.randrange(count) for _ in range(count))
                for selected in totals:
                    value = sum((
                        _decimal(
                            phase_row["windows"][str(index)][selected],
                            "phase seconds per item",
                        )
                        for index in indices
                    ), Decimal(0)) / Decimal(count)
                    totals[selected] += Decimal(
                        phase_row["work_items"]
                    ) * value
            samples.append(totals["vanilla"] / totals[system])
        samples.sort()
        estimate = point["vanilla"] / point[system]
        low = samples[int(Decimal("0.025") * (resamples - 1))]
        high = samples[int(Decimal("0.975") * (resamples - 1))]
        relative = (high - low) / (Decimal(2) * estimate)
        results[system] = {
            "speedup": str(estimate),
            "ci_low": str(low),
            "ci_high": str(high),
            "relative_half_width": str(relative),
            "publishable": relative <= Decimal("0.05"),
            "resamples": resamples,
        }
    return {
        "level": level,
        "absolute_seconds": {
            system: str(value) for system, value in point.items()
        },
        "systems": results,
        "publishable": all(row["publishable"] for row in results.values()),
        "relative_half_width": str(max(
            _decimal(row["relative_half_width"], "relative half width")
            for row in results.values()
        )),
    }


def expand_level(state, workload, *, level):
    if level not in LEVELS[:-1]:
        raise BreadthError("only a nonfinal completed level may expand")
    row = _workload(state, workload)
    next_level = LEVELS[LEVELS.index(level) + 1]
    for phase_row in row["phases"].values():
        if phase_row["level"] != level or phase_row["status"] != "level_complete":
            raise BreadthError("every phase must complete before paired expansion")
        phase_row["level"] = next_level
        phase_row["status"] = "pending"
    return state


def bind_or_resume(root, identity, *, resume):
    root = Path(root).resolve()
    try:
        identity_path = root / "identity.json"
        existed = identity_path.exists()
        contract.bind_root(root, identity)
    except contract.ContractError as error:
        raise BreadthError(str(error)) from error
    if resume and not existed:
        raise BreadthError("resume requested but evidence identity is missing")
    if not resume and existed:
        raise BreadthError("evidence root exists; use --resume")
    return identity_path


def _checkpoint_records(root):
    records = []
    for path in sorted((Path(root) / "checkpoints").glob("*.json")):
        try:
            value = contract.load_json(path)
        except contract.ContractError:
            value = {"invalid_path": str(path)}
        records.append(value)
    return records


def write_checkpoint(root, state, *, boundary, outputs):
    if boundary not in {"functional", "window", "phase"}:
        raise BreadthError("checkpoint boundary must be functional, window, or phase")
    if not isinstance(outputs, dict) or not outputs:
        raise BreadthError("checkpoint named outputs are required")
    if not contract.verify_named_hashes(outputs):
        raise BreadthError("checkpoint named output hashes are invalid")
    existing = _checkpoint_records(root)
    sequences = [
        row.get("sequence") for row in existing
        if isinstance(row.get("sequence"), int)
        and not isinstance(row.get("sequence"), bool)
    ]
    sequence = max(sequences, default=-1) + 1
    state_sha256 = hashlib.sha256(contract.canonical_json(state)).hexdigest()
    record = {
        "schema": 1,
        "sequence": sequence,
        "identity_sha256": state.get("identity_sha256"),
        "boundary": boundary,
        "outputs": outputs,
        "state_sha256": state_sha256,
        "state": state,
    }
    path = Path(root) / "checkpoints" / f"{sequence:08d}-{boundary}.json"
    contract.atomic_write_json(path, record)
    return record


def _state_latency_matches(state, cxl_link_delay=None):
    if not isinstance(state, dict):
        return False
    try:
        label, expected_ticks = _canonical_latency(state.get("cxl_link_delay"))
        if state.get("cxl_link_delay_ticks") != expected_ticks:
            return False
        if cxl_link_delay is not None:
            selected, selected_ticks = _canonical_latency(cxl_link_delay)
            if label != selected or expected_ticks != selected_ticks:
                return False
    except BreadthError:
        return False
    return True


def select_resume(
    root, identity_digest, *, cxl_link_delay=None, correctness_policy=None,
):
    if not isinstance(identity_digest, str) or _SHA256.fullmatch(identity_digest) is None:
        raise BreadthError("identity SHA-256 is invalid")
    if correctness_policy is not None:
        correctness_policy = _correctness_policy(correctness_policy)
    valid = []
    rejected = []
    for record in _checkpoint_records(root):
        sequence = record.get("sequence")
        state = record.get("state")
        state_hash = (
            hashlib.sha256(contract.canonical_json(state)).hexdigest()
            if isinstance(state, dict) else None
        )
        bound_outputs = state.get("evidence_files", {}) if isinstance(state, dict) else {}
        okay = (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence >= 0
            and record.get("identity_sha256") == identity_digest
            and record.get("boundary") in {"functional", "window", "phase"}
            and contract.verify_named_hashes(record.get("outputs"))
            and isinstance(state, dict)
            and state.get("schema") == 1
            and _state_latency_matches(state, cxl_link_delay)
            and (
                correctness_policy is None
                or state.get("correctness_policy", "bit-exact")
                == correctness_policy
            )
            and state.get("identity_sha256") == identity_digest
            and record.get("state_sha256") == state_hash
            and (not bound_outputs or record.get("outputs") == bound_outputs)
            and (
                not bound_outputs
                or _verify_bound_evidence(bound_outputs, state=state)
            )
            and _valid_boundary_state(state, record.get("boundary"))
        )
        (valid if okay else rejected).append(record)
    selected = max(valid, key=lambda row: row["sequence"]) if valid else None
    return selected, tuple(rejected)


def _verify_bound_evidence(records, *, state=None):
    if not contract.verify_named_hashes(records):
        return False
    try:
        for record in records.values():
            evidence = _load_json(record["path"], "checkpoint action evidence")
            if (
                evidence.get("status") != "pass"
                or not contract.verify_named_hashes(evidence.get("outputs"))
            ):
                return False
            if "cxl_link_delay_ticks" in evidence and (
                state is None
                or evidence.get("cxl_link_delay") != state.get("cxl_link_delay")
                or evidence.get("cxl_link_delay_ticks")
                != state.get("cxl_link_delay_ticks")
            ):
                return False
            _boundary_records(evidence.get("boundaries"), "checkpoint")
    except (BreadthError, OSError, KeyError, TypeError):
        return False
    return True


def _valid_boundary_state(state, boundary):
    if boundary == "functional":
        return (
            state.get("status") == "timing_in_progress"
            and all(
                row.get("status") == "functional_pass"
                and functional_complete(row.get("functional"))
                for row in state.get("workloads", {}).values()
            )
        )
    if boundary == "window":
        return state.get("status") == "timing_in_progress" and any(
            set(samples) == set(TIMING_SYSTEMS)
            for row in state.get("workloads", {}).values()
            for phase in row.get("phases", {}).values()
            for samples in phase.get("windows", {}).values()
        )
    if boundary == "phase":
        return state.get("status") in {
            "timing_in_progress", "complete", "inconclusive"
        } and any(
            phase.get("status") == "level_complete"
            or row.get("status") in {"complete", "inconclusive"}
            for row in state.get("workloads", {}).values()
            for phase in row.get("phases", {}).values()
        )
    return False


def _render(value, action, *, extra=None):
    fields = {
        "workload": action.workload,
        "system": action.system or "",
        "phase": action.phase or "",
        "window_index": "" if action.window_index is None else str(action.window_index),
        "level": "" if action.level is None else str(action.level),
        "warmup_start": "" if action.warmup_start is None else str(action.warmup_start),
        "measure_start": "" if action.measure_start is None else str(action.measure_start),
        "measure_stop": "" if action.measure_stop is None else str(action.measure_stop),
        "cxl_link_delay": action.cxl_link_delay or "",
        "cxl_link_delay_ticks": (
            "" if action.cxl_link_delay_ticks is None
            else str(action.cxl_link_delay_ticks)
        ),
    }
    if extra is not None:
        if not isinstance(extra, dict) or any(
            not isinstance(name, str) or not isinstance(value, (str, int))
            for name, value in extra.items()
        ):
            raise BreadthError("action template replacements are invalid")
        fields.update(extra)
    result = str(value)
    for name, replacement in fields.items():
        result = result.replace("{{" + name + "}}", replacement)
    if "{{" in result or "}}" in result:
        raise BreadthError(f"unresolved action template: {result}")
    return result


class ManifestExecutor:
    """Run commands from a hash-bound prepared manifest.

    Each action writes one standardized evidence JSON object.  Existing valid
    evidence may be consumed after a crash before the next boundary; it is
    never silently overwritten.
    """

    def __init__(self, manifest, *, root, cxl_link_delay="1us"):
        self.manifest = manifest
        self.root = Path(root).resolve()
        self.cxl_link_delay, self.cxl_link_delay_ticks = _canonical_latency(
            cxl_link_delay
        )

    def _latency_bound_action(self, action):
        if action.cxl_link_delay not in {None, self.cxl_link_delay} or (
            action.cxl_link_delay_ticks
            not in {None, self.cxl_link_delay_ticks}
        ):
            raise BreadthError("action CXL latency differs")
        return dataclasses.replace(
            action,
            cxl_link_delay=self.cxl_link_delay,
            cxl_link_delay_ticks=self.cxl_link_delay_ticks,
        )

    @staticmethod
    def _validate_latency_template(action, specification):
        command = specification.get("command", [])
        evidence = str(specification.get("evidence", ""))
        if not isinstance(command, list) or any(
            not isinstance(item, (str, int)) for item in command
        ):
            raise BreadthError("prepared action command is invalid")
        serialized = json.dumps(command, sort_keys=True)
        if action.stage == "window":
            try:
                delay_index = command.index("--cxl-link-delay")
                delay_value = command[delay_index + 1]
            except (AttributeError, IndexError, ValueError) as error:
                raise BreadthError(
                    "prepared timing action CXL latency differs"
                ) from error
            if delay_value != "{{cxl_link_delay}}" or (
                "{{cxl_link_delay}}" not in evidence
            ):
                raise BreadthError("prepared timing action CXL latency differs")
            return
        if (
            "--cxl-link-delay" in command
            or "{{cxl_link_delay" in serialized
            or "{{cxl_link_delay" in evidence
            or any(label in Path(evidence).parts for label in latency.LABELS)
        ):
            raise BreadthError("prepared functional action is latency-specific")

    def _specification(self, action):
        try:
            workload = self.manifest["workloads"][action.workload]
            if action.stage == "reference":
                return workload["actions"]["reference"]
            if action.stage == "functional":
                return workload["actions"]["functional"][action.system]
            if action.stage == "window":
                return workload["actions"]["window"][action.phase][action.system]
        except (KeyError, TypeError) as error:
            raise BreadthError(
                f"prepared action is missing: {action.stage}:{action.workload}:"
                f"{action.system or '-'}:{action.phase or '-'}"
            ) from error
        raise BreadthError(f"unsupported collection action: {action.stage}")

    def __call__(self, action):
        action = self._latency_bound_action(action)
        specification = self._specification(action)
        if not isinstance(specification, dict):
            raise BreadthError("prepared action specification is invalid")
        self._validate_latency_template(action, specification)
        evidence_path = Path(_render(specification.get("evidence", ""), action))
        if not evidence_path.is_absolute():
            evidence_path = self.root / evidence_path
        evidence_path = evidence_path.resolve()
        command_value = specification.get("command", [])
        if not isinstance(command_value, list) or any(
            not isinstance(item, (str, int)) for item in command_value
        ):
            raise BreadthError("prepared action command is invalid")
        try:
            evidence_path.relative_to(self.root)
        except ValueError as error:
            if (
                action.stage == "window"
                or command_value
                or not evidence_path.is_file()
            ):
                raise BreadthError(
                    "action evidence path escapes the evidence root"
                ) from error
        command = [
            _render(item, action, extra={
                "prepared_manifest": str(
                    (self.root / "prepared/manifest.json").resolve()
                ),
                "evidence_path": str(evidence_path),
            })
            for item in command_value
        ]
        if evidence_path.is_file():
            try:
                return self._validated(action, evidence_path, command)
            except BreadthError:
                if not command:
                    raise
                preserved = self._unused_path(evidence_path, ".invalid")
                evidence_path.replace(preserved)
        if not command:
            raise BreadthError(f"action evidence is missing: {evidence_path}")
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        log = self._unused_path(
            evidence_path.with_suffix(evidence_path.suffix + ".driver.log"),
            ".retry",
        )
        with log.open("x", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=None,
            )
        if completed.returncode != 0:
            raise BreadthError(
                f"collection action exited {completed.returncode}; see {log}"
            )
        return self._validated(action, evidence_path, command)

    @staticmethod
    def _unused_path(path, suffix):
        path = Path(path)
        if not path.exists():
            return path
        index = 1
        while True:
            candidate = path.with_name(f"{path.name}{suffix}.{index}")
            if not candidate.exists():
                return candidate
            index += 1

    @staticmethod
    def _validated(action, evidence_path, command):
        evidence = _load_json(evidence_path, "action evidence")
        if evidence.get("status") != "pass":
            raise BreadthError("action evidence status is not pass")
        if command and evidence.get("command") != command:
            raise BreadthError("action evidence command differs")
        if action.stage == "window":
            expected_coordinate = {
                "phase": action.phase,
                "window_index": action.window_index,
                "level": action.level,
                "stratum": action.stratum,
                "warmup_start": action.warmup_start,
                "measure_start": action.measure_start,
                "measure_stop": action.measure_stop,
            }
            if evidence.get("coordinate") != expected_coordinate:
                raise BreadthError("window evidence coordinate differs")
        outputs = evidence.get("outputs")
        if not contract.verify_named_hashes(outputs):
            raise BreadthError("action named output evidence is invalid")
        return {
            **evidence,
            "evidence_output": {
                "path": str(evidence_path),
                "sha256": _sha256_file(evidence_path),
            },
        }


def _functional_record(evidence):
    outputs = evidence["outputs"]
    return {
        "status": "pass",
        "verification": evidence.get("verification"),
        "numeric_verification": evidence.get("numeric_verification"),
        "bit_exact": evidence.get("bit_exact") is True,
        "compared_words": evidence.get("compared_words"),
        "mismatched_words": evidence.get("mismatched_words"),
        "nonfinite_words": evidence.get("nonfinite_words"),
        "error_counters": evidence.get("error_counters"),
        "boundaries": evidence.get("boundaries"),
        "outputs": {
            name: record["sha256"] for name, record in outputs.items()
        },
        **{
            field: evidence.get(field)
            for field in (
                "issued_loads", "completed_loads", "drains",
                "phases",
                "issued_prefetches", "completed_prefetches",
                "issued_per_core", "completed_per_core",
                "expected_operations", "compared_operations",
                "expected_launches", "completed_launches", "funcsim_status",
            )
        },
    }


def _checkpoint_after_action(root, state, boundary, evidence):
    outputs = dict(sorted(state.get("evidence_files", {}).items()))
    return write_checkpoint(root, state, boundary=boundary, outputs=outputs)


def _bind_action_evidence(state, evidence):
    files = state.setdefault("evidence_files", {})
    record = evidence.get("evidence_output")
    if not isinstance(record, dict) or not contract.verify_named_hashes(
        {"action": record}
    ):
        raise BreadthError("action evidence binding is invalid")
    if record not in files.values():
        files[f"action_{len(files):08d}"] = record


def _validate_window_evidence(system, evidence, state):
    if (
        evidence.get("cxl_link_delay") != state.get("cxl_link_delay")
        or evidence.get("cxl_link_delay_ticks")
        != state.get("cxl_link_delay_ticks")
    ):
        raise BreadthError("timing evidence CXL latency differs")
    if (
        not _valid_numerical_evidence(
            evidence, state.get("correctness_policy", "bit-exact")
        )
        or evidence.get("threads") != 4
        or evidence.get("all_memory_cxl") is not True
        or evidence.get("allocated_on_cxl") is not True
    ):
        raise BreadthError("timing window correctness or 4-thread all-CXL gate failed")
    if _counter_errors(evidence):
        raise BreadthError("timing window mechanism error counters are nonzero")
    boundaries = _boundary_records(evidence.get("boundaries"), "timing window")
    exact_words = sum(record["count"] for record in boundaries.values())
    if evidence.get("compared_words") != exact_words:
        raise BreadthError("timing window compared-word cardinality differs")
    _validate_mechanism(system, evidence, timing_window=(system == "m2ndp"))
    if _decimal(evidence.get("fixed_seconds"), "fixed seconds") <= 0:
        raise BreadthError("timing fixed cost must be positive")
    if _decimal(evidence.get("seconds_per_item"), "seconds per item") <= 0:
        raise BreadthError("timing seconds per item must be positive")


def collect(state, *, root, executor):
    """Drive the state machine to a terminal result using an action executor."""
    root = Path(root).resolve()
    while state.get("status") not in contract.TERMINAL:
        action = next_action(state)
        if action is not None:
            evidence = executor(action)
            _bind_action_evidence(state, evidence)
            if action.stage == "reference":
                record_reference(state, action.workload, evidence["boundaries"])
            elif action.stage == "functional":
                record_functional(
                    state, action.workload, action.system,
                    _functional_record(evidence),
                )
                if all(
                    row.get("status") == "functional_pass"
                    for row in state["workloads"].values()
                ):
                    begin_timing(state)
                    _checkpoint_after_action(root, state, "functional", evidence)
            elif action.stage == "window":
                _validate_window_evidence(action.system, evidence, state)
                workload_row = _workload(state, action.workload)
                fixed = evidence.get("fixed_seconds")
                if action.system not in workload_row["fixed_seconds"]:
                    record_fixed(state, action.workload, action.system, fixed)
                elif _decimal(
                    workload_row["fixed_seconds"][action.system], "fixed seconds"
                ) != _decimal(fixed, "fixed seconds"):
                    raise BreadthError("fixed cost changed across paired windows")
                record_window(
                    state, action.workload, action.phase,
                    action.window_index, action.system,
                    evidence.get("seconds_per_item"),
                )
                phase_row = workload_row["phases"][action.phase]
                coordinate = phase_row["windows"][str(action.window_index)]
                if set(coordinate) == set(TIMING_SYSTEMS):
                    _checkpoint_after_action(root, state, "window", evidence)
                if phase_row["status"] == "level_complete":
                    _checkpoint_after_action(root, state, "phase", evidence)
            else:
                raise BreadthError(f"unknown action stage: {action.stage}")
            contract.atomic_write_json(root / "state.json", state)
            continue

        if state.get("status") == "planned":
            raise BreadthError("functional collection stopped before complete PASS")
        changed = False
        for workload in state["workload_order"]:
            row = _workload(state, workload)
            if row["status"] in {"complete", "inconclusive"}:
                continue
            levels = {phase["level"] for phase in row["phases"].values()}
            if len(levels) != 1 or any(
                phase["status"] != "level_complete"
                for phase in row["phases"].values()
            ):
                raise BreadthError("paired phase timing stopped before a complete level")
            level = levels.pop()
            result = bootstrap_workload(state, workload, level=level)
            decision = finish_timing(result, level=level)
            state["results"][workload] = decision
            if decision["status"] == "expand":
                expand_level(state, workload, level=level)
            else:
                row["status"] = decision["status"]
            outputs = state.get("evidence_files")
            if not contract.verify_named_hashes(outputs):
                raise BreadthError("phase checkpoint has no valid evidence output")
            write_checkpoint(root, state, boundary="phase", outputs=outputs)
            changed = True
        if any(
            _workload(state, workload)["status"] not in {"complete", "inconclusive"}
            for workload in state["workload_order"]
        ):
            if not changed:
                raise BreadthError("timing state made no progress")
            contract.atomic_write_json(root / "state.json", state)
            continue
        target = (
            "inconclusive"
            if any(
                _workload(state, workload)["status"] == "inconclusive"
                for workload in state["workload_order"]
            )
            else "complete"
        )
        try:
            transitioned = contract.transition(state, target)
        except contract.ContractError as error:
            raise BreadthError(str(error)) from error
        state.clear()
        state.update(transitioned)
        contract.atomic_write_json(root / "state.json", state)
        contract.atomic_write_json(root / f"{target}.json", state)
    return state


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--qualification", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--cxl-link-delay", choices=latency.LABELS, default="1us"
    )
    parser.add_argument(
        "--correctness-policy",
        choices=CORRECTNESS_POLICIES,
        default="bit-exact",
    )
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BreadthError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise BreadthError(f"{label} must be a JSON object")
    return value


def _g20_graph_sha256(inputs):
    graphs = inputs.get("graphs")
    if not isinstance(graphs, list):
        raise BreadthError("frozen inputs do not contain graph records")
    matches = [
        row for row in graphs
        if isinstance(row, dict) and row.get("scale") == 20
    ]
    if len(matches) != 1:
        raise BreadthError("frozen inputs must contain exactly one g20 graph")
    digest = matches[0].get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise BreadthError("g20 graph SHA-256 is invalid")
    try:
        workload_digest = inputs["workloads"]["pr_spmv"]["input_sha256"]
    except (KeyError, TypeError) as error:
        raise BreadthError("PR workload g20 graph identity is missing") from error
    if workload_digest != digest:
        raise BreadthError("PR workload and scaling g20 graph identities differ")
    return digest


def _config_identity_sha256(
    prepared_config_sha256, cxl_link_delay, cxl_link_delay_ticks,
    correctness_policy, *, qualification_sha256=None,
):
    correctness_policy = _correctness_policy(correctness_policy)
    config = {
        "prepared_config_sha256": prepared_config_sha256,
        "cxl_link_delay": cxl_link_delay,
        "cxl_link_delay_ticks": cxl_link_delay_ticks,
        "correctness_policy": correctness_policy,
    }
    if qualification_sha256 is not None:
        if (
            not isinstance(qualification_sha256, str)
            or _SHA256.fullmatch(qualification_sha256) is None
        ):
            raise BreadthError("qualification SHA-256 is invalid")
        config["qualification_sha256"] = qualification_sha256
    return hashlib.sha256(contract.canonical_json(config)).hexdigest()


def validate_qualification(path, calibration_sha256):
    """Validate the formal g12 gate authorizing a schema-2 calibration."""

    path = Path(path).resolve()
    value = _load_json(path, "g12 qualification")
    if (
        value.get("schema") != 1
        or value.get("status") != "passed"
        or value.get("profile") != "pr-offload-4thread-1us"
    ):
        raise BreadthError("qualification is not a formal PASS")
    identity = value.get("identity")
    if (
        not isinstance(identity, dict)
        or identity.get("calibration_sha256") != calibration_sha256
    ):
        raise BreadthError("qualification calibration differs")
    primary = value.get("primary")
    replay = value.get("replay")
    expected_keys = tuple(
        f"g12:{system}" for system in offload.contract.PRIMARY_SYSTEMS
    )
    if (
        not isinstance(primary, dict)
        or not isinstance(replay, dict)
        or set(primary) != set(expected_keys)
        or set(replay) != set(expected_keys)
    ):
        raise BreadthError("qualification point set differs")
    ordered_primary = {key: primary[key] for key in expected_keys}
    ordered_replay = {key: replay[key] for key in expected_keys}
    try:
        expected_gate = offload.qualification_gate(ordered_primary)
    except (offload.OffloadError, KeyError, TypeError, ValueError) as error:
        raise BreadthError(
            f"qualification primary evidence differs: {error}"
        ) from error
    if (
        expected_gate.get("status") != "passed"
        or expected_gate.get("offenders")
        or value.get("performance_gate") != expected_gate
    ):
        raise BreadthError("qualification performance gate differs")
    try:
        offload.validate_replay(ordered_primary, ordered_replay)
    except (offload.OffloadError, KeyError, TypeError, ValueError) as error:
        raise BreadthError(f"qualification replay differs: {error}") from error
    if primary != replay:
        raise BreadthError("qualification replay differs from primary")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _validate_formal_calibration_schema2(path, value):
    """Validate the schema-2 AMU/CIRA interface consumed by formal builds."""

    try:
        from scripts import build_gapbs_matched_pr_spmv_variants as variants
    except ImportError:
        import build_gapbs_matched_pr_spmv_variants as variants
    try:
        read_entries = variants.resolve_amu_row_window(path)
        cira_policy = variants.resolve_cira_build_policy(
            path, "pgo-selected"
        )
        formal = value["amu"]["formal_profile"]
        near_amu = value["near_data_pr"]["amu"]["parameters"]
        near_cira = value["near_data_pr"]["cira"]
    except (
        KeyError, TypeError, variants.VariantEvidenceError
    ) as error:
        raise BreadthError(
            f"formal schema-2 calibration interface differs: {error}"
        ) from error
    positive = (
        "id_batch_entries", "pending_entries_per_state_machine", "spm_bytes"
    )
    nonnegative = (
        "completion_cycles", "id_refill_cycles", "metadata_cycles"
    )
    if any(
        not isinstance(formal.get(name), int)
        or isinstance(formal.get(name), bool)
        or formal[name] <= 0
        for name in positive
    ) or any(
        not isinstance(formal.get(name), int)
        or isinstance(formal.get(name), bool)
        or formal[name] < 0
        for name in nonnegative
    ):
        raise BreadthError("formal schema-2 AMU profile is invalid")
    if (
        read_entries
        != formal["id_batch_entries"]
        * formal["pending_entries_per_state_machine"]
        or near_amu.get("read_entries") != read_entries
        or near_amu.get("id_batch_entries") != formal["id_batch_entries"]
        or near_amu.get("pending_entries_per_state_machine")
        != formal["pending_entries_per_state_machine"]
        or near_amu.get("spm_bytes") != formal["spm_bytes"]
        or near_cira.get("selected_source_row") != cira_policy["source_row"]
    ):
        raise BreadthError("formal schema-2 calibration parameters differ")
    return value


def _validate_calibration(options):
    calibration = _load_json(options.calibration, "calibration")
    if (
        calibration.get("passed") is True
        or calibration.get("status") in {"pass", "accepted", "complete"}
    ):
        return calibration, None
    if calibration.get("schema") != 2:
        raise BreadthError("calibration manifest is not accepted")
    qualification = getattr(options, "qualification", None)
    if qualification is None:
        raise BreadthError(
            "qualification is required for schema-2 calibration"
        )
    _validate_formal_calibration_schema2(Path(options.calibration), calibration)
    record = validate_qualification(
        qualification, _sha256_file(options.calibration)
    )
    return calibration, record


def _preflight_identity_unchecked(options):
    inputs = _load_json(options.inputs, "frozen inputs")
    if inputs.get("schema") != 1 or inputs.get("status") != "accepted":
        raise BreadthError("frozen inputs are not accepted schema 1")
    _calibration, qualification = _validate_calibration(options)
    # A prepared trace suite is created by build_matched_breadth_workloads.py.
    # Refuse to invent it here because source, binary, and ROI hashes are part
    # of the formal experiment identity.
    prepared = Path(options.root).resolve() / "prepared/manifest.json"
    if not prepared.is_file():
        raise BreadthError(
            "failed_input: prepared formal breadth manifest is missing; "
            "run the exact frozen-source workload builder"
        )
    manifest = _load_json(prepared, "prepared breadth manifest")
    if manifest.get("schema") != 1 or manifest.get("status") != "verified":
        raise BreadthError("prepared breadth manifest is not verified schema 1")
    specifications = manifest.get("workloads")
    if set(specifications or {}) != set(WORKLOADS):
        raise BreadthError("prepared formal workload set differs")
    if (
        manifest.get("threads") != 4
        or manifest.get("all_memory_cxl") is not True
        or tuple(manifest.get("functional_systems", ())) != FUNCTIONAL_SYSTEMS
        or tuple(manifest.get("timing_systems", ())) != TIMING_SYSTEMS
    ):
        raise BreadthError(
            "prepared experiment is not four-thread and all-CXL"
        )
    selected_label, selected_ticks = _canonical_latency(options.cxl_link_delay)
    fixed_label = manifest.get("cxl_link_delay")
    fixed_ticks = manifest.get("cxl_link_delay_ticks")
    if (
        fixed_label is not None and fixed_label != selected_label
    ) or (
        fixed_ticks is not None and fixed_ticks != selected_ticks
    ):
        raise BreadthError("prepared experiment CXL latency differs")
    local_code = {
        path.name: {"path": str(path), "sha256": _sha256_file(path)}
        for path in (
            Path(__file__).resolve(),
            REPO / "scripts/cross_system_contract.py",
            REPO / "scripts/stratified_timing.py",
            REPO / "scripts/run_matched_breadth_gem5.py",
            REPO / "scripts/m2ndp_workload_trace.py",
        )
    }
    prepared_code = manifest.get("code_files")
    prepared_config = manifest.get("config_files")
    code_hash = hashlib.sha256(contract.canonical_json({
        "local": _aggregate_files(local_code, "local code"),
        "prepared": _aggregate_files(prepared_code, "code"),
    })).hexdigest()
    prepared_config_hash = _aggregate_files(prepared_config, "configuration")
    config_hash = _config_identity_sha256(
        prepared_config_hash,
        selected_label,
        selected_ticks,
        options.correctness_policy,
        qualification_sha256=(
            qualification["sha256"] if qualification is not None else None
        ),
    )
    identity = contract.ExperimentIdentity(
        code_sha256=code_hash,
        input_manifest_sha256=_sha256_file(options.inputs),
        calibration_manifest_sha256=_sha256_file(options.calibration),
        trace_sha256=_sha256_file(prepared),
        config_sha256=config_hash,
    )
    return identity, specifications, _g20_graph_sha256(inputs)


def _preflight_identity(options):
    try:
        return _preflight_identity_unchecked(options)
    except (BreadthError, contract.ContractError, OSError) as error:
        raise BreadthInputError(str(error)) from error


def main(argv=None):
    options = parse_args(argv)
    root = Path(options.root).resolve()
    try:
        identity, specifications, g20_graph_sha256 = _preflight_identity(options)
        manifest = _load_json(root / "prepared/manifest.json", "prepared breadth manifest")
        bind_or_resume(root, identity, resume=options.resume)
        state_path = root / "state.json"
        if options.resume:
            selected, _ = select_resume(
                root, identity.digest(), cxl_link_delay=options.cxl_link_delay,
                correctness_policy=options.correctness_policy,
            )
            if selected is None:
                raise BreadthError("resume has no valid boundary checkpoint")
            state = selected["state"]
        else:
            state = new_state(
                identity,
                specifications,
                g20_graph_sha256=g20_graph_sha256,
                cxl_link_delay=options.cxl_link_delay,
                correctness_policy=options.correctness_policy,
            )
        contract.atomic_write_json(state_path, state)
        collect(
            state,
            root=root,
            executor=ManifestExecutor(
                manifest, root=root, cxl_link_delay=options.cxl_link_delay
            ),
        )
        if state.get("status") == "complete":
            print(f"BREADTH_COMPLETE workloads={len(WORKLOADS)} manifest={root / 'complete.json'}")
        else:
            print(
                f"BREADTH_INCONCLUSIVE workloads={len(WORKLOADS)} "
                f"manifest={root / 'inconclusive.json'}"
            )
        return 0
    except (BreadthError, OSError, contract.ContractError) as error:
        root.mkdir(parents=True, exist_ok=True)
        status = "failed_input" if isinstance(error, BreadthInputError) else "failed"
        contract.atomic_write_json(
            root / f"{status}.json",
            {"schema": 1, "status": status, "error": str(error)},
        )
        print(f"BREADTH_{status.upper()} error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
