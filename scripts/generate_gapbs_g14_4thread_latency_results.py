#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Collect, validate, and atomically publish the formal g14 matrix."""

import argparse
import csv
import dataclasses
import decimal
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
    from scripts import m2ndp_results
    from scripts import run_gapbs_g14_4thread_latency_sweep as sweep
    from scripts import run_gapbs_matched_pr_spmv_variants as matched
except ImportError:
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts
    import m2ndp_results
    import run_gapbs_g14_4thread_latency_sweep as sweep
    import run_gapbs_matched_pr_spmv_variants as matched


PROFILE = "g14-4thread-sweep"
LATENCIES = ("200ns", "500ns", "1us", "2us")
LATENCY_NS = {"200ns": 200, "500ns": 500, "1us": 1000, "2us": 2000}
SYSTEMS = (
    "vanilla",
    "amu-paper-calibrated",
    "cira-static",
    "cira-pgo-selected",
    "cira-few-shot-online",
    "m2ndp",
)
AMU_SYSTEM = "amu-paper-calibrated"
CIRA_SYSTEMS = (
    "cira-static", "cira-pgo-selected", "cira-few-shot-online"
)
TICKS_PER_SECOND = decimal.Decimal(10**12)
VECTOR_BYTES = (1 << 14) * 4
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CSV_NAME = "gapbs-g14-4thread-latency.csv"
EVIDENCE_NAME = "gapbs-g14-4thread-latency-evidence.json"
TEX_NAME = "gapbs-g14-4thread-latency-table-data.tex"
PDF_NAME = "gapbs-g14-4thread-latency-sweep.pdf"
SVG_NAME = "gapbs-g14-4thread-latency-sweep.svg"
VALIDATION_NAME = "gapbs-g14-4thread-latency-validation.json"
OUTPUT_NAMES = (CSV_NAME, EVIDENCE_NAME, TEX_NAME, PDF_NAME, SVG_NAME,
                VALIDATION_NAME)
REAL_CXL_FIELDS = m2ndp_results.REAL_CXL_FIELDS
COMMON_PROVENANCE = (
    "profile_manifest_sha256", "source_sha256", "config_sha256",
    "checkpoint_sha256", "binary_sha256",
)
M2NDP_PROVENANCE = (
    "trace_sha256", "m2ndp_patch_sha256", "m2ndp_config_sha256",
    "funcsim_binary_sha256", "ndpsim_binary_sha256", "calibration_sha256",
    "gem5_binary_sha256",
)
FIELDNAMES = (
    "profile", "benchmark", "latency", "system", "graph_sha256",
    "profile_manifest_sha256", "cores", "threads", "trials",
    "measured_trial", "iterations", "all_memory_cxl", "verification",
    "bit_exact", "raw_vector_bytes", "result_sha256", "roi_seconds",
    "roi_ticks", "roi_microseconds", "speedup", "sim_ticks",
    "end_to_end_ticks", "profiling_ticks", "reconfiguration_ticks",
    "steady_ticks", "amu_cira_calibration_sha256", "amu_profile",
    "cira_mode", "policy_manifest_sha256", "fit_residuals_json",
    "amu_pdf_doi", "amu_pdf_sha256", "cira_csv_sha256",
    "measured_cycles", "core_period_seconds", *REAL_CXL_FIELDS,
    "asmc_loads", "asmc_completed", "cira_prefetches", "cira_completed",
    "cira_indexed_prefetches", "cira_csr_prefetches",
    "cira_issued_per_core", "cira_completed_per_core", "cira_csr_per_core",
    "cira_rejected_queue_full", "cira_dropped_csr_descriptors",
    "cira_rejected_csr_index_queue_full",
    "cira_csr_queue_high_watermark", "funcsim_compared",
    "funcsim_mismatched", "calibration_pass", "calibration_cxl_delay",
    "calibration_residual_ns", "calibration_link_period_ns",
    "gem5_microprobe_ns", "m2ndp_boundary_ns", "source_path",
    "source_sha256", "config_sha256", "checkpoint_sha256", "binary_sha256",
    "trace_sha256", "m2ndp_patch_sha256", "m2ndp_config_sha256",
    "funcsim_binary_sha256", "ndpsim_binary_sha256", "calibration_sha256",
    "gem5_binary_sha256", "provenance_json",
)


