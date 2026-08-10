#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate the evidence-gated GAPBS g20 end-to-end paper table."""

import argparse
import csv
import dataclasses
import decimal
import hashlib
import io
import json
import math
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree

try:
    from scripts import calibrate_m2ndp_cxl as calibration
    from scripts import generate_gapbs_g20_e2e_figure as paper_figure
    from scripts import m2ndp_artifacts as artifacts
    from scripts import m2ndp_results as results
    from scripts import run_gapbs_matched_pr_spmv_variants as matched_runner
    from scripts import run_m2ndp_g20_pr_spmv as orchestrator
except ImportError:
    import calibrate_m2ndp_cxl as calibration
    import generate_gapbs_g20_e2e_figure as paper_figure
    import m2ndp_artifacts as artifacts
    import m2ndp_results as results
    import run_gapbs_matched_pr_spmv_variants as matched_runner
    import run_m2ndp_g20_pr_spmv as orchestrator


REPO = Path(__file__).resolve().parents[1]
PATCH = REPO / "util/m2ndp/patches"
G20_SHA256 = artifacts.EXPECTED_G20_SHA256
G20_WORDS = 1 << 20
TICKS_PER_SECOND = Decimal(10**12)
LATENCIES = ("200ns", "500ns", "1us", "2us")
WORKLOADS = ("bfs", "bc", "pr", "sssp")
DELAY_TICKS = {
    "200ns": "200000",
    "500ns": "500000",
    "1us": "1000000",
    "2us": "2000000",
}
SPEEDUP_TOLERANCE = Decimal("1e-12")
MAIN_FIELDS = (
    "system",
    "latency_seconds",
    "speedup_vs_vanilla_cxl",
    "correctness",
)


class TableEvidenceError(RuntimeError):
    """Publication evidence is absent, malformed, or inconsistent."""


@dataclasses.dataclass(frozen=True)
class MainRow:
    system: str
    latency_seconds: Decimal
    speedup: Decimal
    correctness: str


