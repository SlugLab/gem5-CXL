#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate the evidence-gated GAPBS g20 end-to-end paper table."""

import csv
import dataclasses
import decimal
import json
from decimal import Decimal
from pathlib import Path

try:
    from scripts import calibrate_m2ndp_cxl as calibration
    from scripts import m2ndp_artifacts as artifacts
    from scripts import m2ndp_results as results
    from scripts import run_gapbs_matched_pr_spmv_variants as matched_runner
    from scripts import run_m2ndp_g20_pr_spmv as orchestrator
except ImportError:
    import calibrate_m2ndp_cxl as calibration
    import m2ndp_artifacts as artifacts
    import m2ndp_results as results
    import run_gapbs_matched_pr_spmv_variants as matched_runner
    import run_m2ndp_g20_pr_spmv as orchestrator


REPO = Path(__file__).resolve().parents[1]
PATCH = REPO / "util/m2ndp/patches/0001-funcsim-strict-sequence.patch"
G20_SHA256 = artifacts.EXPECTED_G20_SHA256
G20_WORDS = 1 << 20
TICKS_PER_SECOND = Decimal(10**12)


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