class PublicationError(RuntimeError):
    """Evidence is incomplete or inconsistent and cannot be published."""


@dataclasses.dataclass(frozen=True)
class PublicationPaths:
    root: Path
    csv: Path
    evidence: Path
    tex: Path
    pdf: Path
    svg: Path
    validation: Path

    @property
    def files(self):
        return (self.csv, self.evidence, self.tex, self.pdf, self.svg,
                self.validation)


def publication_paths(root):
    root = Path(root)
    return PublicationPaths(root, *(root / name for name in OUTPUT_NAMES))


def _decimal(value, name, *, allow_zero=False):
    try:
        number = decimal.Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError) as error:
        raise PublicationError(f"{name} is not decimal") from error
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        raise PublicationError(f"{name} must be finite and positive")
    return number


def _integer(value, name, *, allow_zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise PublicationError(f"{name} is not an integer")
    number = int(value)
    if number < 0 or (number == 0 and not allow_zero):
        raise PublicationError(f"{name} must be positive")
    return number


def _sha(value, name):
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise PublicationError(f"missing provenance hash: {name}")
    return value


def _per_core(row, field):
    try:
        values = tuple(int(value) for value in row.get(field, "").split(";"))
    except (TypeError, ValueError) as error:
        raise PublicationError(f"invalid {field}") from error
    if len(values) != 4 or any(value <= 0 for value in values):
        raise PublicationError("four active CIRA ports are required")
    return values


def _row_seconds(row):
    if row["system"] == "m2ndp":
        cycles = _integer(row.get("measured_cycles"), "measured cycles")
        period = _decimal(row.get("core_period_seconds"), "core period")
        return decimal.Decimal(cycles) * period
    return _decimal(
        row.get("end_to_end_ticks"), "end_to_end_ticks"
    ) / TICKS_PER_SECOND


def _validate_activity(row):
    system = row["system"]
    if system == AMU_SYSTEM:
        issued = _integer(row.get("asmc_loads"), "AMU loads")
        completed = _integer(row.get("asmc_completed"), "AMU completed")
        if issued != completed:
            raise PublicationError("AMU mechanism gate failed")
    elif system in CIRA_SYSTEMS:
        completed = _integer(row.get("cira_completed"), "CIRA completed")
        descriptors = sum(_integer(row.get(field), field, allow_zero=True)
                          for field in ("cira_prefetches",
                                        "cira_indexed_prefetches",
                                        "cira_csr_prefetches"))
        if descriptors <= 0 or completed <= 0:
            raise PublicationError("CIRA mechanism gate failed")
        if _per_core(row, "cira_issued_per_core") != _per_core(
            row, "cira_completed_per_core"
        ):
            raise PublicationError("four active CIRA ports are required")
        _per_core(row, "cira_csr_per_core")
        for field in ("cira_rejected_queue_full",
                      "cira_rejected_csr_index_queue_full",
                      "cira_dropped_csr_descriptors"):
            if _integer(row.get(field), field, allow_zero=True):
                raise PublicationError(f"CIRA mechanism gate failed: {field}")
        if _integer(row.get("cira_csr_queue_high_watermark"),
                    "CIRA queue watermark", allow_zero=True) > 4096:
            raise PublicationError("CIRA mechanism gate failed: queue bound")
    elif system == "m2ndp":
        if row.get("funcsim_compared") != "16384" or row.get(
            "funcsim_mismatched"
        ) != "0":
            raise PublicationError("FuncSim bit-exact gate failed")
        if row.get("calibration_pass") != "pass" or row.get(
            "calibration_cxl_delay"
        ) != row["latency"]:
            raise PublicationError("M2NDP calibration gate failed")
        residual = _decimal(row.get("calibration_residual_ns"),
                            "calibration residual", allow_zero=True)
        microprobe = _decimal(row.get("gem5_microprobe_ns"),
                              "gem5 microprobe")
        boundary = _decimal(row.get("m2ndp_boundary_ns"), "M2NDP boundary")
        if residual != abs(boundary - microprobe) or residual > decimal.Decimal("0.125"):
            raise PublicationError("M2NDP calibration residual exceeds 0.125 ns")


def validate_matrix(rows, *, graph_sha256, profile_manifest_sha256,
                    require_sensitivity=False, explanation=""):
    rows = [dict(row) for row in rows]
    expected_count = len(LATENCIES) * len(SYSTEMS)
    if len(rows) != expected_count:
        raise PublicationError(
            f"publication requires exactly {expected_count} rows, found {len(rows)}"
        )
    expected_keys = {(latency, system) for latency in LATENCIES for system in SYSTEMS}
    keys = [(row.get("latency"), row.get("system")) for row in rows]
    if len(set(keys)) != expected_count or set(keys) != expected_keys:
        raise PublicationError(
            f"publication is not the exact {expected_count} calibrated rows"
        )
    _sha(graph_sha256, "graph")
    _sha(profile_manifest_sha256, "profile manifest")
    by_key = {}
    expected = {
        "profile": PROFILE, "benchmark": "pr_spmv", "graph_sha256": graph_sha256,
        "profile_manifest_sha256": profile_manifest_sha256, "cores": "4",
        "threads": "4", "trials": "2", "measured_trial": "1",
        "iterations": "20", "all_memory_cxl": "True", "verification": "pass",
        "bit_exact": "pass", "raw_vector_bytes": str(VECTOR_BYTES),
    }
    for row in rows:
        context = f"{row.get('latency')}/{row.get('system')}"
        for field, value in expected.items():
            if row.get(field) != value:
                message = "vector length" if field == "raw_vector_bytes" else field
                raise PublicationError(f"{context} {message} mismatch")
        _sha(row.get("result_sha256"), "result")
        _sha(
            row.get("amu_cira_calibration_sha256"),
            "AMU/CIRA calibration manifest",
        )
        _sha(row.get("amu_pdf_sha256"), "AMU PDF")
        _sha(row.get("cira_csv_sha256"), "CIRA CSV")
        if row.get("amu_pdf_doi") != "10.1145/3663479":
            raise PublicationError("AMU PDF DOI mismatch")
        if row["system"] == AMU_SYSTEM and row.get("amu_profile") != "paper-calibrated":
            raise PublicationError("AMU row is not paper-calibrated")
        if row["system"] in CIRA_SYSTEMS:
            expected_mode = row["system"].removeprefix("cira-")
            if row.get("cira_mode") != expected_mode:
                raise PublicationError(f"{context} CIRA mode mismatch")
            _sha(row.get("policy_manifest_sha256"), "CIRA policy manifest")
        try:
            residuals = json.loads(row.get("fit_residuals_json", ""))
        except (TypeError, json.JSONDecodeError) as error:
            raise PublicationError("missing calibrated residual fields") from error
        if not isinstance(residuals, dict) or not residuals:
            raise PublicationError("missing calibrated residual fields")
        end_to_end = _decimal(row.get("end_to_end_ticks"), "end-to-end ticks")
        profiling = _decimal(
            row.get("profiling_ticks"), "profiling ticks", allow_zero=True
        )
        reconfiguration = _decimal(
            row.get("reconfiguration_ticks"),
            "reconfiguration ticks", allow_zero=True,
        )
        steady = _decimal(row.get("steady_ticks"), "steady ticks")
        if end_to_end != profiling + reconfiguration + steady:
            raise PublicationError(f"{context} end-to-end tick accounting mismatch")
        if row["system"] == "cira-few-shot-online":
            if profiling <= 0 or reconfiguration <= 0:
                raise PublicationError("few-shot profiling costs are not charged")
        elif profiling != 0 or reconfiguration != 0:
            raise PublicationError(f"{context} has unexpected profiling cost")
        for field in COMMON_PROVENANCE:
            _sha(row.get(field), field)
        try:
            provenance = json.loads(row.get("provenance_json", ""))
        except (TypeError, json.JSONDecodeError) as error:
            raise PublicationError("missing provenance map") from error
        if not isinstance(provenance, dict) or not provenance:
            raise PublicationError("missing provenance map")
        for field, digest in provenance.items():
            _sha(digest, f"provenance_json/{field}")
        if row["system"] == "m2ndp":
            for field in M2NDP_PROVENANCE:
                _sha(row.get(field), field)
        for field in REAL_CXL_FIELDS:
            _integer(row.get(field), field)
        _validate_activity(row)
        seconds = _row_seconds(row)
        if _decimal(row.get("roi_seconds"), "ROI seconds") != seconds:
            raise PublicationError(f"{context} ROI seconds mismatch")
        expected_ticks = seconds * TICKS_PER_SECOND
        if _decimal(row.get("roi_ticks"), "ROI ticks") != expected_ticks:
            raise PublicationError(f"{context} ROI ticks mismatch")
        if _decimal(row.get("roi_microseconds"), "ROI microseconds") != seconds * 10**6:
            raise PublicationError(f"{context} ROI microseconds mismatch")
        by_key[(row["latency"], row["system"])] = (row, seconds)
    for latency in LATENCIES:
        hashes = {by_key[(latency, system)][0]["result_sha256"] for system in SYSTEMS}
        if len(hashes) != 1:
            raise PublicationError(f"{latency} vectors are not bit-exact")
        vanilla_seconds = by_key[(latency, "vanilla")][1]
        for system in SYSTEMS:
            row, seconds = by_key[(latency, system)]
            expected_speedup = vanilla_seconds / seconds
            if _decimal(row.get("speedup"), "speedup") != expected_speedup:
                raise PublicationError(f"{latency}/{system} speedup mismatch")
    if require_sensitivity:
        first = _integer(by_key[("200ns", "vanilla")][0]["sim_ticks"], "200ns ticks")
        last = _integer(by_key[("2us", "vanilla")][0]["sim_ticks"], "2us ticks")
        if last <= first:
            raise PublicationError("Vanilla 2us ROI must be slower than Vanilla 200ns ROI")
        for field in REAL_CXL_FIELDS:
            values = [_integer(by_key[(latency, "vanilla")][0][field], field)
                      for latency in LATENCIES]
            if decimal.Decimal(max(values) - min(values)) / decimal.Decimal(min(values)) > decimal.Decimal("0.05") and not explanation.strip():
                raise PublicationError(f"{field} varies by more than 5 percent without explanation")
    return tuple(
        by_key[(latency, system)][0]
        for latency in LATENCIES for system in SYSTEMS
    )


def _read_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def _single_csv(path, label):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise PublicationError(f"invalid {label}: {error}") from error
    if len(rows) != 1:
        raise PublicationError(f"{label} must contain one row")
    return rows[0]


def _relative(path, root):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError as error:
        raise PublicationError(f"source escapes sweep root: {path}") from error


def _record_provenance(record):
    return {
        **{f"input/{name}": digest
           for name, digest in record.get("input_hashes", {}).items()},
        **{f"output/{name}": digest
           for name, digest in record.get("output_hashes", {}).items()},
    }


def _base(
    profile, manifest_sha, latency, system, source, raw, root, hashes,
    provenance, calibration_provenance,
):
    row = {field: "" for field in FIELDNAMES}
    raw = Path(raw)
    if raw.stat().st_size != profile.num_nodes * 4:
        raise PublicationError(f"{latency}/{system} raw vector length mismatch")
    row.update(
        profile=PROFILE, benchmark="pr_spmv", latency=latency, system=system,
        graph_sha256=profile.graph_sha256, profile_manifest_sha256=manifest_sha,
        cores="4", threads="4", trials="2", measured_trial="1",
        iterations="20", all_memory_cxl="True", verification="pass",
        bit_exact="pass", raw_vector_bytes=str(raw.stat().st_size),
        result_sha256=artifacts.sha256_file(raw), source_path=_relative(source, root),
        source_sha256=hashes["summary"], config_sha256=hashes["config"],
        checkpoint_sha256=hashes["checkpoint"],
        binary_sha256=hashes.get("binary", hashes.get("workload_binary", "")),
        provenance_json=json.dumps(provenance, sort_keys=True,
                                   separators=(",", ":")),
        asmc_loads="0", asmc_completed="0", cira_prefetches="0",
        cira_completed="0", cira_indexed_prefetches="0",
        cira_csr_prefetches="0", cira_rejected_queue_full="0",
        cira_dropped_csr_descriptors="0", cira_csr_queue_high_watermark="0",
        cira_rejected_csr_index_queue_full="0",
        amu_cira_calibration_sha256=calibration_provenance[
            "calibration_manifest_sha256"
        ],
        amu_profile=("paper-calibrated" if system == AMU_SYSTEM else ""),
        cira_mode=(
            system.removeprefix("cira-") if system in CIRA_SYSTEMS else ""
        ),
        fit_residuals_json=json.dumps(
            {
                "fit": calibration_provenance.get("fit_residuals", {}),
                "holdout": calibration_provenance.get(
                    "holdout_residuals", {}
                ),
            },
            sort_keys=True, separators=(",", ":"),
        ),
        profiling_ticks="0", reconfiguration_ticks="0",
        amu_pdf_doi=calibration_provenance.get("source_identity", {}).get(
            "amu_pdf_doi", ""
        ),
        amu_pdf_sha256=calibration_provenance.get("source_hashes", {}).get(
            "amu_pdf", ""
        ),
        cira_csv_sha256=calibration_provenance.get("source_hashes", {}).get(
            "cira_csv", ""
        ),
    )
    return row


def _record(state, latency, system):
    record = state.get("latencies", {}).get(latency, {}).get(system)
    if not isinstance(record, dict) or record.get("status") != "passed":
        raise PublicationError(f"{latency}/{system} is not passed")
    paths = {name: Path(path) for name, path in record.get("output_paths", {}).items()}
    actual = sweep.shared.hash_named_paths(paths)
    if actual != record.get("output_hashes"):
        raise PublicationError(f"{latency}/{system} output provenance changed")
    return record, paths, actual


def _fill_gem5(row, source_row):
    row["sim_ticks"] = source_row["sim_ticks"]
    row["steady_ticks"] = source_row["sim_ticks"]
    row["end_to_end_ticks"] = source_row.get(
        "end_to_end_ticks", source_row["sim_ticks"]
    )
    row["profiling_ticks"] = source_row.get("profiling_ticks", "0")
    row["reconfiguration_ticks"] = source_row.get(
        "reconfiguration_ticks", "0"
    )
    seconds = decimal.Decimal(row["end_to_end_ticks"]) / TICKS_PER_SECOND
    row["roi_seconds"] = str(seconds)
    row["roi_ticks"] = row["end_to_end_ticks"]
    row["roi_microseconds"] = str(seconds * 10**6)
    for field in REAL_CXL_FIELDS:
        row[field] = source_row.get(field, "")


def collect_rows(sweep_root):
    root = Path(sweep_root).resolve()
    status_path = root / "formal/status.json"
    state = _read_json(status_path, "formal sweep status")
    qualification_path = root / "qualification/qualification.json"
    qualification = _read_json(
        qualification_path, "calibrated g12 qualification"
    )
    calibration_provenance = qualification.get("calibration")
    if (
        qualification.get("schema") != 2
        or qualification.get("status") != "PASS"
        or not isinstance(calibration_provenance, dict)
        or calibration_provenance.get("status") != "PASS"
    ):
        raise PublicationError("passed calibrated g12 qualification is required")
    manifest_path = root / "graphs/g14.manifest.json"
    profile = profiles.load_frozen_profile(PROFILE, manifest_path)
    manifest_sha = artifacts.sha256_file(manifest_path)
    contract = state.get("contract", {})
    if contract.get("profile") != PROFILE or contract.get("hashes", {}).get("graph_manifest") != manifest_sha:
        raise PublicationError("formal sweep/profile manifest contract mismatch")
    rows = []
    sources = {}
    for latency in LATENCIES:
        latency_root = root / "formal/runs" / latency
        mroot = latency_root / "m2ndp"
        reference = mroot / "reference/scores.raw"
        vanilla_record, vanilla_paths, vanilla_hashes = _record(state, latency, "vanilla")
        vanilla_source = vanilla_paths["summary"]
        vanilla_evidence = m2ndp_results.parse_gem5_summary(
            vanilla_source, profile=profile, latency=latency
        )
        vanilla = _base(profile, manifest_sha, latency, "vanilla", vanilla_source,
                        vanilla_paths["raw"], root, vanilla_hashes,
                        _record_provenance(vanilla_record),
                        calibration_provenance)
        _fill_gem5(vanilla, vanilla_evidence.row)
        rows.append(vanilla)
        for system in (AMU_SYSTEM, *CIRA_SYSTEMS):
            record, paths, hashes = _record(state, latency, system)
            source = paths["summary"]
            source_row = _single_csv(source, f"{latency}/{system} summary")
            typed = dict(source_row)
            for field in ("scale", "iterations", "measured_trial", "cores",
                          "checkpoint_restores", "sim_ticks", "asmc_loads",
                          "asmc_completed", "cira_prefetches", "cira_completed",
                          "cira_indexed_prefetches", "cira_csr_prefetches"):
                typed[field] = int(typed.get(field, 0))
            typed["all_memory_cxl"] = source_row.get("all_memory_cxl") == "True"
            kind = "amu" if system == AMU_SYSTEM else "cira"
            matched.validate_row(typed, kind, smoke_test=False, profile=profile,
                                 latency=latency)
            matched.validate_config_delay(paths["config"], latency)
            row = _base(profile, manifest_sha, latency, system, source,
                        paths["raw"], root, {**hashes,
                        "binary": record.get("input_hashes", {}).get("binary", "")},
                        _record_provenance(record), calibration_provenance)
            _fill_gem5(row, source_row)
            if system in CIRA_SYSTEMS:
                row["policy_manifest_sha256"] = record.get(
                    "policy_manifest_sha256",
                    record.get("provenance", {}).get(
                        "policy_manifest_sha256", ""
                    ),
                )
            for field in ("asmc_loads", "asmc_completed", "cira_prefetches",
                          "cira_completed", "cira_indexed_prefetches",
                          "cira_csr_prefetches", "cira_issued_per_core",
                          "cira_completed_per_core", "cira_csr_per_core",
                          "cira_rejected_queue_full", "cira_dropped_csr_descriptors",
                          "cira_rejected_csr_index_queue_full",
                          "cira_csr_queue_high_watermark"):
                row[field] = source_row.get(field, row[field])
            rows.append(row)
        record, paths, hashes = _record(state, latency, "m2ndp")
        source = paths["summary"]
        source_row = _single_csv(source, f"{latency}/M2NDP summary")
        final_manifest = _read_json(paths["m2ndp_manifest"], "M2NDP manifest")
        artifact_hashes = final_manifest.get("artifact_sha256", {})
        calibration = _read_json(paths["calibration"], "M2NDP calibration")
        row = _base(profile, manifest_sha, latency, "m2ndp", source,
                    paths["raw"], root, hashes, _record_provenance(record),
                    calibration_provenance)
        if artifacts.sha256_file(paths["funcsim_raw"]) != row["result_sha256"]:
            raise PublicationError(f"{latency} FuncSim is not bit-exact")
        expected_m2 = {"profile": PROFILE, "benchmark": "pr_spmv",
                       "graph_sha256": profile.graph_sha256, "cores": "4",
                       "iterations": "20", "trials": "2", "measured_trial": "1",
                       "all_memory_cxl": "True", "cxl_link_delay": latency,
                       "verification": "pass", "funcsim_strict": "pass",
                       "funcsim_compared": "16384", "funcsim_dump_sha256": row["result_sha256"],
                       "reference_raw_sha256": row["result_sha256"],
                       "profile_manifest_sha256": manifest_sha}
        for field, value in expected_m2.items():
            if source_row.get(field) != value:
                raise PublicationError(f"{latency}/M2NDP {field} mismatch")
        manifest_contract = final_manifest.get("contract", {})
        if (manifest_contract.get("profile") != PROFILE
                or manifest_contract.get("cxl_link_delay") != latency
                or manifest_contract.get("profile_manifest_sha256") != manifest_sha):
            raise PublicationError(f"{latency}/M2NDP manifest binding mismatch")
        summary_to_artifact = {
            "trace_sha256": "trace",
            "m2ndp_patch_sha256": "m2ndp_patch",
            "m2ndp_config_sha256": "m2ndp_config",
            "gem5_binary_sha256": "gem5_binary",
        }
        for summary_field, artifact_name in summary_to_artifact.items():
            if source_row.get(summary_field) != artifact_hashes.get(artifact_name):
                raise PublicationError(
                    f"{latency}/M2NDP provenance mismatch: {summary_field}"
                )
        for artifact_name, output_name in (
            ("reference_raw", "raw"), ("funcsim_dump", "funcsim_raw"),
            ("calibration", "calibration"), ("summary", "summary"),
            ("profile_manifest", None),
        ):
            expected_hash = manifest_sha if output_name is None else hashes[output_name]
            if artifact_hashes.get(artifact_name) != expected_hash:
                raise PublicationError(
                    f"{latency}/M2NDP artifact hash mismatch: {artifact_name}"
                )
        if final_manifest.get("build_binary_sha256") != hashes.get("workload_binary"):
            raise PublicationError(f"{latency}/M2NDP kernel binary mismatch")
        row.update(
            measured_cycles=source_row.get("ndpsim_measured_cycles", ""),
            core_period_seconds=source_row.get("ndpsim_core_period_seconds", ""),
            roi_seconds=source_row.get("m2ndp_seconds", ""),
            funcsim_compared=source_row["funcsim_compared"], funcsim_mismatched="0",
            calibration_pass="pass" if calibration.get("passed") is True else "failed",
            calibration_cxl_delay=calibration.get("cxl_link_delay", calibration.get("cxl_delay", "")),
            calibration_residual_ns=str(calibration.get("residual_ns", "")),
            calibration_link_period_ns=str(calibration.get("link_period_ns", "")),
            gem5_microprobe_ns=str(calibration.get("gem5_microprobe_ns", "")),
            m2ndp_boundary_ns=str(calibration.get("m2ndp_boundary_ns", "")),
            trace_sha256=source_row.get("trace_sha256", ""),
            m2ndp_patch_sha256=source_row.get("m2ndp_patch_sha256", ""),
            m2ndp_config_sha256=source_row.get("m2ndp_config_sha256", ""),
            funcsim_binary_sha256=artifact_hashes.get("funcsim_binary", ""),
            ndpsim_binary_sha256=artifact_hashes.get("ndpsim_binary", ""),
            calibration_sha256=hashes.get("calibration", ""),
            gem5_binary_sha256=source_row.get("gem5_binary_sha256", ""),
            provenance_json=json.dumps(
                {**_record_provenance(record),
                 **{f"manifest/{name}": digest
                    for name, digest in artifact_hashes.items()}},
                sort_keys=True, separators=(",", ":"),
            ),
        )
        seconds = _row_seconds(row)
        row["steady_ticks"] = row["roi_ticks"] = str(
            seconds * TICKS_PER_SECOND
        )
        row["end_to_end_ticks"] = row["steady_ticks"]
        row["roi_ticks"] = str(seconds * TICKS_PER_SECOND)
        row["roi_microseconds"] = str(seconds * 10**6)
        for field in REAL_CXL_FIELDS:
            row[field] = source_row.get(field, "")
        rows.append(row)
        sources[latency] = {system: state["latencies"][latency][system]["output_hashes"]
                            for system in SYSTEMS}
    seconds = {(row["latency"], row["system"]): _row_seconds(row) for row in rows}
    for row in rows:
        row["speedup"] = str(seconds[(row["latency"], "vanilla")] /
                             seconds[(row["latency"], row["system"])])
    validated = validate_matrix(rows, graph_sha256=profile.graph_sha256,
                                profile_manifest_sha256=manifest_sha,
                                require_sensitivity=True)
    return validated, {"status_sha256": artifacts.sha256_file(status_path),
                       "profile_manifest_sha256": manifest_sha,
                       "source_hashes": sources}


def render_tex(rows):
    labels = {
        "vanilla": "Vanilla CXL",
        "amu-paper-calibrated": "AMU (paper-calibrated)",
        "cira-static": "CIRA static",
        "cira-pgo-selected": "CIRA PGO-selected",
        "cira-few-shot-online": "CIRA few-shot online",
        "m2ndp": r"M$^2$NDP",
    }
    calibration = rows[0]
    lines = [
             "% AMU DOI: " + calibration["amu_pdf_doi"],
             "% AMU PDF SHA-256: " + calibration["amu_pdf_sha256"],
             "% CIRA CSV SHA-256: " + calibration["cira_csv_sha256"],
             r"\begin{tabular}{llrr}", r"\toprule",
             r"CXL latency & System & ROI ($\mu$s) & Speedup \\", r"\midrule"]
    for index, row in enumerate(rows):
        latency = row["latency"].replace("us", r"\,$\mu$s")
        lines.append(f"{latency} & {labels[row['system']]} & "
                     f"{decimal.Decimal(row['roi_microseconds']):.3f} & "
                     f"{decimal.Decimal(row['speedup']):.3f}\\,$\\times$ \\\\")
        if (
            (index + 1) % len(SYSTEMS) == 0
            and index != len(rows) - 1
        ):
            lines.append(r"\midrule")
    return "\n".join((*lines, r"\bottomrule", r"\end{tabular}")) + "\n"


def _write_text(path, value):
    path = Path(path)
    path.write_text(value, encoding="utf-8")
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_dir(path):
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_staged_files(rows, staging, *, graph_sha256,
                       profile_manifest_sha256, source_evidence=None):
    rows = validate_matrix(rows, graph_sha256=graph_sha256,
                           profile_manifest_sha256=profile_manifest_sha256)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=False)
    paths = publication_paths(staging)
    artifacts.atomic_write_csv(paths.csv, FIELDNAMES, rows)
    _write_text(paths.tex, render_tex(rows))
    evidence = {"schema": 1, "profile": PROFILE, "graph_sha256": graph_sha256,
                "profile_manifest_sha256": profile_manifest_sha256,
                "row_count": len(LATENCIES) * len(SYSTEMS),
                "csv_sha256": artifacts.sha256_file(paths.csv),
                "rows": list(rows), "source_evidence": source_evidence or {}}
    artifacts.atomic_write_json(paths.evidence, evidence)
    from scripts import generate_gapbs_g14_4thread_latency_figure as figure
    figure.write_figure(rows, evidence_sha256=artifacts.sha256_file(paths.evidence),
                        outdir=staging)
    for path in paths.files[:-1]:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    _fsync_dir(staging)
    return paths


