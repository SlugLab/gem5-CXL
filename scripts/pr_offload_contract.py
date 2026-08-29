#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Shared contracts for formal PageRank near-data offload."""

import dataclasses
import re
from decimal import Decimal, InvalidOperation


PR_ROW_DESC_BYTES = 104
FORMAL_THREADS = 4
FORMAL_ITERATIONS = 20
FORMAL_PROFILE = "pr-offload-4thread-1us"
SCALES = (12, 14, 20)
PRIMARY_SYSTEMS = ("vanilla", "amu", "cira-few-shot", "m2ndp")
CIRA_ABLATIONS = (
    "cira-static", "cira-pgo", "cira-A", "cira-B", "cira-C",
)
MIN_SPEEDUP = Decimal("1.4")
MAX_SPEEDUP = Decimal("1.6")
CIRA_PHASES = (
    "formation", "sampling", "selection", "jit", "execution", "drain",
)
IDENTITY_HASH_FIELDS = (
    "source_sha256",
    "gem5_sha256",
    "libm5_sha256",
    "graph_set_sha256",
    "workload_binaries_sha256",
    "m2ndp_patches_sha256",
    "m2ndp_config_sha256",
    "calibration_sha256",
    "policy_sha256",
    "source_inputs_sha256",
    "selected_inputs_sha256",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class OffloadError(RuntimeError):
    """Formal PR offload evidence violates an immutable contract."""


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    scale: int
    system: str
    stage: str
    replica: str = "primary"

    def __post_init__(self):
        if self.scale not in SCALES:
            raise OffloadError(f"unsupported formal scale g{self.scale}")
        if self.system not in PRIMARY_SYSTEMS + CIRA_ABLATIONS:
            raise OffloadError(f"unsupported formal system {self.system}")
        expected_stage = (
            "ablation" if self.system in CIRA_ABLATIONS
            else "qualification" if self.scale == 12
            else "formal"
        )
        if self.stage != expected_stage:
            raise OffloadError("matrix stage differs from scale/system")
        if self.replica not in {"primary", "replay"}:
            raise OffloadError("unknown qualification replica")

    @property
    def key(self):
        return f"g{self.scale}:{self.system}"


def build_matrix():
    primary = tuple(
        MatrixEntry(
            scale,
            system,
            "qualification" if scale == 12 else "formal",
        )
        for scale in SCALES
        for system in PRIMARY_SYSTEMS
    )
    ablations = tuple(
        MatrixEntry(scale, system, "ablation")
        for scale in SCALES
        for system in CIRA_ABLATIONS
    )
    return primary, ablations


def validate_identity(identity):
    if not isinstance(identity, dict):
        raise OffloadError("campaign identity must be an object")
    expected = set(IDENTITY_HASH_FIELDS) | {"m2ndp_commit"}
    if set(identity) != expected:
        raise OffloadError("campaign identity fields differ")
    for field in IDENTITY_HASH_FIELDS:
        if not _SHA256.fullmatch(identity[field]):
            raise OffloadError(f"campaign identity {field} is invalid")
    if not _COMMIT.fullmatch(identity["m2ndp_commit"]):
        raise OffloadError("campaign identity m2ndp_commit is invalid")
    return dict(identity)


def require_resume_identity(saved, live):
    if validate_identity(saved) != validate_identity(live):
        raise OffloadError("campaign resume identity differs")


def _integer(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise OffloadError(f"{label} must be an exact integer")
    if value < 0 or (positive and value == 0):
        raise OffloadError(f"{label} is outside its valid range")
    return value


def _decimal(value, label):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OffloadError(f"{label} must be decimal") from error
    if not parsed.is_finite() or parsed <= 0:
        raise OffloadError(f"{label} must be positive and finite")
    return parsed


def performance_policy(system):
    if system == "m2ndp":
        return {
            "minimum": str(MIN_SPEEDUP),
            "maximum": None,
            "correctness": "bit-exact-funcsim-before-ndpsim",
        }
    if system == "amu" or system == "cira" or system.startswith("cira-"):
        return {
            "minimum": str(MIN_SPEEDUP),
            "maximum": str(MAX_SPEEDUP),
            "correctness": "bit-exact",
        }
    raise OffloadError(f"no performance policy for system {system}")


def performance_accepted(system, speedup):
    value = _decimal(speedup, f"{system} speedup")
    policy = performance_policy(system)
    minimum = Decimal(policy["minimum"])
    maximum = (
        None
        if policy["maximum"] is None
        else Decimal(policy["maximum"])
    )
    return value >= minimum and (maximum is None or value <= maximum)


def validate_point(point):
    if not isinstance(point, dict):
        raise OffloadError("point must be an object")
    if "speedup" in point:
        raise OffloadError("stored speedup is forbidden")
    scale = point.get("scale")
    system = point.get("system")
    if scale not in SCALES or system not in PRIMARY_SYSTEMS + CIRA_ABLATIONS:
        raise OffloadError("point scale/system is outside the formal matrix")
    expected = {
        "profile": FORMAL_PROFILE,
        "cxl_link_delay": "1us",
        "workers": FORMAL_THREADS,
        "iterations": FORMAL_ITERATIONS,
        "all_memory_cxl": True,
        "verification": "pass",
    }
    for field, value in expected.items():
        if point.get(field) != value:
            raise OffloadError(f"point {field} differs from formal contract")
    if not _SHA256.fullmatch(str(point.get("raw_sha256", ""))):
        raise OffloadError("point raw vector hash is invalid")
    completions = point.get("worker_completions")
    valid_completion_shape = (
        isinstance(completions, list)
        and len(completions) == FORMAL_THREADS
        and all(
            _integer(value, "worker completion", positive=True) > 0
            for value in completions
        )
    )
    balanced = valid_completion_shape and max(completions) - min(completions) <= 1
    charged_few_shot = (
        valid_completion_shape
        and system == "cira-few-shot"
        and sorted(completions) == [40, 40, 40, 43]
    )
    if not (balanced or charged_few_shot):
        raise OffloadError("point worker completions are not balanced")
    pending = point.get("pending")
    if not isinstance(pending, dict) or not pending:
        raise OffloadError("point pending-work evidence is missing")
    for name, value in pending.items():
        if _integer(value, f"pending {name}") != 0:
            raise OffloadError("point has pending work")

    if system == "m2ndp":
        funcsim = point.get("funcsim")
        if (
            not isinstance(funcsim, dict)
            or funcsim.get("status") != "pass"
            or funcsim.get("compared") != 1 << scale
            or funcsim.get("mismatched") != 0
        ):
            raise OffloadError("M2NDP FuncSim bit-exact evidence is invalid")
        completed = _integer(
            funcsim.get("completed_at_seq"), "FuncSim completion sequence"
        )
        started = _integer(
            point.get("ndpsim_started_at_seq"), "NDPSim start sequence"
        )
        if completed >= started:
            raise OffloadError("M2NDP FuncSim did not precede NDPSim")
        cycles = _integer(point.get("ndpsim_cycles"), "NDPSim cycles", positive=True)
        period = _decimal(
            point.get("ndpsim_core_period_seconds"), "NDPSim core period"
        )
        seconds = Decimal(cycles) * period
    else:
        ticks = _integer(point.get("sim_ticks"), "gem5 sim_ticks", positive=True)
        seconds = Decimal(ticks) / Decimal(10**12)

    if system.startswith("cira"):
        phases = point.get("phases")
        if not isinstance(phases, dict) or set(phases) != set(CIRA_PHASES):
            raise OffloadError("CIRA additive phase set differs")
        values = {
            name: _integer(phases[name], f"CIRA {name} nanoseconds")
            for name in CIRA_PHASES
        }
        phase_total_ns = _integer(
            point.get("phase_total_ns"), "CIRA phase total nanoseconds",
            positive=True,
        )
        if sum(values.values()) != phase_total_ns:
            raise OffloadError("CIRA phase sum differs from phase_total_ns")
        if system == "cira-few-shot" and any(
            values[name] <= 0 for name in ("sampling", "selection", "jit")
        ):
            raise OffloadError("CIRA few-shot runtime phases are uncharged")
        if system == "cira-pgo" and any(
            values[name] != 0 for name in ("sampling", "selection", "jit")
        ):
            raise OffloadError(
                "CIRA PGO offline policy work is charged inside the ROI"
            )
    normalized = dict(point)
    normalized["seconds"] = seconds
    return normalized


def validate_complete(complete):
    if not isinstance(complete, dict) or complete.get("schema") != 1:
        raise OffloadError("complete evidence schema differs")
    identity = validate_identity(complete.get("identity"))
    primary = complete.get("primary")
    ablations = complete.get("ablations")
    if not isinstance(primary, list) or len(primary) != 12:
        raise OffloadError("complete evidence must contain 12 primary points")
    if not isinstance(ablations, list) or len(ablations) != 15:
        raise OffloadError("complete evidence must contain 15 ablation points")
    normalized_primary = [validate_point(row) for row in primary]
    normalized_ablations = [validate_point(row) for row in ablations]
    expected_primary, expected_ablations = build_matrix()
    if [f"g{r['scale']}:{r['system']}" for r in normalized_primary] != [
        entry.key for entry in expected_primary
    ]:
        raise OffloadError("primary point order or membership differs")
    if [f"g{r['scale']}:{r['system']}" for r in normalized_ablations] != [
        entry.key for entry in expected_ablations
    ]:
        raise OffloadError("ablation point order or membership differs")
    by_scale = {
        scale: next(
            row for row in normalized_primary
            if row["scale"] == scale and row["system"] == "vanilla"
        )
        for scale in SCALES
    }
    for row in normalized_primary + normalized_ablations:
        if row["raw_sha256"] != by_scale[row["scale"]]["raw_sha256"]:
            raise OffloadError("raw vector differs within a graph scale")
    gate = []
    for row in normalized_primary:
        if row["system"] == "vanilla":
            continue
        speedup = by_scale[row["scale"]]["seconds"] / row["seconds"]
        policy = performance_policy(row["system"])
        gate.append({
            "scale": row["scale"], "system": row["system"],
            "speedup": speedup,
            **policy,
            "accepted": performance_accepted(row["system"], speedup),
        })
    if len(gate) != 9:
        raise OffloadError("formal performance gate must contain nine points")
    if not all(row["accepted"] for row in gate):
        raise OffloadError("formal performance gate has an offender")
    return {
        "schema": 1,
        "identity": identity,
        "primary": normalized_primary,
        "ablations": normalized_ablations,
        "performance_gate": gate,
    }


def static_partition(rows, workers, worker):
    """Return the half-open contiguous row range owned by one worker."""

    if (
        isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or isinstance(workers, bool)
        or not isinstance(workers, int)
        or workers <= 0
        or isinstance(worker, bool)
        or not isinstance(worker, int)
        or worker < 0
        or worker >= workers
    ):
        raise ValueError("invalid PR static partition")
    quotient, remainder = divmod(rows, workers)
    begin = worker * quotient + min(worker, remainder)
    end = begin + quotient + (1 if worker < remainder else 0)
    return begin, end