def load_json(path, context):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TableEvidenceError(
            f"{context} is missing or invalid: {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise TableEvidenceError(f"{context} must be a JSON object")
    return value


def read_csv(path, context):
    path = Path(path)
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise TableEvidenceError(
            f"{context} is missing or invalid: {path}: {error}"
        ) from error


def require_one_csv(path, context):
    rows = read_csv(path, context)
    if len(rows) != 1:
        raise TableEvidenceError(
            f"{context}: expected exactly one row, got {len(rows)}"
        )
    return rows[0]


def require_decimal(mapping, field, context, *, allow_zero=False):
    try:
        value = Decimal(str(mapping[field]))
    except (KeyError, decimal.InvalidOperation) as error:
        raise TableEvidenceError(
            f"{context}: invalid {field}"
        ) from error
    if not value.is_finite():
        raise TableEvidenceError(f"{context}: {field} must be finite")
    if value < 0 or (not allow_zero and value == 0):
        raise TableEvidenceError(
            f"{context}: {field} must be positive"
        )
    return value


def require_config_delay(path, *, expected="1000000"):
    path = Path(path)
    try:
        values = [
            line.split("=", 1)[1].strip()
            for line in path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.startswith("delay=")
        ]
    except OSError as error:
        raise TableEvidenceError(
            f"CXL config is missing: {path}: {error}"
        ) from error
    if values != [expected]:
        raise TableEvidenceError(
            f"{path}: delay values are {values!r}, expected [{expected!r}]"
        )


def _require_contract(mapping, expected, context):
    for field, value in expected.items():
        if mapping.get(field) != value:
            raise TableEvidenceError(
                f"{context}: {field}={mapping.get(field)!r}, "
                f"expected {value!r}"
            )


def require_all_m2ndp_stages_passed(status):
    stages = status.get("stages")
    if not isinstance(stages, dict):
        raise TableEvidenceError("M2NDP status has no stage map")
    for stage in orchestrator.STAGES:
        record = stages.get(stage)
        actual = record.get("status") if isinstance(record, dict) else None
        if actual != "passed":
            raise TableEvidenceError(
                f"M2NDP stage {stage} must be passed, got {actual!r}"
            )


def _hash_path(path):
    try:
        return orchestrator.hash_path(path)
    except artifacts.EvidenceError as error:
        raise TableEvidenceError(str(error)) from error


def verify_m2ndp_manifest(root, manifest):
    contract = manifest.get("contract")
    if not isinstance(contract, dict):
        raise TableEvidenceError("M2NDP manifest contract is missing")
    _require_contract(
        contract,
        {
            "benchmark": "pr_spmv",
            "graph_scale": 20,
            "page_rank_iterations": 20,
            "trials": 2,
            "measured_trial": 1,
            "cpu": "timing",
            "cores": 2,
            "all_memory_cxl": True,
            "cxl_link_delay": "1us",
            "smoke_test": False,
        },
        "M2NDP manifest contract",
    )
    if (
        manifest.get("m2ndp_upstream_commit")
        != artifacts.EXPECTED_M2NDP_COMMIT
    ):
        raise TableEvidenceError("M2NDP upstream commit is not pinned")
    recorded = manifest.get("artifact_sha256")
    if not isinstance(recorded, dict):
        raise TableEvidenceError("M2NDP artifact hash map is missing")
    paths = {
        "m2ndp_patch": PATCH,
        "trace": root / "trace",
        "m2ndp_config": root / "calibration/config",
        "reference_raw": root / "reference/scores.raw",
        "funcsim_dump": root / "funcsim/scores.u32",
        "calibration": root / "calibration/calibration.json",
        "gem5_log": root / "gem5/run/pr_spmv/cxl_vanilla/gem5.log",
        "funcsim_log": root / "logs/funcsim.log",
        "ndpsim_log": root / "logs/ndpsim.log",
        "summary": root / "summary.csv",
    }
    for name, path in paths.items():
        actual = _hash_path(path)
        if recorded.get(name) != actual:
            raise TableEvidenceError(
                f"M2NDP manifest hash mismatch for {name}"
            )
    return recorded


def validate_calibration(root, summary):
    value = load_json(
        root / "calibration/calibration.json", "calibration artifact"
    )
    if value.get("passed") is not True:
        raise TableEvidenceError("calibration must be passed")
    if value.get("request_bytes") != 64:
        raise TableEvidenceError("calibration request_bytes must be 64")
    target = require_decimal(value, "target_ns", "calibration")
    measured = require_decimal(value, "measured_ns", "calibration")
    residual = require_decimal(
        value, "residual_ns", "calibration", allow_zero=True
    )
    link_period = require_decimal(
        value, "link_period_ns", "calibration"
    )
    if residual != abs(measured - target):
        raise TableEvidenceError(
            "calibration residual does not match measured-target"
        )
    if residual > link_period:
        raise TableEvidenceError(
            "calibration residual exceeds one link clock"
        )
    try:
        config_sha256 = calibration.sha256_config_tree(
            root / "calibration/config"
        )
    except calibration.CalibrationError as error:
        raise TableEvidenceError(str(error)) from error
    if value.get("config_sha256") != config_sha256:
        raise TableEvidenceError("calibration config hash mismatch")
    if summary.get("m2ndp_config_sha256") != config_sha256:
        raise TableEvidenceError(
            "M2NDP summary config is outside calibration"
        )
    return value


def _rows_match_csv(recorded, row, context):
    for field, value in row.items():
        if field not in recorded:
            continue
        if str(recorded[field]) != str(value):
            raise TableEvidenceError(
                f"{context}: summary/evidence mismatch for {field}"
            )


def load_variant(variants_root, kind):
    run_root = variants_root / kind / "run"
    evidence = load_json(
        run_root / "evidence.json", f"{kind} evidence"
    )
    if evidence.get("graph_sha256") != G20_SHA256:
        raise TableEvidenceError(f"{kind} graph SHA-256 is not fixed g20")
    runs = evidence.get("runs")
    if not isinstance(runs, dict) or set(runs) != {kind}:
        raise TableEvidenceError(
            f"{kind} evidence must contain exactly one {kind} run"
        )
    record = runs[kind]
    if not isinstance(record, dict) or not isinstance(
        record.get("row"), dict
    ):
        raise TableEvidenceError(f"{kind} run evidence is invalid")
    row = record["row"]
    try:
        matched_runner.validate_row(row, kind, smoke_test=False)
    except matched_runner.VariantRunError as error:
        raise TableEvidenceError(str(error)) from error
    require_config_delay(Path(row["run_dir"]) / "config.ini")
    if record.get("config_delay_ticks") != 1_000_000:
        raise TableEvidenceError(f"{kind} evidence delay is not 1us")

    manifest_path = Path(evidence.get("variant_manifest", ""))
    if not manifest_path.is_file():
        raise TableEvidenceError(f"{kind} variant manifest is missing")
    if (
        artifacts.sha256_file(manifest_path)
        != evidence.get("variant_manifest_sha256")
    ):
        raise TableEvidenceError(f"{kind} variant manifest hash changed")
    try:
        manifest, by_kind = matched_runner.load_manifest(manifest_path)
    except matched_runner.VariantRunError as error:
        raise TableEvidenceError(str(error)) from error
    if evidence.get("fixed_source_sha256") != manifest.get(
        "fixed_source_sha256"
    ):
        raise TableEvidenceError(f"{kind} fixed source hash mismatch")
    variant = by_kind[kind]
    binary = Path(variant["binary"])
    binary_sha256 = artifacts.sha256_file(binary)
    if (
        binary_sha256 != variant.get("binary_sha256")
        or binary_sha256 != record.get("binary_sha256")
    ):
        raise TableEvidenceError(f"{kind} binary hash changed")
    reference = Path(record.get("reference_raw", ""))
    if reference.resolve() != Path(variant["reference_raw"]).resolve():
        raise TableEvidenceError(f"{kind} reference path mismatch")
    expected_size = G20_WORDS * 4
    if not reference.is_file() or reference.stat().st_size != expected_size:
        raise TableEvidenceError(
            f"{kind} raw float32 result size is invalid"
        )
    reference_sha256 = artifacts.sha256_file(reference)
    if reference_sha256 != record.get("reference_raw_sha256"):
        raise TableEvidenceError(
            f"{kind} raw float32 result hash changed"
        )
    summary_row = require_one_csv(
        run_root / "summary.csv", f"{kind} summary"
    )
    _rows_match_csv(summary_row, row, kind)
    return {
        "row": row,
        "reference": reference,
        "reference_sha256": reference_sha256,
        "binary_sha256": binary_sha256,
        "manifest_sha256": evidence["variant_manifest_sha256"],
    }


def validate_m2ndp_summary(root, summary, baseline, manifest_hashes):
    _require_contract(
        summary,
        {
            "benchmark": "pr_spmv",
            "graph_sha256": G20_SHA256,
            "iterations": "20",
            "trials": "2",
            "measured_trial": "1",
            "cores": "2",
            "all_memory_cxl": "True",
            "cxl_link_delay": "1us",
            "verification": "pass",
            "funcsim_strict": "pass",
            "funcsim_compared": str(G20_WORDS),
        },
        "M2NDP summary",
    )
    if require_decimal(
        summary, "gem5_sim_ticks", "M2NDP summary"
    ) != Decimal(baseline.sim_ticks):
        raise TableEvidenceError("M2NDP/baseline gem5 ticks mismatch")
    if summary.get("m2ndp_patch_sha256") != manifest_hashes.get(
        "m2ndp_patch"
    ):
        raise TableEvidenceError("M2NDP patch hash mismatch")
    if summary.get("trace_sha256") != manifest_hashes.get("trace"):
        raise TableEvidenceError("M2NDP trace hash mismatch")

    try:
        funcsim = results.parse_funcsim(
            (root / "logs/funcsim.log").read_text(
                encoding="utf-8", errors="replace"
            ),
            returncode=0,
            expected_count=G20_WORDS,
            dump_path=root / "funcsim/scores.u32",
            reference_path=root / "reference/scores.raw",
        )
        ndpsim = results.parse_ndpsim(
            (root / "logs/ndpsim.log").read_text(
                encoding="utf-8", errors="replace"
            ),
            returncode=0,
        )
    except (OSError, artifacts.EvidenceError) as error:
        raise TableEvidenceError(str(error)) from error
    for field, actual in (
        ("ndpsim_start_cycle", ndpsim.start_cycle),
        ("ndpsim_end_cycle", ndpsim.end_cycle),
        ("ndpsim_measured_cycles", ndpsim.measured_cycles),
    ):
        if require_decimal(summary, field, "M2NDP summary") != Decimal(
            actual
        ):
            raise TableEvidenceError(f"M2NDP {field} mismatch")
    if require_decimal(
        summary, "ndpsim_core_period_seconds", "M2NDP summary"
    ) != ndpsim.core_period_seconds:
        raise TableEvidenceError("M2NDP core period mismatch")
    if not funcsim.passed or funcsim.mismatched:
        raise TableEvidenceError("FuncSim strict validation failed")
    validate_calibration(root, summary)
    return ndpsim


def require_shared_raw_bits(root, manifest_hashes, amu, cira):
    reference = root / "reference/scores.raw"
    funcsim = root / "funcsim/scores.u32"
    expected_size = G20_WORDS * 4
    for path in (reference, funcsim):
        if not path.is_file() or path.stat().st_size != expected_size:
            raise TableEvidenceError(
                f"raw float32 result size is invalid: {path}"
            )
    hashes = {
        artifacts.sha256_file(reference),
        artifacts.sha256_file(funcsim),
        amu["reference_sha256"],
        cira["reference_sha256"],
    }
    if len(hashes) != 1:
        raise TableEvidenceError("raw float32 results are not bit-exact")
    only = next(iter(hashes))
    if (
        manifest_hashes.get("reference_raw") != only
        or manifest_hashes.get("funcsim_dump") != only
    ):
        raise TableEvidenceError("raw float32 manifest hash mismatch")
    return only


def _require_stored_decimal(summary, field, expected):
    if require_decimal(summary, field, "M2NDP summary") != expected:
        raise TableEvidenceError(f"stored M2NDP {field} mismatch")


def load_formal_rows(m2ndp_root, variants_root):
    m2ndp_root = Path(m2ndp_root).resolve()
    variants_root = Path(variants_root).resolve()
    try:
        status = load_json(m2ndp_root / "status.json", "M2NDP status")
        require_all_m2ndp_stages_passed(status)
        _require_contract(
            status.get("contract", {}),
            {
                "benchmark": "pr_spmv",
                "graph_scale": 20,
                "page_rank_iterations": 20,
                "trials": 2,
                "measured_trial": 1,
                "cpu": "timing",
                "cores": 2,
                "all_memory_cxl": True,
                "cxl_link_delay": "1us",
                "smoke_test": False,
            },
            "M2NDP status contract",
        )
        manifest = load_json(
            m2ndp_root / "manifest.json", "M2NDP manifest"
        )
        manifest_hashes = verify_m2ndp_manifest(
            m2ndp_root, manifest
        )
        summary = require_one_csv(
            m2ndp_root / "summary.csv", "M2NDP summary"
        )
        baseline = results.parse_gem5_summary(
            m2ndp_root / "gem5/run/summary.csv"
        )
        require_config_delay(
            m2ndp_root
            / "gem5/run/pr_spmv/cxl_vanilla/config.ini"
        )
        amu = load_variant(variants_root, "amu")
        cira = load_variant(variants_root, "cira")
        if amu["manifest_sha256"] != cira["manifest_sha256"]:
            raise TableEvidenceError(
                "AMU and CIRA variant manifests must be the same"
            )
        raw_sha256 = require_shared_raw_bits(
            m2ndp_root, manifest_hashes, amu, cira
        )
        ndpsim = validate_m2ndp_summary(
            m2ndp_root, summary, baseline, manifest_hashes
        )

        baseline_seconds = (
            Decimal(baseline.sim_ticks) / TICKS_PER_SECOND
        )
        amu_seconds = (
            Decimal(amu["row"]["sim_ticks"]) / TICKS_PER_SECOND
        )
        cira_seconds = (
            Decimal(cira["row"]["sim_ticks"]) / TICKS_PER_SECOND
        )
        m2ndp_seconds = (
            Decimal(ndpsim.measured_cycles)
            * ndpsim.core_period_seconds
        )
        rows = [
            MainRow(
                "Vanilla CXL", baseline_seconds, Decimal(1), "PASS"
            ),
            MainRow(
                "AMU",
                amu_seconds,
                baseline_seconds / amu_seconds,
                "Bit-exact PASS",
            ),
            MainRow(
                "CIRA",
                cira_seconds,
                baseline_seconds / cira_seconds,
                "Bit-exact PASS",
            ),
            MainRow(
                "M2NDP",
                m2ndp_seconds,
                baseline_seconds / m2ndp_seconds,
                "FuncSim bit-exact PASS",
            ),
        ]
        _require_stored_decimal(
            summary, "gem5_seconds", baseline_seconds
        )
        _require_stored_decimal(
            summary, "m2ndp_seconds", m2ndp_seconds
        )
        _require_stored_decimal(
            summary, "speedup", rows[3].speedup
        )
        return rows, {
            "graph_sha256": G20_SHA256,
            "raw_float32_sha256": raw_sha256,
            "m2ndp_manifest_sha256": artifacts.sha256_file(
                m2ndp_root / "manifest.json"
            ),
            "variant_manifest_sha256": amu["manifest_sha256"],
            "rows": [
                {
                    "system": row.system,
                    "latency_seconds": str(row.latency_seconds),
                    "speedup": str(row.speedup),
                    "correctness": row.correctness,
                }
                for row in rows
            ],
        }
    except TableEvidenceError:
        raise
    except (
        artifacts.EvidenceError,
        matched_runner.VariantRunError,
        calibration.CalibrationError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise TableEvidenceError(str(error)) from error


def close_speedup(stored, recomputed):
    difference = abs(stored - recomputed)
    limit = max(
        SPEEDUP_TOLERANCE,
        abs(recomputed) * SPEEDUP_TOLERANCE,
    )
    return difference <= limit


def sensitivity_key(row):
    latency = row.get("latency")
    benchmark = row.get("benchmark")
    if latency not in LATENCIES:
        raise TableEvidenceError(
            f"unsupported sensitivity latency: {latency!r}"
        )
    if benchmark not in WORKLOADS:
        raise TableEvidenceError(
            f"unsupported sensitivity workload: {benchmark!r}"
        )
    identity = (row.get("label"), row.get("kind"))
    configurations = {
        ("cxl_vanilla", "baseline"): "Baseline",
        ("amu", "amu"): "AMU",
        ("cira_pgo", "cira"): "CIRA",
    }
    configuration = configurations.get(identity)
    if configuration is None:
        raise TableEvidenceError(
            f"unsupported sensitivity configuration: {identity!r}"
        )
    return latency, benchmark, configuration


def _positive_integral_decimal(value, context):
    try:
        number = Decimal(str(value))
    except decimal.InvalidOperation as error:
        raise TableEvidenceError(f"{context} is not numeric") from error
    if (
        not number.is_finite()
        or number <= 0
        or number != number.to_integral_value()
    ):
        raise TableEvidenceError(
            f"{context} must be a positive integer"
        )
    return number


def validate_sensitivity_row(row, run_root):
    key = sensitivity_key(row)
    context = "/".join(key)
    if row.get("status") != "ok":
        raise TableEvidenceError(f"{context}: status is not ok")
    if row.get("verification") != "pass":
        raise TableEvidenceError(
            f"{context}: verification is not pass"
        )
    ticks = _positive_integral_decimal(
        row.get("sim_ticks"), f"{context} sim_ticks"
    )
    relative = Path(row.get("run_dir", ""))
    if relative.is_absolute():
        raise TableEvidenceError(f"{context}: run_dir must be relative")
    run_dir = (run_root / relative).resolve()
    try:
        run_dir.relative_to(run_root)
    except ValueError as error:
        raise TableEvidenceError(
            f"{context}: run_dir escapes the latency run root"
        ) from error
    require_config_delay(
        run_dir / "config.ini", expected=DELAY_TICKS[key[0]]
    )
    stored = require_decimal(
        row, "speedup_vs_cxl", f"{context} stored speedup"
    )
    return {
        "ticks": ticks,
        "stored_speedup": stored,
        "run_dir": str(run_dir),
    }


def require_exact_sensitivity_keys(by_key):
    expected = {
        (latency, benchmark, configuration)
        for latency in LATENCIES
        for benchmark in WORKLOADS
        for configuration in ("Baseline", "AMU", "CIRA")
    }
    actual = set(by_key)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TableEvidenceError(
            "sensitivity key set mismatch: "
            f"missing={missing}, extra={extra}"
        )


def _geomean(values):
    if not values or any(value <= 0 for value in values):
        raise TableEvidenceError(
            "sensitivity geomean inputs must be positive"
        )
    if all(value == values[0] for value in values):
        return values[0]
    return Decimal(
        str(
            math.exp(
                sum(math.log(float(value)) for value in values)
                / len(values)
            )
        )
    )


def calculate_sensitivity(by_key):
    output = {}
    for latency in LATENCIES:
        output[latency] = {}
        amu_values = []
        cira_values = []
        for benchmark in WORKLOADS:
            baseline = by_key[(latency, benchmark, "Baseline")]
            amu = by_key[(latency, benchmark, "AMU")]
            cira = by_key[(latency, benchmark, "CIRA")]
            amu_speedup = baseline["ticks"] / amu["ticks"]
            cira_speedup = baseline["ticks"] / cira["ticks"]
            for configuration, record, recomputed in (
                ("Baseline", baseline, Decimal(1)),
                ("AMU", amu, amu_speedup),
                ("CIRA", cira, cira_speedup),
            ):
                if not close_speedup(
                    record["stored_speedup"], recomputed
                ):
                    raise TableEvidenceError(
                        f"{latency}/{benchmark}/{configuration}: "
                        "stored speedup disagrees with recomputation"
                    )
            output[latency][benchmark] = {
                "AMU": amu_speedup,
                "CIRA": cira_speedup,
            }
            amu_values.append(amu_speedup)
            cira_values.append(cira_speedup)
        output[latency]["Geo."] = {
            "AMU": _geomean(amu_values),
            "CIRA": _geomean(cira_values),
        }
    return output


def load_sensitivity(csv_path, run_root):
    rows = read_csv(csv_path, "latency sensitivity")
    run_root = Path(run_root).resolve()
    if not run_root.is_dir():
        raise TableEvidenceError(
            f"latency run root is missing: {run_root}"
        )
    by_key = {}
    for row in rows:
        key = sensitivity_key(row)
        if key in by_key:
            raise TableEvidenceError(
                f"duplicate sensitivity row: {key}"
            )
        by_key[key] = validate_sensitivity_row(row, run_root)
    require_exact_sensitivity_keys(by_key)
    return calculate_sensitivity(by_key)


def main_csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=MAIN_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "system": row.system,
                "latency_seconds": str(row.latency_seconds),
                "speedup_vs_vanilla_cxl": str(row.speedup),
                "correctness": row.correctness,
            }
        )
    return stream.getvalue().encode("utf-8")


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def evidence_json_bytes(
    formal,
    sensitivity,
    input_hashes,
    *,
    repository_commit,
):
    payload = {
        "schema": 1,
        "contract": {
            "graph_sha256": G20_SHA256,
            "iterations": 20,
            "trials": 2,
            "measured_trial": 1,
            "cores": 2,
            "cpu": "timing",
            "all_memory_cxl": True,
            "cxl_link_delay": "1us",
        },
        "formal": _json_safe(formal),
        "sensitivity": _json_safe(sensitivity),
        "input_sha256": _json_safe(input_hashes),
        "repository_commit": repository_commit,
    }
    return (
        json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def latency_cell(value):
    return f"{value:.6f} s"


def speedup_cell(value):
    return f"{value:.2f}$\\times$"


def render_latex(rows, sensitivity, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise TableEvidenceError("evidence SHA-256 is invalid")
    systems = {
        "Vanilla CXL": "Vanilla CXL",
        "AMU": "AMU",
        "CIRA": "CIRA",
        "M2NDP": r"M$^2$NDP",
    }
    lines = [
        "% Generated by scripts/generate_gapbs_g20_e2e_table.py.",
        f"% Evidence SHA-256: {evidence_sha256}",
        r"\begin{table}[t]",
        r"\centering",
        (
            r"\caption{Panel (a) reports matched application end-to-end "
            r"latency for fixed-20 PageRank on g20 with two Timing cores, "
            r"all memory on 1~$\mu$s CXL. Panel (b) reports separate "
            r"scale 4, single-core latency sensitivity and is not g20 "
            r"evidence. Every displayed run passes its verifier; "
            r"M$^2$NDP additionally passes strict FuncSim bit-exact "
            r"validation.}"
        ),
        r"\label{tab:gapbs_vtune_cxl}",
        r"\textbf{(a) Formal g20 end-to-end comparison (1~$\mu$s CXL)}",
        r"\smallskip",
        r"\resizebox{\columnwidth}{!}{%",
        r"\begin{tabular}{@{}lrrl@{}}",
        r"\toprule",
        (
            r"\textbf{System} & \textbf{Latency} & "
            r"\textbf{Speedup} & \textbf{Correctness} \\"
        ),
        r"\midrule",
    ]
    for row in rows:
        try:
            system = systems[row.system]
        except KeyError as error:
            raise TableEvidenceError(
                f"unsupported formal system: {row.system!r}"
            ) from error
        lines.append(
            f"{system} & {latency_cell(row.latency_seconds)} & "
            f"{speedup_cell(row.speedup)} & {row.correctness} \\\\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}%",
            r"}",
            r"\medskip",
            (
                r"\textbf{(b) Scale-4 latency sensitivity "
                r"(single Timing core)}"
            ),
            r"\smallskip",
            r"\resizebox{\columnwidth}{!}{%",
            r"\begin{tabular}{@{}l*{4}{rr}@{}}",
            r"\toprule",
            (
                r"\textbf{Workload} & \multicolumn{2}{c}{200 ns} & "
                r"\multicolumn{2}{c}{500 ns} & "
                r"\multicolumn{2}{c}{1 $\mu$s} & "
                r"\multicolumn{2}{c}{2 $\mu$s} \\"
            ),
            (
                r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}"
                r"\cmidrule(lr){6-7}\cmidrule(lr){8-9}"
            ),
            (
                r" & \textbf{AMU} & \textbf{CIRA}"
                r" & \textbf{AMU} & \textbf{CIRA}"
                r" & \textbf{AMU} & \textbf{CIRA}"
                r" & \textbf{AMU} & \textbf{CIRA} \\"
            ),
            r"\midrule",
        ]
    )
    display = {
        "bfs": "BFS",
        "bc": "BC",
        "pr": "PR",
        "sssp": "SSSP",
        "Geo.": "Geo.",
    }
    for benchmark in (*WORKLOADS, "Geo."):
        cells = []
        for latency in LATENCIES:
            try:
                values = sensitivity[latency][benchmark]
                cells.extend(
                    (
                        speedup_cell(values["AMU"]),
                        speedup_cell(values["CIRA"]),
                    )
                )
            except KeyError as error:
                raise TableEvidenceError(
                    f"missing sensitivity cell: {latency}/{benchmark}"
                ) from error
        if benchmark == "Geo.":
            lines.append(r"\midrule")
        lines.append(
            f"{display[benchmark]} & " + " & ".join(cells) + r" \\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}%",
            r"}",
            r"\end{table}",
            "",
        ]
    )
    return "\n".join(lines)


