#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate and publish the formal g4/four-thread latency matrix."""

import argparse
import csv
import dataclasses
import decimal
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
    from scripts import run_gapbs_matched_pr_spmv_variants as matched_runner
except ImportError:
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts
    import m2ndp_results
    import run_gapbs_matched_pr_spmv_variants as matched_runner


LATENCIES = ("200ns", "500ns", "1us", "2us")
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
REAL_CXL_FIELDS = m2ndp_results.REAL_CXL_FIELDS
TICKS_PER_SECOND = decimal.Decimal(10**12)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
CSV_NAME = "gapbs-g4-4thread-latency-results.csv"
EVIDENCE_NAME = "gapbs-g4-4thread-latency-evidence.json"
TEX_NAME = "gapbs-g4-4thread-latency-table.tex"
FIELDNAMES = (
    "profile",
    "benchmark",
    "latency",
    "system",
    "graph_sha256",
    "cores",
    "threads",
    "trials",
    "measured_trial",
    "iterations",
    "all_memory_cxl",
    "verification",
    "bit_exact",
    "result_sha256",
    "latency_seconds",
    "speedup_vs_vanilla_cxl",
    "sim_ticks",
    "measured_cycles",
    "core_period_seconds",
    "asmc_loads",
    "asmc_completed",
    "cira_prefetches",
    "cira_completed",
    "cira_indexed_prefetches",
    "cira_csr_prefetches",
    "cira_issued_per_core",
    "cira_completed_per_core",
    "cira_csr_per_core",
    "cira_rejected_queue_full",
    "cira_dropped_csr_descriptors",
    "cira_csr_queue_high_watermark",
    "funcsim_compared",
    "funcsim_mismatched",
    "calibration_pass",
    "calibration_cxl_delay",
    "calibration_residual_ns",
    "calibration_link_period_ns",
    "source_path",
    "source_sha256",
)


class PublicationError(RuntimeError):
    """A row or artifact is not safe to publish."""


@dataclasses.dataclass(frozen=True)
class PublicationPaths:
    root: Path
    csv: Path
    evidence: Path
    tex: Path


def _decimal(value, name, *, allow_zero=False):
    try:
        number = decimal.Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError) as error:
        raise PublicationError(f"{name} is not decimal: {value!r}") from error
    if not number.is_finite():
        raise PublicationError(f"{name} must be finite")
    if number < 0 or (number == 0 and not allow_zero):
        raise PublicationError(f"{name} must be positive")
    return number


