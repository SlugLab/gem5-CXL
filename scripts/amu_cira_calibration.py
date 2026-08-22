#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Immutable source evidence for the AMU and CIRA calibration models."""

import csv
import hashlib
import itertools
import math
import re
from pathlib import Path


AMU_PDF_SHA256 = (
    "cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f"
)
CIRA_CSV_SHA256 = (
    "4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184"
)
CIRA_SPATTER_CSV_SHA256 = (
    "5e813083909be5d5c1a766ed0646268b426378d365989932bc57b3d8dd52429d"
)

AMU_TABLE4 = {
    "gups": {
        "0.1": {"baseline": 1.00, "amu": 0.96},
        "0.2": {"baseline": 1.38, "amu": 0.96},
        "0.5": {"baseline": 2.54, "amu": 0.97},
        "1": {"baseline": 4.40, "amu": 0.98},
        "2": {"baseline": 8.21, "amu": 1.00},
        "5": {"baseline": 19.83, "amu": 1.03},
    },
    "hj": {
        "0.1": {"baseline": 1.00, "amu": 2.69},
        "0.2": {"baseline": 1.41, "amu": 2.67},
        "0.5": {"baseline": 2.61, "amu": 2.68},
        "1": {"baseline": 4.59, "amu": 2.71},
        "2": {"baseline": 8.61, "amu": 2.79},
        "5": {"baseline": 20.70, "amu": 3.08},
    },
    "stream": {
        "0.1": {"baseline": 1.00, "amu": 1.64},
        "0.2": {"baseline": 1.28, "amu": 1.67},
        "0.5": {"baseline": 2.28, "amu": 1.74},
        "1": {"baseline": 4.00, "amu": 1.87},
        "2": {"baseline": 7.63, "amu": 2.18},
        "5": {"baseline": 18.66, "amu": 3.33},
    },
}

_CIRA_REQUIRED_MODES = ("baseline", "A", "B", "C", "ABC")
_CIRA_VERIFIED_MODES = ("baseline", "A", "B", "C")

AMU_SEARCH_SPACE = {
    "metadata_cycles": (0, 2, 4, 6, 8, 10),
    "id_refill_cycles": (0, 2, 4, 6, 8, 10),
    "completion_cycles": (0, 2, 4, 6, 8, 10),
}

AMU_ARCHITECTURE_DEFAULTS = {
    "metadata_cycles": 10,
    "id_refill_cycles": 0,
    "completion_cycles": 0,
}

NEAR_DATA_PR_AMU_ASSUMPTIONS = {
    "descriptor_entries": 32,
    "read_entries": 1024,
    "fp_add_cycles": 1,
    "fp_mul_cycles": 1,
    "fp_div_cycles": 4,
}
NEAR_DATA_PR_CIRA_ASSUMPTIONS = {
    "descriptor_entries": 16,
    "csr_read_entries": 256,
    "coherent_entries": 256,
    "fp_add_cycles": 1,
    "fp_mul_cycles": 1,
    "fp_div_cycles": 4,
    "reconfiguration_latency_ns": 100,
    "policy_base_cycles": 1000,
}


class CalibrationError(RuntimeError):
    """The selected calibration evidence is missing or inconsistent."""