def output_paths(output_dir):
    output_dir = Path(output_dir)
    return (
        output_dir / "gapbs-g20-e2e-results.csv",
        output_dir / "gapbs-g20-e2e-table-evidence.json",
        output_dir / "gapbs-vtune-cxl-table.tex",
        output_dir / "fig/gapbs-g20-e2e.pdf",
        output_dir / "fig/gapbs-g20-e2e.svg",
    )


def _validate_publication_payloads(contents):
    if any(not isinstance(content, bytes) or not content for content in contents):
        raise TableEvidenceError("publication payloads must be nonempty bytes")
    csv_bytes, evidence_bytes, latex_bytes, pdf_bytes, svg_bytes = contents
    try:
        csv_rows = list(
            csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
        )
        evidence = json.loads(evidence_bytes.decode("utf-8"))
        latex = latex_bytes.decode("utf-8")
        svg_root = ElementTree.fromstring(svg_bytes)
    except (
        UnicodeDecodeError,
        csv.Error,
        json.JSONDecodeError,
        ElementTree.ParseError,
    ) as error:
        raise TableEvidenceError(
            f"publication payload is not parseable: {error}"
        ) from error
    if len(csv_rows) != 4:
        raise TableEvidenceError("publication CSV must contain four rows")
    if not isinstance(evidence, dict):
        raise TableEvidenceError("publication evidence must be an object")
    if r"\begin{table}" not in latex:
        raise TableEvidenceError("publication TeX has no table")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise TableEvidenceError("publication PDF signature is invalid")
    if not svg_root.tag.endswith("svg"):
        raise TableEvidenceError("publication SVG root is invalid")


