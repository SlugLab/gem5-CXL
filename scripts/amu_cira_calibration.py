#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Immutable source evidence for the AMU and CIRA calibration models."""

import csv
import hashlib
import math
from pathlib import Path


AMU_PDF_SHA256 = (
    "cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f"
)
CIRA_CSV_SHA256 = (
    "4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184"
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