def sha256_file(path):
    path = Path(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CalibrationError(f"cannot read calibration source {path}") from error
    return digest.hexdigest()


def require_hash(path, expected, label):
    actual = sha256_file(path)
    if actual != expected:
        raise CalibrationError(
            f"{label} SHA-256 {actual} does not match approved {expected}"
        )
    return actual


def load_amu_source(path):
    path = Path(path).resolve()
    digest = require_hash(path, AMU_PDF_SHA256, "AMU PDF")
    return {
        "path": str(path),
        "sha256": digest,
        "publication": {
            "title": (
                "Asynchronous Memory Access Unit: Exploiting Massive "
                "Parallelism for Far Memory Access"
            ),
            "doi": "10.1145/3663479",
            "venue": "ACM TACO 21(3), Article 55",
            "date": "2024-09",
        },
        "direct": {
            "source_locations": ["Table 2", "Sections 3-4", "Section 6.1"],
            "clock_ghz": 3,
            "issue_width": 6,
            "rob_entries": 512,
            "physical_registers": 512,
            "lsq_entries": 192,
            "l1_bytes": 32 * 1024,
            "l1_associativity": 16,
            "l1_mshrs": 48,
            "l1_cycles": 4,
            "l2_bytes": 256 * 1024,
            "l2_associativity": 8,
            "l2_mshrs": 48,
            "l2_cycles": 10,
            "spm_bytes": 64 * 1024,
            "pending_entries": 32,
            "id_bits": 16,
            "id_vector_bits": 512,
            "id_batch_entries": 32,
            "latency_us": [0.1, 0.2, 0.5, 1, 2, 5],
        },
        "validation": {
            "source_locations": ["Abstract", "Figure 9", "Table 4"],
            "mean_speedup_1us": 2.42,
            "gups_speedup_5us": 26.86,
            "gups_5us_min_mlp": 130,
            "table4": AMU_TABLE4,
        },
        "classification": {
            "processor_and_cache_parameters": "direct",
            "spm_and_queue_parameters": "direct",
            "mean_speedup_1us": "validation",
            "gups_speedup_5us": "validation",
            "table4": "validation",
        },
        "limitations": [
            "The paper evaluates a single RISC-V core, not formal x86.",
            "The paper does not evaluate PageRank or GAPBS pr_spmv.",
            "Figure-only values are not converted into invented data points.",
        ],
    }


def _integer(row, field, context):
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError(f"{context}: invalid integer {field}") from error


def _number(row, field, context):
    try:
        return float(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError(f"{context}: invalid number {field}") from error


def _optional_integer(row, field, context):
    value = row.get(field, "")
    if value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise CalibrationError(f"{context}: invalid integer {field}") from error


def _parse_raw_times(value, context):
    try:
        values = [float(item) for item in value.split(";") if item]
    except (AttributeError, ValueError) as error:
        raise CalibrationError(f"{context}: invalid RawTimes_ms") from error
    if len(values) != 10:
        raise CalibrationError(
            f"{context}: expected 10 RawTimes_ms values, got {len(values)}"
        )
    return values


def _parse_cira_row(row, context):
    trials = _integer(row, "Trials", context)
    if trials != 10:
        raise CalibrationError(f"{context}: expected 10 trials, got {trials}")
    raw_times = _parse_raw_times(row.get("RawTimes_ms"), context)
    return {
        "mode": row.get("Mode", ""),
        "label": row.get("Label", ""),
        "binary": row.get("Binary", ""),
        "selected_from": row.get("SelectedFrom", ""),
        "fallback": row.get("Fallback", ""),
        "verification": row.get("Verification", ""),
        "return_code": _optional_integer(row, "ReturnCode", context),
        "trials": trials,
        "mean_time_ms": _number(row, "MeanTime_ms", context),
        "stddev_time_ms": _number(row, "StdDevTime_ms", context),
        "ci95_time_ms": _number(row, "CI95Time_ms", context),
        "ci95_time_low_ms": _number(row, "CI95TimeLow_ms", context),
        "ci95_time_high_ms": _number(row, "CI95TimeHigh_ms", context),
        "speedup_mean": _number(row, "SpeedupMean", context),
        "speedup_ci95_low": _number(row, "SpeedupCI95Low", context),
        "speedup_ci95_high": _number(row, "SpeedupCI95High", context),
        "raw_times_ms": raw_times,
        "log_file": row.get("LogFile", ""),
    }


def _geomean(values):
    if not values or any(value <= 0 for value in values):
        raise CalibrationError("geometric mean requires positive observations")
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def load_cira_source(path):
    path = Path(path).resolve()
    digest = require_hash(path, CIRA_CSV_SHA256, "CIRA CSV")
    by_workload = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for line_number, source_row in enumerate(
                csv.DictReader(stream), start=2
            ):
                workload = source_row.get("Workload", "")
                mode = source_row.get("Mode", "")
                context = f"{path}:{line_number} {workload}/{mode}"
                if not workload or mode not in _CIRA_REQUIRED_MODES:
                    raise CalibrationError(f"{context}: unexpected row identity")
                modes = by_workload.setdefault(workload, {})
                if mode in modes:
                    raise CalibrationError(f"{context}: duplicate mode")
                modes[mode] = _parse_cira_row(source_row, context)
    except OSError as error:
        raise CalibrationError(f"cannot read CIRA CSV {path}") from error

    for workload, modes in by_workload.items():
        missing = sorted(set(_CIRA_REQUIRED_MODES) - set(modes))
        if missing:
            raise CalibrationError(
                f"{workload}: missing CIRA modes {', '.join(missing)}"
            )

    verified = sorted(
        workload
        for workload, modes in by_workload.items()
        if all(
            modes[mode]["verification"] == "PASS"
            for mode in _CIRA_VERIFIED_MODES
        )
    )
    static_values = [by_workload[name]["A"]["speedup_mean"] for name in verified]
    pgo_values = [by_workload[name]["ABC"]["speedup_mean"] for name in verified]
    static_geomean = _geomean(static_values)
    pgo_geomean = _geomean(pgo_values)

    if "pr_spmv" not in verified:
        raise CalibrationError("verified pr_spmv calibration row is missing")
    primary_static = by_workload["pr_spmv"]["A"]["speedup_mean"]
    primary_pgo = by_workload["pr_spmv"]["ABC"]["speedup_mean"]

    return {
        "path": str(path),
        "sha256": digest,
        "rows": by_workload,
        "verified_workloads": verified,
        "excluded_workloads": sorted(set(by_workload) - set(verified)),
        "primary": {
            "workload": "pr_spmv",
            "static_speedup": primary_static,
            "pgo_selected_speedup": primary_pgo,
            "pgo_over_static": primary_pgo / primary_static,
            "selected_source_mode": by_workload["pr_spmv"]["ABC"][
                "selected_from"
            ],
        },
        "geomean": {
            "static": static_geomean,
            "pgo_selected": pgo_geomean,
            "pgo_over_static": pgo_geomean / static_geomean,
        },
        "classification": {
            "A": "static",
            "B": "candidate_2k",
            "C": "candidate_1k",
            "ABC": "offline_pgo_selected",
        },
        "limitations": [
            "ABC is a post-hoc best measured selector, not an online JIT.",
            "Rows failing Verification are excluded from fitting and plots.",
            "Original fallback and confidence-interval fields are preserved.",
        ],
    }


def build_near_data_pr_section(amu_source, cira_source):
    """Freeze executor resources and hardware-backed CIRA policy ranking."""
    direct = amu_source["direct"]
    amu_parameters = {
        "spm_bytes": direct["spm_bytes"],
        "pending_entries_per_state_machine": direct["pending_entries"],
        "id_batch_entries": direct["id_batch_entries"],
        **NEAR_DATA_PR_AMU_ASSUMPTIONS,
    }
    amu_sources = {
        "spm_bytes": "AMU paper direct",
        "pending_entries_per_state_machine": "AMU paper direct",
        "id_batch_entries": "AMU paper direct",
        "descriptor_entries": "derived from paper pending entries",
        "read_entries": "pending entries times ID batch entries",
        "fp_add_cycles": "explicit architecture assumption",
        "fp_mul_cycles": "explicit architecture assumption",
        "fp_div_cycles": "explicit architecture assumption",
    }
    if amu_parameters["read_entries"] != (
        direct["pending_entries"] * direct["id_batch_entries"]
    ):
        raise CalibrationError("AMU PR read entries are not paper-derived")

    rows = cira_source["rows"]["pr_spmv"]
    selected = cira_source["primary"]["selected_source_mode"]
    if selected not in {"A", "B", "C"}:
        raise CalibrationError("CIRA PR selected source row is invalid")
    selected_mean = rows[selected]["mean_time_ms"]
    candidates = {}
    for name in ("A", "B", "C"):
        row = rows[name]
        if row["verification"] != "PASS" or row["return_code"] != 0:
            raise CalibrationError(f"CIRA PR candidate {name} is not verified")
        candidates[name] = {
            "mean_time_ms": row["mean_time_ms"],
            "stddev_time_ms": row["stddev_time_ms"],
            "ci95_time_ms": row["ci95_time_ms"],
            "ci95_time_low_ms": row["ci95_time_low_ms"],
            "ci95_time_high_ms": row["ci95_time_high_ms"],
            "raw_times_ms": row["raw_times_ms"],
            "relative_cost_ppm": round(
                row["mean_time_ms"] / selected_mean * 1_000_000
            ),
        }
    if min(candidates, key=lambda name: candidates[name]["mean_time_ms"]) != selected:
        raise CalibrationError("CIRA selected source row differs from raw ranking")

    cira_sources = {
        name: "explicit architecture assumption"
        for name in NEAR_DATA_PR_CIRA_ASSUMPTIONS
    }
    cira_sources["policy_base_cycles"] = (
        "architecture charge scaled only by hardware policy ranking"
    )
    return {
        "formal_speedup_is_fit_target": False,
        "amu": {
            "fit_role": "architecture_and_cross_workload_validation",
            "parameters": amu_parameters,
            "parameter_sources": amu_sources,
            "limitations": list(amu_source["limitations"]),
        },
        "cira": {
            "fit_role": "pr_spmv_policy_ranking",
            "parameters": dict(NEAR_DATA_PR_CIRA_ASSUMPTIONS),
            "parameter_sources": cira_sources,
            "selected_source_row": selected,
            "candidates": candidates,
            "policy_multiplier_role": (
                "relative descriptor formation charge, not E2E replacement"
            ),
        },
    }


def load_cira_spatter_source(path):
    """Load the approved real-hardware Spatter policy-selection rows."""
    path = Path(path).resolve()
    digest = require_hash(
        path, CIRA_SPATTER_CSV_SHA256, "CIRA Spatter CSV"
    )
    required_workloads = ("amg", "lulesh", "nekbone", "pennant")
    rows = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for line_number, source in enumerate(csv.DictReader(stream), start=2):
                workload = source.get("Workload", "")
                mode = source.get("Mode", "")
                context = f"{path}:{line_number} {workload}/{mode}"
                if source.get("Suite") != "spatter":
                    raise CalibrationError(f"{context}: suite is not Spatter")
                if workload not in required_workloads:
                    raise CalibrationError(f"{context}: unexpected workload")
                if mode not in _CIRA_REQUIRED_MODES:
                    raise CalibrationError(f"{context}: unexpected mode")
                modes = rows.setdefault(workload, {})
                if mode in modes:
                    raise CalibrationError(f"{context}: duplicate mode")
                trials = _integer(source, "Trials", context)
                if trials != 10:
                    raise CalibrationError(
                        f"{context}: expected 10 trials, got {trials}"
                    )
                try:
                    raw = [
                        float(value)
                        for value in source.get("RawTrialMs", "").split(";")
                        if value
                    ]
                except ValueError as error:
                    raise CalibrationError(
                        f"{context}: invalid RawTrialMs"
                    ) from error
                if len(raw) != trials or any(
                    not math.isfinite(value) or value <= 0 for value in raw
                ):
                    raise CalibrationError(
                        f"{context}: RawTrialMs must contain 10 positive values"
                    )
                numeric = {}
                for field in (
                    "MeanRuntimeMs", "RuntimeStdMs",
                    "RuntimeCI95HalfWidthMs", "RuntimeCI95LowMs",
                    "RuntimeCI95HighMs", "SpeedupVsBaseline",
                    "SpeedupCI95Low", "SpeedupCI95High",
                ):
                    numeric[field] = _number(source, field, context)
                    if not math.isfinite(numeric[field]):
                        raise CalibrationError(f"{context}: invalid {field}")
                if numeric["MeanRuntimeMs"] <= 0 or numeric["SpeedupVsBaseline"] <= 0:
                    raise CalibrationError(f"{context}: timing value is nonpositive")
                modes[mode] = {
                    "mode": mode,
                    "label": source.get("Label", ""),
                    "binary": source.get("Binary", ""),
                    "selected_from": source.get("SelectedFrom", ""),
                    "trials": trials,
                    "timing_method": source.get("TimingMethod", ""),
                    "raw_trial_ms": raw,
                    **{
                        re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): value
                        for key, value in numeric.items()
                    },
                }
    except OSError as error:
        raise CalibrationError(f"cannot read CIRA Spatter CSV {path}") from error
    if tuple(sorted(rows)) != tuple(sorted(required_workloads)):
        raise CalibrationError("CIRA Spatter CSV workload set differs")
    for workload in required_workloads:
        missing = sorted(set(_CIRA_REQUIRED_MODES) - set(rows[workload]))
        if missing:
            raise CalibrationError(
                f"{workload}: missing Spatter modes {', '.join(missing)}"
            )
    return {
        "path": str(path),
        "sha256": digest,
        "rows": rows,
        "classification": "direct_cira_policy",
        "fit_source_speedup": False,
        "limitations": [
            "Hardware speedups select policy evidence only and are never fit targets.",
            "Only AMG gather and LULESH scatter structurally match breadth regions.",
        ],
    }


def _breadth_identity(value, label):
    fields = ("input_sha256", "source_sha256", "roi_sha256")
    if not isinstance(value, dict):
        raise CalibrationError(f"{label} identity is missing")
    result = {}
    for field in fields:
        digest = value.get(field)
        if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise CalibrationError(f"{label} {field} is invalid")
        result[field] = digest
    return result


def classify_breadth_cira_evidence(
    workload, *, trace_identity, hardware_identity=None, spatter=None,
    synthetic=False,
):
    """Classify policy evidence without ever fitting a source speedup."""
    trace = _breadth_identity(trace_identity, "trace")
    if workload == "mcf" and synthetic:
        raise CalibrationError(
            "synthetic MCF cannot be a 345 MB speedup target"
        )
    spatter_names = {
        "amg_gather": "amg",
        "lulesh_scatter": "lulesh",
    }
    if workload in spatter_names:
        if (
            not isinstance(spatter, dict)
            or spatter.get("sha256") != CIRA_SPATTER_CSV_SHA256
        ):
            raise CalibrationError("approved CIRA Spatter evidence is missing")
        source_name = spatter_names[workload]
        try:
            modes = spatter["rows"][source_name]
        except (KeyError, TypeError) as error:
            raise CalibrationError(
                f"CIRA Spatter {source_name} rows are missing"
            ) from error
        return {
            "workload": workload,
            "classification": "direct_cira_policy",
            "source": "hardware_spatter",
            "source_sha256": spatter["sha256"],
            "source_workload": source_name,
            "modes": modes,
            "trace_identity": trace,
            "fit_source_speedup": False,
        }
    if workload not in {"mcf", "npb_cg", "npb_mg"}:
        raise CalibrationError(f"unsupported breadth workload {workload}")
    if hardware_identity is None:
        mismatch = list(trace)
        hardware = None
    else:
        hardware = _breadth_identity(hardware_identity, "hardware")
        mismatch = [field for field in trace if trace[field] != hardware[field]]
    return {
        "workload": workload,
        "classification": (
            "component_costs_only" if mismatch else "direct_cira_policy"
        ),
        "source": "identity_matched_hardware" if not mismatch else "components",
        "trace_identity": trace,
        "hardware_identity": hardware,
        "mismatched_identity": mismatch,
        "fit_source_speedup": False,
    }


def _measurement_key(row):
    latency = float(row["latency_us"])
    latency_text = str(int(latency)) if latency.is_integer() else str(latency)
    return f"{row['workload']}@{latency_text}"


def _validated_measurements(measurements):
    validated = []
    seen = set()
    count_fields = (
        "metadata_accesses",
        "id_batch_refills",
        "completions",
    )
    number_fields = (
        "latency_us",
        "simulated_normalized_time",
        "normalizer_cycles",
        "target",
        "weight",
    )
    for index, source in enumerate(measurements):
        if not isinstance(source, dict):
            raise CalibrationError(f"measurement {index} is not an object")
        row = dict(source)
        if not row.get("workload"):
            raise CalibrationError(f"measurement {index} has no workload")
        for field in number_fields:
            try:
                row[field] = float(row[field])
            except (KeyError, TypeError, ValueError) as error:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                ) from error
            if not math.isfinite(row[field]):
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                )
        if row["latency_us"] <= 0:
            raise CalibrationError(f"measurement {index}: invalid latency_us")
        if row["simulated_normalized_time"] < 0:
            raise CalibrationError(
                f"measurement {index}: invalid simulated_normalized_time"
            )
        for field in ("normalizer_cycles", "target", "weight"):
            if row[field] <= 0:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                )
        for field in count_fields:
            try:
                numeric = float(row[field])
                value = int(numeric)
            except (KeyError, TypeError, ValueError) as error:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                ) from error
            if value < 0 or not math.isfinite(numeric) or value != numeric:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                )
            row[field] = value
        key = _measurement_key(row)
        if key in seen:
            raise CalibrationError(f"duplicate measurement {key}")
        seen.add(key)
        validated.append(row)
    if len(validated) < 2:
        raise CalibrationError("at least two AMU measurements are required")
    return validated