def _recover_stale_backup(target, backup):
    if not backup.exists():
        return
    if target.exists():
        backup.unlink()
    else:
        os.replace(backup, target)


def publish_bytes(
    output_dir,
    csv_bytes,
    evidence_bytes,
    latex_bytes,
    pdf_bytes,
    svg_bytes,
):
    output_dir = Path(output_dir)
    targets = output_paths(output_dir)
    contents = (
        csv_bytes,
        evidence_bytes,
        latex_bytes,
        pdf_bytes,
        svg_bytes,
    )
    _validate_publication_payloads(contents)
    staged = []
    backups = []
    promoted = []
    try:
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = target.with_name(f".{target.name}.bak")
            _recover_stale_backup(target, backup)
        for target, content in zip(
            targets,
            contents,
            strict=True,
        ):
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.unlink(missing_ok=True)
            temporary.write_bytes(content)
            staged.append((temporary, target))
        for target in targets:
            backup = target.with_name(f".{target.name}.bak")
            if target.exists():
                os.replace(target, backup)
                backups.append((backup, target))
        for temporary, target in staged:
            os.replace(temporary, target)
            promoted.append(target)
    except BaseException:
        for target in promoted:
            target.unlink(missing_ok=True)
        for backup, target in reversed(backups):
            if backup.exists():
                target.unlink(missing_ok=True)
                os.replace(backup, target)
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        raise
    for backup, _target in backups:
        backup.unlink(missing_ok=True)
    return targets