def _integer(value, name, *, allow_zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise PublicationError(f"{name} is not an integer: {value!r}")
    number = int(value)
    if number < 0 or (number == 0 and not allow_zero):
        raise PublicationError(f"{name} must be positive")
    return number


def gem5_seconds(row):
    return decimal.Decimal(
        _integer(row.get("sim_ticks"), "sim_ticks")
    ) / TICKS_PER_SECOND


def m2ndp_seconds(row):
    cycles = _integer(row.get("measured_cycles"), "measured_cycles")
    period = _decimal(row.get("core_period_seconds"), "core period")
    return decimal.Decimal(cycles) * period


def recompute_speedup(vanilla_seconds, mechanism_seconds):
    vanilla_seconds = _decimal(vanilla_seconds, "Vanilla latency")
    mechanism_seconds = _decimal(mechanism_seconds, "mechanism latency")
    return vanilla_seconds / mechanism_seconds


def validate_vanilla_endpoints(rows, explanation=""):
    vanilla = {}
    for row in rows:
        if row.get("system") != "vanilla":
            continue
        latency = row.get("latency")
        if latency in vanilla:
            raise PublicationError(f"duplicate Vanilla row for {latency}")
        vanilla[latency] = row
    if set(vanilla) != set(LATENCIES):
        raise PublicationError(
            "Vanilla endpoint gate requires all four latency rows"
        )
    ticks_200ns = _integer(
        vanilla["200ns"].get("sim_ticks"), "Vanilla 200ns sim_ticks"
    )
    ticks_2us = _integer(
        vanilla["2us"].get("sim_ticks"), "Vanilla 2us sim_ticks"
    )
    delta = ticks_2us - ticks_200ns
    if delta <= 0:
        raise PublicationError(
            "Vanilla 2us ROI must be slower than Vanilla 200ns ROI"
        )
    explained = isinstance(explanation, str) and bool(explanation.strip())
    for field in REAL_CXL_FIELDS:
        values = [
            _integer(vanilla[latency].get(field), field)
            for latency in LATENCIES
        ]
        relative_range = decimal.Decimal(max(values) - min(values)) / \
            decimal.Decimal(min(values))
        if relative_range > decimal.Decimal("0.05") and not explained:
            raise PublicationError(
                f"{field} varies by more than 5 percent without explanation"
            )
    return delta


def _per_core(row, field):
    text = row.get(field, "")
    try:
        values = tuple(int(value) for value in text.split(";"))
    except (TypeError, ValueError) as error:
        raise PublicationError(
            f"four active balanced CIRA ports require valid {field}"
        ) from error
    if len(values) != 4 or any(value <= 0 for value in values):
        raise PublicationError(
            "four active balanced CIRA ports are required"
        )
    return values


def _validate_contract(row):
    expected = {
        "profile": "g4-4thread-sweep",
        "benchmark": "pr_spmv",
        "graph_sha256": profiles.G4_SHA256,
        "cores": "4",
        "threads": "4",
        "trials": "2",
        "measured_trial": "1",
        "iterations": "20",
        "all_memory_cxl": "True",
        "verification": "pass",
        "bit_exact": "pass",
    }
    for field, value in expected.items():
        if row.get(field) != value:
            raise PublicationError(
                f"{row.get('latency')}/{row.get('system')} "
                f"{field}={row.get(field)!r}, expected {value!r}"
            )
    for field in ("result_sha256", "source_sha256"):
        if not _HASH_RE.fullmatch(row.get(field, "")):
            raise PublicationError(f"{field} is not a SHA-256")


def _validate_activity(row):
    system = row["system"]
    if system == "amu":
        issued = _integer(row.get("asmc_loads"), "AMU issued loads")
        completed = _integer(
            row.get("asmc_completed"), "AMU completed loads"
        )
        if issued != completed:
            raise PublicationError(
                f"AMU issued/completed mismatch: {issued}/{completed}"
            )
    elif system == "cira":
        descriptors = sum(
            _integer(row.get(field), field, allow_zero=True)
            for field in (
                "cira_prefetches",
                "cira_indexed_prefetches",
                "cira_csr_prefetches",
            )
        )
        completed = _integer(row.get("cira_completed"), "CIRA completed")
        if descriptors <= 0 or completed <= 0:
            raise PublicationError("CIRA has no completed activity")
        issued_per_core = _per_core(row, "cira_issued_per_core")
        completed_per_core = _per_core(row, "cira_completed_per_core")
        _per_core(row, "cira_csr_per_core")
        if issued_per_core != completed_per_core:
            raise PublicationError(
                "four active balanced CIRA ports are required"
            )
        for field in (
            "cira_rejected_queue_full",
            "cira_dropped_csr_descriptors",
        ):
            if _integer(row.get(field), field, allow_zero=True) != 0:
                raise PublicationError(f"CIRA {field} must be zero")
        high_watermark = _integer(
            row.get("cira_csr_queue_high_watermark"),
            "CIRA CSR queue high watermark",
            allow_zero=True,
        )
        if high_watermark > 4096:
            raise PublicationError("CIRA CSR queue exceeded its bound")
    elif system == "m2ndp":
        if row.get("funcsim_compared") != "16":
            raise PublicationError("FuncSim compared count must be 16")
        if row.get("funcsim_mismatched") != "0":
            raise PublicationError("FuncSim is not bit-exact")
        if row.get("calibration_pass") != "pass":
            raise PublicationError("M2NDP calibration did not pass")
        if row.get("calibration_cxl_delay") != row["latency"]:
            raise PublicationError("M2NDP calibration latency mismatch")
        residual = _decimal(
            row.get("calibration_residual_ns"),
            "calibration residual",
            allow_zero=True,
        )
        link_period = _decimal(
            row.get("calibration_link_period_ns"),
            "calibration link period",
        )
        if residual > link_period:
            raise PublicationError(
                "calibration residual exceeds one link clock"
            )


def validate_matrix(rows, *, require_real_cxl=False, explanation=""):
    rows = [dict(row) for row in rows]
    if len(rows) != 16:
        raise PublicationError(
            f"publication requires exactly 16 rows, found {len(rows)}"
        )
    expected_keys = {
        (latency, system)
        for latency in LATENCIES
        for system in SYSTEMS
    }
    actual_keys = [
        (row.get("latency"), row.get("system")) for row in rows
    ]
    if len(set(actual_keys)) != len(actual_keys):
        raise PublicationError("publication matrix contains duplicate rows")
    if set(actual_keys) != expected_keys:
        raise PublicationError("publication matrix is not the exact 16 rows")

    by_key = {}
    for row in rows:
        _validate_contract(row)
        _validate_activity(row)
        system = row["system"]
        seconds = (
            m2ndp_seconds(row)
            if system == "m2ndp"
            else gem5_seconds(row)
        )
        stored_seconds = _decimal(
            row.get("latency_seconds"), "stored latency"
        )
        if stored_seconds != seconds:
            raise PublicationError(
                f"{row['latency']}/{system} stored latency mismatch"
            )
        by_key[(row["latency"], system)] = (row, seconds)

    for latency in LATENCIES:
        hashes = {
            by_key[(latency, system)][0]["result_sha256"]
            for system in SYSTEMS
        }
        if len(hashes) != 1:
            raise PublicationError(
                f"{latency} result vectors are not bit-exact"
            )
        vanilla_seconds = by_key[(latency, "vanilla")][1]
        for system in SYSTEMS:
            row, seconds = by_key[(latency, system)]
            expected_speedup = recompute_speedup(
                vanilla_seconds, seconds
            )
            stored_speedup = _decimal(
                row.get("speedup_vs_vanilla_cxl"), "stored speedup"
            )
            if stored_speedup != expected_speedup:
                raise PublicationError(
                    f"{latency}/{system} speedup mismatch"
                )

    if require_real_cxl:
        validate_vanilla_endpoints(rows, explanation=explanation)

    return tuple(
        by_key[(latency, system)][0]
        for latency in LATENCIES
        for system in SYSTEMS
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
    path = Path(path)
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise PublicationError(f"invalid {label}: {error}") from error
    if len(rows) != 1:
        raise PublicationError(
            f"{label} row count is {len(rows)}, expected 1"
        )
    return rows[0]


def _relative(path, root):
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(root).resolve()).as_posix()
    except ValueError as error:
        raise PublicationError(f"source escapes sweep root: {path}") from error


def _canonical_base(latency, system, source, result_sha256, root):
    return {
        "profile": "g4-4thread-sweep",
        "benchmark": "pr_spmv",
        "latency": latency,
        "system": system,
        "graph_sha256": profiles.G4_SHA256,
        "cores": "4",
        "threads": "4",
        "trials": "2",
        "measured_trial": "1",
        "iterations": "20",
        "all_memory_cxl": "True",
        "verification": "pass",
        "bit_exact": "pass",
        "result_sha256": result_sha256,
        "latency_seconds": "",
        "speedup_vs_vanilla_cxl": "",
        "sim_ticks": "",
        "measured_cycles": "",
        "core_period_seconds": "",
        "asmc_loads": "0",
        "asmc_completed": "0",
        "cira_prefetches": "0",
        "cira_completed": "0",
        "cira_indexed_prefetches": "0",
        "cira_csr_prefetches": "0",
        "cira_issued_per_core": "",
        "cira_completed_per_core": "",
        "cira_csr_per_core": "",
        "cira_rejected_queue_full": "0",
        "cira_dropped_csr_descriptors": "0",
        "cira_csr_queue_high_watermark": "0",
        "funcsim_compared": "",
        "funcsim_mismatched": "",
        "calibration_pass": "",
        "calibration_cxl_delay": "",
        "calibration_residual_ns": "",
        "calibration_link_period_ns": "",
        "source_path": _relative(source, root),
        "source_sha256": artifacts.sha256_file(source),
    }


def _typed_matched_row(row):
    typed = dict(row)
    for field in (
        "scale",
        "iterations",
        "measured_trial",
        "cores",
        "checkpoint_restores",
        "sim_ticks",
        "asmc_loads",
        "asmc_completed",
        "cira_prefetches",
        "cira_completed",
        "cira_indexed_prefetches",
        "cira_csr_prefetches",
    ):
        try:
            typed[field] = int(typed.get(field, ""))
        except (TypeError, ValueError) as error:
            raise PublicationError(
                f"matched {field} is not an integer"
            ) from error
    if typed.get("all_memory_cxl") not in {"True", True}:
        raise PublicationError("matched row is not all-memory-CXL")
    typed["all_memory_cxl"] = True
    return typed


def _verify_top_state(root):
    status_path = root / "status.json"
    state = _read_json(status_path, "sweep status")
    contract = state.get("contract", {})
    expected = {
        "profile": "g4-4thread-sweep",
        "graph_scale": 4,
        "graph_sha256": profiles.G4_SHA256,
        "cores": 4,
        "threads": 4,
        "trials": 2,
        "measured_trial": 1,
        "page_rank_iterations": 20,
        "all_memory_cxl": True,
    }
    for field, value in expected.items():
        if contract.get(field) != value:
            raise PublicationError(f"sweep contract {field} mismatch")
    source_hashes = {}
    for latency in LATENCIES:
        for system in SYSTEMS:
            record = state.get("latencies", {}).get(latency, {}).get(system)
            if not isinstance(record, dict) or record.get("status") != "passed":
                raise PublicationError(f"{latency}/{system} is not passed")
            output = Path(record.get("output", ""))
            if not output.is_absolute():
                output = root / output
            expected_output = (
                root / "runs" / latency / "m2ndp/gem5/run/summary.csv"
                if system == "vanilla"
                else root / "runs" / latency / "m2ndp/summary.csv"
                if system == "m2ndp"
                else root / "runs" / latency / system / "summary.csv"
            )
            if output.resolve() != expected_output.resolve():
                raise PublicationError(
                    f"{latency}/{system} output path mismatch"
                )
            actual = artifacts.sha256_file(output)
            if actual != record.get("output_sha256"):
                raise PublicationError(
                    f"{latency}/{system} output hash mismatch"
                )
            source_hashes[f"{latency}/{system}"] = actual
    return status_path, source_hashes


def collect_rows(sweep_root):
    root = Path(sweep_root).resolve()
    status_path, source_hashes = _verify_top_state(root)
    profile = profiles.get_profile("g4-4thread-sweep")
    rows = []
    artifact_hashes = {}
    for latency in LATENCIES:
        latency_root = root / "runs" / latency
        m2ndp_root = latency_root / "m2ndp"
        reference = m2ndp_root / "reference/scores.raw"
        dump = m2ndp_root / "funcsim/scores.u32"
        if reference.stat().st_size != profile.num_nodes * 4:
            raise PublicationError("Vanilla reference vector length mismatch")
        if dump.stat().st_size != profile.num_nodes * 4:
            raise PublicationError("FuncSim result vector length mismatch")
        result_sha256 = artifacts.sha256_file(reference)
        if artifacts.sha256_file(dump) != result_sha256:
            raise PublicationError(f"{latency} FuncSim is not bit-exact")
        artifact_hashes[f"{latency}/reference"] = result_sha256
        artifact_hashes[f"{latency}/funcsim_dump"] = result_sha256

        vanilla_source = m2ndp_root / "gem5/run/summary.csv"
        vanilla_evidence = m2ndp_results.parse_gem5_summary(
            vanilla_source, profile=profile, latency=latency
        )
        vanilla = _canonical_base(
            latency,
            "vanilla",
            vanilla_source,
            result_sha256,
            root,
        )
        vanilla["sim_ticks"] = str(vanilla_evidence.sim_ticks)
        vanilla["latency_seconds"] = str(gem5_seconds(vanilla))
        vanilla_run = Path(vanilla_evidence.row.get("run_dir", ""))
        matched_runner.validate_config_delay(
            vanilla_run / "config.ini", latency
        )
        rows.append(vanilla)

        for system in ("amu", "cira"):
            source = latency_root / system / "summary.csv"
            source_row = _single_csv(source, f"{latency}/{system} summary")
            typed = _typed_matched_row(source_row)
            matched_runner.validate_row(
                typed,
                system,
                smoke_test=False,
                profile_name=profile.name,
                latency=latency,
            )
            matched_runner.validate_config_delay(
                Path(source_row.get("run_dir", "")) / "config.ini",
                latency,
            )
            run_evidence = _read_json(
                latency_root / system / "evidence.json",
                f"{latency}/{system} evidence",
            )
            if (
                run_evidence.get("profile") != profile.name
                or run_evidence.get("cxl_link_delay") != latency
                or run_evidence.get("graph_sha256") != profiles.G4_SHA256
                or run_evidence.get("runs", {})
                .get(system, {})
                .get("reference_raw_sha256")
                != result_sha256
            ):
                raise PublicationError(
                    f"{latency}/{system} bit-exact evidence mismatch"
                )
            row = _canonical_base(
                latency, system, source, result_sha256, root
            )
            row["sim_ticks"] = source_row["sim_ticks"]
            row["latency_seconds"] = str(gem5_seconds(row))
            for field in (
                "asmc_loads",
                "asmc_completed",
                "cira_prefetches",
                "cira_completed",
                "cira_indexed_prefetches",
                "cira_csr_prefetches",
                "cira_issued_per_core",
                "cira_completed_per_core",
                "cira_csr_per_core",
                "cira_rejected_queue_full",
                "cira_dropped_csr_descriptors",
                "cira_csr_queue_high_watermark",
            ):
                row[field] = source_row.get(field, row[field])
            rows.append(row)

        m2_source = m2ndp_root / "summary.csv"
        m2_source_row = _single_csv(m2_source, f"{latency}/M2NDP summary")
        expected_m2 = {
            "profile": profile.name,
            "benchmark": "pr_spmv",
            "graph_sha256": profiles.G4_SHA256,
            "iterations": "20",
            "trials": "2",
            "measured_trial": "1",
            "cores": "4",
            "all_memory_cxl": "True",
            "cxl_link_delay": latency,
            "verification": "pass",
            "funcsim_strict": "pass",
            "funcsim_compared": "16",
        }
        for field, value in expected_m2.items():
            if m2_source_row.get(field) != value:
                raise PublicationError(
                    f"{latency}/M2NDP {field} mismatch"
                )
        calibration_path = m2ndp_root / "calibration/calibration.json"
        calibrated = _read_json(calibration_path, f"{latency} calibration")
        m2 = _canonical_base(
            latency, "m2ndp", m2_source, result_sha256, root
        )
        m2["measured_cycles"] = m2_source_row.get(
            "ndpsim_measured_cycles", ""
        )
        m2["core_period_seconds"] = m2_source_row.get(
            "ndpsim_core_period_seconds", ""
        )
        m2["latency_seconds"] = str(m2ndp_seconds(m2))
        if _decimal(
            m2_source_row.get("m2ndp_seconds"), "M2NDP stored latency"
        ) != _decimal(m2["latency_seconds"], "M2NDP latency"):
            raise PublicationError(f"{latency}/M2NDP latency mismatch")
        m2["funcsim_compared"] = m2_source_row["funcsim_compared"]
        m2["funcsim_mismatched"] = "0"
        m2["calibration_pass"] = (
            "pass" if calibrated.get("passed") is True else "failed"
        )
        m2["calibration_cxl_delay"] = calibrated.get("cxl_delay", "")
        m2["calibration_residual_ns"] = calibrated.get(
            "residual_ns", ""
        )
        m2["calibration_link_period_ns"] = calibrated.get(
            "link_period_ns", ""
        )
        rows.append(m2)
        artifact_hashes[f"{latency}/calibration"] = artifacts.sha256_file(
            calibration_path
        )

    seconds = {
        (row["latency"], row["system"]): _decimal(
            row["latency_seconds"], "latency"
        )
        for row in rows
    }
    for row in rows:
        row["speedup_vs_vanilla_cxl"] = str(
            recompute_speedup(
                seconds[(row["latency"], "vanilla")],
                seconds[(row["latency"], row["system"])],
            )
        )
    validated = validate_matrix(rows)
    return validated, {
        "status_path": _relative(status_path, root),
        "status_sha256": artifacts.sha256_file(status_path),
        "source_sha256": source_hashes,
        "artifact_sha256": artifact_hashes,
    }


def render_tex(rows):
    labels = {
        "vanilla": "Vanilla CXL",
        "amu": "AMU",
        "cira": "CIRA",
        "m2ndp": r"M$^2$NDP",
    }
    lines = [
        r"\begin{tabular}{llrr}",
        r"\toprule",
        r"CXL latency & System & ROI ($\mu$s) & Speedup \\",
        r"\midrule",
    ]
    for index, row in enumerate(rows):
        latency = row["latency"].replace("us", r"\,$\mu$s")
        roi_us = decimal.Decimal(row["latency_seconds"]) * decimal.Decimal(
            10**6
        )
        speedup = decimal.Decimal(row["speedup_vs_vanilla_cxl"])
        lines.append(
            f"{latency} & {labels[row['system']]} & "
            f"{roi_us:.3f} & {speedup:.3f}\\,$\\times$ \\\\"
        )
        if (index + 1) % len(SYSTEMS) == 0 and index + 1 != len(rows):
            lines.append(r"\midrule")
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    return "\n".join(lines) + "\n"


def _atomic_write_text(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publication_paths(root):
    root = Path(root)
    return PublicationPaths(
        root=root,
        csv=root / CSV_NAME,
        evidence=root / EVIDENCE_NAME,
        tex=root / TEX_NAME,
    )


def publish(rows, output_root, *, evidence=None):
    rows = validate_matrix(rows)
    for row in rows:
        missing = [field for field in FIELDNAMES if field not in row]
        if missing:
            raise PublicationError(
                "canonical row missing fields: " + ", ".join(missing)
            )
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".published.", dir=output_root)
    )
    staged = _publication_paths(staging)
    try:
        artifacts.atomic_write_csv(
            staged.csv,
            FIELDNAMES,
            [{field: row[field] for field in FIELDNAMES} for row in rows],
        )
        _atomic_write_text(staged.tex, render_tex(rows))
        payload = {
            "schema": 1,
            "profile": "g4-4thread-sweep",
            "graph_sha256": profiles.G4_SHA256,
            "row_count": len(rows),
            "csv_sha256": artifacts.sha256_file(staged.csv),
            "rows": list(rows),
            "source_evidence": evidence or {},
        }
        artifacts.atomic_write_json(staged.evidence, payload)
        with staged.csv.open(newline="", encoding="utf-8") as stream:
            reloaded = list(csv.DictReader(stream))
        validate_matrix(reloaded)
        loaded_evidence = json.loads(staged.evidence.read_text())
        if (
            loaded_evidence.get("row_count") != 16
            or loaded_evidence.get("csv_sha256")
            != artifacts.sha256_file(staged.csv)
        ):
            raise PublicationError("published evidence failed reload")
        _fsync_directory(staging)

        published = output_root / "published"
        backup = output_root / f".published.backup.{os.getpid()}"
        if backup.exists():
            raise PublicationError(f"publication backup exists: {backup}")
        had_previous = published.exists()
        if had_previous:
            os.replace(published, backup)
        try:
            os.replace(staging, published)
        except BaseException:
            if had_previous:
                os.replace(backup, published)
            raise
        if had_previous:
            shutil.rmtree(backup)
        _fsync_directory(output_root)
        return _publication_paths(published)
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
    sweep_root = args.sweep_root.resolve()
    output_root = (
        args.output_root.resolve()
        if args.output_root is not None
        else sweep_root
    )
    try:
        rows, evidence = collect_rows(sweep_root)
        paths = publish(rows, output_root, evidence=evidence)
        print(f"Wrote {paths.csv}")
        print(f"Wrote {paths.evidence}")
        print(f"Wrote {paths.tex}")
        return 0
    except (
        PublicationError,
        artifacts.EvidenceError,
        matched_runner.VariantRunError,
        profiles.ProfileError,
        OSError,
        KeyError,
    ) as error:
        print(f"G4_PUBLICATION_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