def predict_normalized_time(row, parameters):
    overhead_cycles = (
        row["metadata_accesses"] * parameters["metadata_cycles"]
        + row["id_batch_refills"] * parameters["id_refill_cycles"]
        + row["completions"] * parameters["completion_cycles"]
    )
    return row["simulated_normalized_time"] + (
        overhead_cycles / row["normalizer_cycles"]
    )


def analyze_amu_proxy_feasibility(measurements):
    """Prove whether nonnegative costs and the fixed MLP cap can hit targets."""
    rows = _validated_measurements(measurements)
    points = {}
    infeasible = []
    for index, row in enumerate(rows):
        numeric = {}
        for field in (
            "normalizer_ticks",
            "outstanding_integral",
            "occupancy_ticks",
            "average_mlp",
        ):
            try:
                numeric[field] = float(row[field])
            except (KeyError, TypeError, ValueError) as error:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                ) from error
            if not math.isfinite(numeric[field]) or numeric[field] <= 0:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                )
        limits = {}
        for field in ("peak_mlp", "max_mlp"):
            try:
                raw = float(row[field])
                limits[field] = int(raw)
            except (KeyError, TypeError, ValueError) as error:
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                ) from error
            if (
                not math.isfinite(raw)
                or limits[field] != raw
                or limits[field] <= 0
            ):
                raise CalibrationError(
                    f"measurement {index}: invalid {field}"
                )
        if limits["peak_mlp"] > limits["max_mlp"]:
            raise CalibrationError(
                f"measurement {index}: peak_mlp exceeds max_mlp"
            )
        derived_average = (
            numeric["outstanding_integral"] / numeric["occupancy_ticks"]
        )
        if not math.isclose(
            derived_average,
            numeric["average_mlp"],
            rel_tol=1e-9,
            abs_tol=5.1e-7,
        ):
            raise CalibrationError(
                f"measurement {index}: average_mlp differs from "
                "outstanding_integral / occupancy_ticks"
            )
        if numeric["average_mlp"] > limits["peak_mlp"] + 1e-9:
            raise CalibrationError(
                f"measurement {index}: average_mlp exceeds peak_mlp"
            )

        target_ticks = row["target"] * numeric["normalizer_ticks"]
        required_average_mlp = (
            numeric["outstanding_integral"] / target_ticks
        )
        mlp_capacity_floor = (
            numeric["outstanding_integral"]
            / (limits["max_mlp"] * numeric["normalizer_ticks"])
        )
        reasons = []
        if row["simulated_normalized_time"] > row["target"] + 1e-12:
            reasons.append("ZERO_COST_PROXY")
        if mlp_capacity_floor > row["target"] + 1e-12:
            reasons.append("MLP_CAPACITY")
        key = _measurement_key(row)
        if reasons:
            infeasible.append(key)
        points[key] = {
            "status": (
                "INFEASIBLE_NONNEGATIVE_COSTS" if reasons else "FEASIBLE"
            ),
            "target": row["target"],
            "zero_cost_proxy": row["simulated_normalized_time"],
            "normalizer_ticks": numeric["normalizer_ticks"],
            "outstanding_integral": numeric["outstanding_integral"],
            "occupancy_ticks": numeric["occupancy_ticks"],
            "observed_average_mlp": numeric["average_mlp"],
            "derived_average_mlp": derived_average,
            "peak_mlp": limits["peak_mlp"],
            "max_mlp": limits["max_mlp"],
            "required_average_mlp": required_average_mlp,
            "mlp_capacity_floor": mlp_capacity_floor,
            "reasons": reasons,
        }
    return {
        "status": (
            "INFEASIBLE_NONNEGATIVE_COSTS" if infeasible else "FEASIBLE"
        ),
        "proof": (
            "outstanding_integral / (max_mlp * normalizer_ticks)"
        ),
        "proof_scope": (
            "frozen zero-control request occupancy and fixed scheduler"
        ),
        "infeasible_points": sorted(infeasible),
        "points": points,
    }