def git_head(root):
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise TableEvidenceError(
            f"cannot resolve repository commit: {error}"
        ) from error


def collect_input_hashes(
    m2ndp_root,
    variants_root,
    latency_csv,
    latency_run_root,
):
    paths = {
        "m2ndp_status": m2ndp_root / "status.json",
        "m2ndp_summary": m2ndp_root / "summary.csv",
        "m2ndp_manifest": m2ndp_root / "manifest.json",
        "variant_manifest": variants_root / "build/manifest.json",
        "amu_summary": variants_root / "amu/run/summary.csv",
        "amu_evidence": variants_root / "amu/run/evidence.json",
        "cira_summary": variants_root / "cira/run/summary.csv",
        "cira_evidence": variants_root / "cira/run/evidence.json",
        "latency_csv": latency_csv,
    }
    hashes = {
        name: artifacts.sha256_file(path)
        for name, path in paths.items()
    }
    config_hashes = {}
    for row in read_csv(latency_csv, "latency sensitivity"):
        relative = Path(row["run_dir"])
        config = (latency_run_root / relative / "config.ini").resolve()
        config_hashes[
            "/".join(
                (
                    row["latency"],
                    row["benchmark"],
                    row["label"],
                )
            )
        ] = artifacts.sha256_file(config)
    hashes["latency_config_sha256"] = config_hashes
    return hashes