def publish(rows, output_root, *, graph_sha256, profile_manifest_sha256,
            source_evidence=None, sweep_root=None):
    validate_matrix(rows, graph_sha256=graph_sha256,
                    profile_manifest_sha256=profile_manifest_sha256)
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".g14-publication-", dir=output_root))
    staging.rmdir()
    try:
        paths = write_staged_files(rows, staging, graph_sha256=graph_sha256,
                                   profile_manifest_sha256=profile_manifest_sha256,
                                   source_evidence=source_evidence)
        from scripts import validate_gapbs_g14_4thread_latency_results as validator
        report = validator.validate_directory(
            staging, sweep_root, expected_rows=None if sweep_root else rows
        )
        artifacts.atomic_write_json(paths.validation, report)
        with paths.validation.open("rb") as stream:
            os.fsync(stream.fileno())
        _fsync_dir(staging)
        content = {path.name: artifacts.sha256_file(path) for path in paths.files}
        digest = hashlib.sha256(json.dumps(
            content, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        immutable = output_root / f"publication-{digest}"
        if immutable.exists():
            existing = {name: artifacts.sha256_file(immutable / name)
                        for name in OUTPUT_NAMES}
            if existing != content:
                raise PublicationError("content-addressed publication collision")
            shutil.rmtree(staging)
        else:
            os.replace(staging, immutable)
        temporary_link = output_root / f".publication-current.{os.getpid()}"
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(immutable.name)
        os.replace(temporary_link, output_root / "publication-current")
        _fsync_dir(output_root)
        return publication_paths(immutable)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = args.sweep_root.resolve()
    try:
        rows, evidence = collect_rows(root)
        paths = publish(rows, (args.output_root or root / "formal").resolve(),
                        graph_sha256=rows[0]["graph_sha256"],
                        profile_manifest_sha256=rows[0]["profile_manifest_sha256"],
                        source_evidence=evidence, sweep_root=root)
        print(f"G14_PUBLICATION_COMPLETE root={paths.root}")
        return 0
    except (PublicationError, profiles.ProfileError, artifacts.EvidenceError,
            matched.VariantRunError, OSError, KeyError) as error:
        print(f"G14_PUBLICATION_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