def _residual_record(row, prediction):
    residual = prediction - row["target"]
    return {
        "target": row["target"],
        "prediction": prediction,
        "residual": residual,
        "relative_error": abs(residual) / row["target"],
    }


def fit_amu_control_costs(measurements, holdout):
    rows = _validated_measurements(measurements)
    try:
        holdout_workload = str(holdout["workload"])
        holdout_latency = float(holdout["latency_us"])
    except (KeyError, TypeError, ValueError) as error:
        raise CalibrationError("invalid AMU holdout identity") from error

    held = [
        row
        for row in rows
        if row["workload"] == holdout_workload
        and row["latency_us"] == holdout_latency
    ]
    if len(held) != 1:
        raise CalibrationError("AMU holdout must match exactly one measurement")
    training = [row for row in rows if row is not held[0]]

    candidates = []
    names = tuple(AMU_SEARCH_SPACE)
    for values in itertools.product(*(AMU_SEARCH_SPACE[name] for name in names)):
        parameters = dict(zip(names, values))
        error = math.fsum(
            row["weight"]
            * (predict_normalized_time(row, parameters) - row["target"]) ** 2
            for row in training
        )
        candidates.append((error, values, parameters))
    weighted_sse, _, selected = min(candidates)

    training_residuals = {
        _measurement_key(row): _residual_record(
            row, predict_normalized_time(row, selected)
        )
        for row in training
    }
    holdout_residuals = {
        _measurement_key(row): _residual_record(
            row, predict_normalized_time(row, selected)
        )
        for row in held
    }
    return {
        "objective": "normalized_time_weighted_sse",
        "search_space": {name: list(values) for name, values in AMU_SEARCH_SPACE.items()},
        "parameters": selected,
        "weighted_sse": weighted_sse,
        "training_points": sorted(training_residuals),
        "training_residuals": training_residuals,
        "holdout_points": sorted(holdout_residuals),
        "holdout_residuals": holdout_residuals,
    }