def publish(
    m2ndp_root,
    variants_root,
    latency_csv,
    latency_run_root,
    output_dir,
):
    m2ndp_root = Path(m2ndp_root).resolve()
    variants_root = Path(variants_root).resolve()
    latency_csv = Path(latency_csv).resolve()
    latency_run_root = Path(latency_run_root).resolve()
    rows, formal = load_formal_rows(m2ndp_root, variants_root)
    sensitivity = load_sensitivity(latency_csv, latency_run_root)
    input_hashes = collect_input_hashes(
        m2ndp_root,
        variants_root,
        latency_csv,
        latency_run_root,
    )
    csv_bytes = main_csv_bytes(rows)
    evidence_bytes = evidence_json_bytes(
        formal,
        sensitivity,
        input_hashes,
        repository_commit=git_head(REPO),
    )
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    latex_bytes = render_latex(
        rows,
        sensitivity,
        evidence_sha256=evidence_sha256,
    ).encode("utf-8")
    pdf_bytes, svg_bytes = paper_figure.render_figure(
        rows,
        sensitivity,
        evidence_sha256=evidence_sha256,
    )
    return publish_bytes(
        output_dir,
        csv_bytes,
        evidence_bytes,
        latex_bytes,
        pdf_bytes,
        svg_bytes,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--m2ndp-results-root", type=Path, required=True
    )
    parser.add_argument(
        "--variants-results-root", type=Path, required=True
    )
    parser.add_argument("--latency-csv", type=Path, required=True)
    parser.add_argument(
        "--latency-run-root", type=Path, required=True
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        paths = publish(
            options.m2ndp_results_root,
            options.variants_results_root,
            options.latency_csv,
            options.latency_run_root,
            options.output_dir,
        )
    except (TableEvidenceError, OSError, ValueError) as error:
        print(f"GAPBS_G20_TABLE_FAILED error={error}")
        return 1
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