def synthetic_measurements_for_test():
    return [
        {
            "workload": "gups",
            "latency_us": 1.0,
            "simulated_normalized_time": 0.5,
            "normalizer_cycles": 100,
            "metadata_accesses": 10,
            "id_batch_refills": 0,
            "completions": 0,
            "target": 0.9,
            "weight": 1.0,
        },
        {
            "workload": "hj",
            "latency_us": 1.0,
            "simulated_normalized_time": 0.5,
            "normalizer_cycles": 100,
            "metadata_accesses": 0,
            "id_batch_refills": 10,
            "completions": 0,
            "target": 1.1,
            "weight": 1.0,
        },
        {
            "workload": "stream",
            "latency_us": 1.0,
            "simulated_normalized_time": 0.5,
            "normalizer_cycles": 100,
            "metadata_accesses": 0,
            "id_batch_refills": 0,
            "completions": 10,
            "target": 0.7,
            "weight": 1.0,
        },
        {
            "workload": "stream",
            "latency_us": 2.0,
            "simulated_normalized_time": 0.5,
            "normalizer_cycles": 100,
            "metadata_accesses": 5,
            "id_batch_refills": 5,
            "completions": 5,
            "target": 1.1,
            "weight": 1.0,
        },
    ]


def paper_measurements_for_test():
    counts = {
        "gups": (1000, 0, 0),
        "hj": (0, 1000, 0),
        "stream": (0, 0, 1000),
    }
    known = {
        "metadata_cycles": 4,
        "id_refill_cycles": 6,
        "completion_cycles": 2,
    }
    rows = []
    for workload, observations in AMU_TABLE4.items():
        metadata, refills, completions = counts[workload]
        overhead = (
            metadata * known["metadata_cycles"]
            + refills * known["id_refill_cycles"]
            + completions * known["completion_cycles"]
        ) / 100000
        for latency_text, targets in observations.items():
            latency = float(latency_text)
            checksum = f"{workload}-{latency_text}-checksum"
            simulated = targets["amu"] - overhead
            average_mlp = (
                131.0
                if workload == "gups" and latency == 5.0
                else 16.0
            )
            normalizer_ticks = 100000.0
            occupancy_ticks = simulated * normalizer_ticks
            rows.append(
                {
                    "workload": workload,
                    "latency_us": latency,
                    "simulated_normalized_time": simulated,
                    "normalizer_cycles": 100000,
                    "normalizer_ticks": normalizer_ticks,
                    "metadata_accesses": metadata,
                    "id_batch_refills": refills,
                    "completions": completions,
                    "target": targets["amu"],
                    "paper_baseline_normalized": targets["baseline"],
                    "weight": 1.0,
                    "outstanding_integral": average_mlp * occupancy_ticks,
                    "occupancy_ticks": occupancy_ticks,
                    "average_mlp": average_mlp,
                    "peak_mlp": 131 if average_mlp == 131.0 else 16,
                    "max_mlp": 256,
                    "baseline_checksum": checksum,
                    "amu_checksum": checksum,
                }
            )
    return rows
