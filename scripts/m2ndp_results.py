#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Fail-closed parsers and publication gates for M2NDP PageRank runs."""

import csv
import dataclasses
import decimal
import re
from pathlib import Path

try:
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts


@dataclasses.dataclass(frozen=True)
class FuncSimEvidence:
    passed: bool
    compared: int
    mismatched: int
    dump_sha256: str


@dataclasses.dataclass(frozen=True)
class NDPSimEvidence:
    start_cycle: int
    end_cycle: int
    measured_cycles: int
    core_period_seconds: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class Gem5Evidence:
    row: dict
    sim_ticks: int


@dataclasses.dataclass(frozen=True)
class CalibrationEvidence:
    passed: bool
    request_bytes: int
    target_ns: decimal.Decimal
    measured_ns: decimal.Decimal
    residual_ns: decimal.Decimal
    link_period_ns: decimal.Decimal
    config_sha256: str
    profile: str = ""
    cxl_link_delay: str = ""
    profile_manifest_sha256: str = ""
    gem5_microprobe_ns: decimal.Decimal | None = None
    m2ndp_boundary_ns: decimal.Decimal | None = None


@dataclasses.dataclass(frozen=True)
class ProvenanceEvidence:
    graph_sha256: str
    gem5_binary_sha256: str
    trace_sha256: str
    m2ndp_patch_sha256: str
    m2ndp_config_sha256: str
    reference_raw_sha256: str
    funcsim_dump_sha256: str


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REAL_CXL_FIELDS = (
    "mem_ctrl_read_reqs",
    "mem_ctrl_read_bursts",
    "mem_ctrl_bytes_read",
    "mem_ctrl_cpu_data_reads",
)
FORMAL_PROFILE = "pr-offload-4thread-1us"


def validate_formal_result(result):
    if result.get("profile") != FORMAL_PROFILE:
        raise artifacts.EvidenceError(
            "M2NDP result is not the formal PR profile"
        )
    try:
        logical_partitions = int(result.get("logical_partitions"))
    except (TypeError, ValueError) as error:
        raise artifacts.EvidenceError(
            "M2NDP trace is not four-way partitioned"
        ) from error
    if logical_partitions != 4:
        raise artifacts.EvidenceError(
            "M2NDP trace is not four-way partitioned"
        )
    return result


def expected_gem5_contract(profile, latency):
    profiles.require_latency(profile, latency)
    return {
        "benchmark": "pr_spmv",
        "kind": "baseline",
        "status": "ok",
        "verification": "pass",
        "roi_cpu": "timing",
        "cores": str(profile.cores),
        "cxl_link_delay": latency,
        "all_memory_cxl": "True",
        "graph_sha256": profile.graph_sha256,
        "iterations": str(profile.trials),
        "measured_trial": str(profile.measured_trial),
        "checkpoint_restores": "1",
    }


def _single_match(text, pattern, name, *, flags=0):
    matches = list(re.finditer(pattern, text, flags))
    if len(matches) != 1:
        raise artifacts.EvidenceError(
            f"{name} marker count is {len(matches)}, expected 1"
        )
    return matches[0]


def parse_funcsim(
    log,
    *,
    returncode,
    expected_count,
    dump_path=None,
    reference_path=None,
):
    if returncode != 0:
        raise artifacts.EvidenceError(
            f"FuncSim exit status {returncode}, expected 0"
        )
    patterns = (
        ("MODE", r"^M2NDP_STRICT_MODE=(\S+)\s*$"),
        ("COMPARED", r"^M2NDP_STRICT_COMPARED=(\d+)\s*$"),
        ("MISMATCHED", r"^M2NDP_STRICT_MISMATCHED=(\d+)\s*$"),
        ("MATCH", r"^M2NDP_STRICT_MATCH=(\S+)\s*$"),
    )
    found = [
        _single_match(log, pattern, name, flags=re.MULTILINE)
        for name, pattern in patterns
    ]
    if [match.start() for match in found] != sorted(
        match.start() for match in found
    ):
        raise artifacts.EvidenceError("FuncSim strict markers are reordered")
    mode = found[0].group(1)
    compared = int(found[1].group(1))
    mismatched = int(found[2].group(1))
    match_status = found[3].group(1)
    if mode != "1":
        raise artifacts.EvidenceError(
            f"FuncSim strict mode is {mode}, expected 1"
        )
    if compared != expected_count:
        raise artifacts.EvidenceError(
            f"FuncSim compared {compared}, expected {expected_count}"
        )
    if mismatched != 0:
        raise artifacts.EvidenceError(
            f"FuncSim reported {mismatched} mismatches"
        )
    if match_status != "PASS":
        raise artifacts.EvidenceError(
            f"FuncSim strict match is {match_status}, expected PASS"
        )
    if (dump_path is None) != (reference_path is None):
        raise artifacts.EvidenceError(
            "FuncSim dump and reference paths must be provided together"
        )
    dump_sha256 = ""
    if dump_path is not None:
        dump_sha256 = validate_reference_dump(
            reference_path, dump_path, expected_count=expected_count
        )
    return FuncSimEvidence(True, compared, mismatched, dump_sha256)


def validate_reference_dump(reference, actual, *, expected_count):
    reference = Path(reference)
    actual = Path(actual)
    expected_size = expected_count * 4
    for label, path in (("reference", reference), ("FuncSim dump", actual)):
        if not path.is_file():
            raise artifacts.EvidenceError(f"{label} is missing: {path}")
        if path.stat().st_size != expected_size:
            raise artifacts.EvidenceError(
                f"{label} size is {path.stat().st_size}, "
                f"expected {expected_size}"
            )
    byte_offset = 0
    with reference.open("rb") as expected_stream, actual.open(
        "rb"
    ) as actual_stream:
        while True:
            expected_chunk = expected_stream.read(1024 * 1024)
            actual_chunk = actual_stream.read(1024 * 1024)
            if expected_chunk != actual_chunk:
                mismatch = next(
                    index
                    for index, pair in enumerate(
                        zip(expected_chunk, actual_chunk)
                    )
                    if pair[0] != pair[1]
                )
                word = (byte_offset + mismatch) // 4
                raise artifacts.EvidenceError(
                    f"FuncSim dump is not bit-exact at float32 index {word}"
                )
            if not expected_chunk:
                break
            byte_offset += len(expected_chunk)
    return artifacts.sha256_file(actual)


def parse_ndpsim(log, *, returncode=0, output_text=None):
    if returncode != 0:
        raise artifacts.EvidenceError(
            f"NDPSim exit status {returncode}, expected 0"
        )
    formal_marker = re.search(
        r"K2_CONTRIB_TRIAL1_PART0[^\n]*?\b(?:at\s+)?cycle\s+(\d+)",
        log,
        re.IGNORECASE,
    )
    if formal_marker is not None:
        start = _single_match(
            log,
            r"K2_CONTRIB_TRIAL1_PART0[^\n]*?\b(?:at\s+)?cycle\s+(\d+)",
            "K2_CONTRIB_TRIAL1_PART0",
            flags=re.IGNORECASE,
        )
    else:
        start = _single_match(
            log,
            r"K0_INIT_TRIAL1[^\n]*?\b(?:at\s+)?cycle\s+(\d+)",
            "legacy K0_INIT_TRIAL1",
            flags=re.IGNORECASE,
        )
    finish = _single_match(
        log,
        r"\bEXPR\s+FINISHED\s+(\d+)\b",
        "EXPR FINISHED",
    )
    if finish.start() <= start.start():
        raise artifacts.EvidenceError(
            "NDPSim trial-1 timing markers are reordered"
        )
    start_cycle = int(start.group(1))
    end_cycle = int(finish.group(1))
    measured_cycles = end_cycle - start_cycle
    if measured_cycles <= 0:
        raise artifacts.EvidenceError(
            "NDPSim measured cycle count must be positive"
        )
    period_source = log if output_text is None else output_text
    period = _single_match(
        period_source,
        r"\bCORE\s+period:\s*"
        r"([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)",
        "CORE period",
    )
    try:
        core_period = decimal.Decimal(period.group(1))
    except decimal.InvalidOperation as error:
        raise artifacts.EvidenceError(
            "NDPSim core period is not decimal"
        ) from error
    if not core_period.is_finite() or core_period <= 0:
        raise artifacts.EvidenceError(
            "NDPSim core period must be positive and finite"
        )
    return NDPSimEvidence(
        start_cycle,
        end_cycle,
        measured_cycles,
        core_period,
    )


def validate_gem5_row(row, *, profile, latency, smoke_test=False):
    contract = expected_gem5_contract(profile, latency)
    if smoke_test:
        contract.pop("graph_sha256")
    for field, expected in contract.items():
        actual = row.get(field)
        if actual != expected:
            raise artifacts.EvidenceError(
                f"gem5 {field}={actual!r}, expected {expected!r}"
            )
    scale = row.get("scale")
    if (
        not smoke_test
        and scale not in (None, "")
        and scale != str(profile.graph_scale)
    ):
        raise artifacts.EvidenceError(
            f"gem5 scale={scale!r}, expected {profile.graph_scale!r}"
        )
    graph_sha256 = row.get("graph_sha256", "")
    if not _HASH_RE.fullmatch(graph_sha256):
        raise artifacts.EvidenceError(
            "gem5 graph_sha256 is not a SHA-256"
        )
    ticks_text = row.get("sim_ticks", "")
    if not re.fullmatch(r"[0-9]+", ticks_text):
        raise artifacts.EvidenceError(
            f"gem5 sim_ticks is not a positive integer: {ticks_text!r}"
        )
    ticks = int(ticks_text)
    if ticks <= 0:
        raise artifacts.EvidenceError("gem5 sim_ticks must be positive")
    if not smoke_test and profile.name in profiles.FROZEN_PROFILE_CONTRACTS:
        validate_real_cxl_row(row)
    return ticks


def validate_real_cxl_row(row):
    evidence = {}
    for field in REAL_CXL_FIELDS:
        value = row.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
            raise artifacts.EvidenceError(
                f"gem5 {field} is not a positive integer: {value!r}"
            )
        parsed = decimal.Decimal(value)
        if parsed <= 0:
            raise artifacts.EvidenceError(
                f"gem5 {field} must be positive in measured ROI"
            )
        evidence[field] = parsed
    return evidence


def parse_gem5_summary(
    path,
    *,
    profile=None,
    latency="1us",
    smoke_test=False,
):
    if profile is None:
        profile = profiles.get_legacy_diagnostic_profile(
            "g20-2thread-1us"
        )
    path = Path(path)
    if not path.is_file():
        raise artifacts.EvidenceError(f"gem5 summary is missing: {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise artifacts.EvidenceError(
            f"gem5 summary row count is {len(rows)}, expected 1"
        )
    ticks = validate_gem5_row(
        rows[0],
        profile=profile,
        latency=latency,
        smoke_test=smoke_test,
    )
    return Gem5Evidence(dict(rows[0]), ticks)


def _require_decimal_positive(value, name, *, allow_zero=False):
    try:
        value = decimal.Decimal(value)
    except (decimal.InvalidOperation, TypeError, ValueError) as error:
        raise artifacts.EvidenceError(
            f"{name} must be a decimal number"
        ) from error
    if not value.is_finite():
        raise artifacts.EvidenceError(f"{name} must be finite")
    if value < 0 or (not allow_zero and value == 0):
        raise artifacts.EvidenceError(f"{name} must be positive")
    return value


def validate_calibration_binding(
    calibration, *, profile, latency, profile_manifest_sha256
):
    if (
        profile.name not in profiles.FROZEN_PROFILE_CONTRACTS
        and profile.name != FORMAL_PROFILE
    ):
        return
    if calibration.profile != profile.name:
        raise artifacts.EvidenceError("calibration profile binding mismatch")
    if calibration.cxl_link_delay != latency:
        raise artifacts.EvidenceError("calibration latency binding mismatch")
    if (
        not _HASH_RE.fullmatch(calibration.profile_manifest_sha256)
        or calibration.profile_manifest_sha256 != profile_manifest_sha256
    ):
        raise artifacts.EvidenceError(
            "calibration profile manifest binding mismatch"
        )
    microprobe = _require_decimal_positive(
        calibration.gem5_microprobe_ns, "gem5 microprobe latency"
    )
    boundary = _require_decimal_positive(
        calibration.m2ndp_boundary_ns, "M2NDP boundary latency"
    )
    if microprobe != calibration.target_ns:
        raise artifacts.EvidenceError(
            "calibration gem5 microprobe does not match target"
        )
    if boundary != calibration.measured_ns:
        raise artifacts.EvidenceError(
            "calibration M2NDP boundary does not match measurement"
        )
    if abs(boundary - microprobe) > decimal.Decimal("0.125"):
        raise artifacts.EvidenceError(
            "calibration profile-bound residual exceeds 0.125 ns"
        )


def _validate_provenance(
    provenance,
    calibration,
    funcsim,
    gem5,
    *,
    profile,
    smoke_test=False,
):
    if provenance is None:
        raise artifacts.EvidenceError("provenance evidence is missing")
    for field in dataclasses.fields(provenance):
        value = getattr(provenance, field.name)
        if not _HASH_RE.fullmatch(value):
            raise artifacts.EvidenceError(
                f"provenance {field.name} is not a SHA-256"
            )
    if (
        not smoke_test
        and provenance.graph_sha256 != profile.graph_sha256
    ):
        raise artifacts.EvidenceError(
            "provenance graph hash does not match profile"
        )
    if provenance.graph_sha256 != gem5.row["graph_sha256"]:
        raise artifacts.EvidenceError("gem5/provenance graph hash mismatch")
    if provenance.m2ndp_config_sha256 != calibration.config_sha256:
        raise artifacts.EvidenceError(
            "M2NDP config is outside the accepted calibration"
        )
    if (
        provenance.reference_raw_sha256
        != provenance.funcsim_dump_sha256
    ):
        raise artifacts.EvidenceError(
            "FuncSim dump/reference hash mismatch"
        )
    if (
        funcsim.dump_sha256
        and funcsim.dump_sha256 != provenance.funcsim_dump_sha256
    ):
        raise artifacts.EvidenceError(
            "FuncSim evidence/provenance dump hash mismatch"
        )


def build_summary(
    *,
    gem5,
    funcsim,
    ndpsim,
    calibration,
    provenance=None,
    profile=None,
    latency="1us",
    smoke_test=False,
    profile_manifest_sha256=None,
):
    if profile is None:
        profile = profiles.get_legacy_diagnostic_profile(
            "g20-2thread-1us"
        )
    sim_ticks = validate_gem5_row(
        gem5.row,
        profile=profile,
        latency=latency,
        smoke_test=smoke_test,
    )
    if sim_ticks != gem5.sim_ticks:
        raise artifacts.EvidenceError(
            "gem5 evidence sim_ticks does not match its summary row"
        )
    if not funcsim.passed or funcsim.mismatched != 0:
        raise artifacts.EvidenceError("FuncSim strict validation failed")
    if funcsim.compared <= 0:
        raise artifacts.EvidenceError(
            "FuncSim compared-element count must be positive"
        )
    if (
        ndpsim.measured_cycles <= 0
        or ndpsim.measured_cycles
        != ndpsim.end_cycle - ndpsim.start_cycle
    ):
        raise artifacts.EvidenceError("NDPSim timing evidence is invalid")
    core_period = _require_decimal_positive(
        ndpsim.core_period_seconds, "NDPSim core period"
    )
    if not calibration.passed:
        raise artifacts.EvidenceError("CXL calibration failed")
    validate_calibration_binding(
        calibration,
        profile=profile,
        latency=latency,
        profile_manifest_sha256=profile_manifest_sha256,
    )
    if calibration.request_bytes != 64:
        raise artifacts.EvidenceError(
            "CXL calibration request size must be 64 bytes"
        )
    target_ns = _require_decimal_positive(
        calibration.target_ns, "calibration target"
    )
    measured_ns = _require_decimal_positive(
        calibration.measured_ns, "calibration measurement"
    )
    residual_ns = _require_decimal_positive(
        calibration.residual_ns,
        "calibration residual",
        allow_zero=True,
    )
    link_period_ns = _require_decimal_positive(
        calibration.link_period_ns, "calibration link period"
    )
    if residual_ns != abs(measured_ns - target_ns):
        raise artifacts.EvidenceError(
            "calibration residual does not match measured-target"
        )
    if residual_ns > link_period_ns:
        raise artifacts.EvidenceError(
            "calibration residual exceeds one link clock"
        )
    _validate_provenance(
        provenance,
        calibration,
        funcsim,
        gem5,
        profile=profile,
        smoke_test=smoke_test,
    )
    gem5_seconds = decimal.Decimal(sim_ticks) / decimal.Decimal(10**12)
    m2ndp_seconds = decimal.Decimal(
        ndpsim.measured_cycles
    ) * core_period
    speedup = gem5_seconds / m2ndp_seconds
    for value, name in (
        (gem5_seconds, "gem5 time"),
        (m2ndp_seconds, "M2NDP time"),
        (speedup, "speedup"),
    ):
        if not value.is_finite() or value <= 0:
            raise artifacts.EvidenceError(
                f"{name} must be positive and finite"
            )
    return {
        "benchmark": "pr_spmv",
        "profile": profile.name,
        "profile_manifest_sha256": profile_manifest_sha256 or "",
        "graph_sha256": provenance.graph_sha256,
        "gem5_binary_sha256": provenance.gem5_binary_sha256,
        "m2ndp_patch_sha256": provenance.m2ndp_patch_sha256,
        "m2ndp_config_sha256": provenance.m2ndp_config_sha256,
        "trace_sha256": provenance.trace_sha256,
        "reference_raw_sha256": provenance.reference_raw_sha256,
        "funcsim_dump_sha256": provenance.funcsim_dump_sha256,
        "iterations": str(profile.page_rank_iterations),
        "trials": str(profile.trials),
        "measured_trial": str(profile.measured_trial),
        "cores": str(profile.cores),
        "logical_partitions": str(
            getattr(profile, "logical_partitions", 1)
        ),
        "all_memory_cxl": "True",
        "cxl_link_delay": latency,
        "gem5_microprobe_ns": str(calibration.target_ns),
        "m2ndp_boundary_ns": str(calibration.measured_ns),
        "verification": "pass",
        "funcsim_strict": "pass",
        "funcsim_compared": str(funcsim.compared),
        **{
            field: gem5.row.get(field, "") for field in REAL_CXL_FIELDS
        },
        "gem5_sim_ticks": str(sim_ticks),
        "ndpsim_start_cycle": str(ndpsim.start_cycle),
        "ndpsim_end_cycle": str(ndpsim.end_cycle),
        "ndpsim_measured_cycles": str(ndpsim.measured_cycles),
        "ndpsim_core_period_seconds": str(core_period),
        "gem5_seconds": str(gem5_seconds),
        "m2ndp_seconds": str(m2ndp_seconds),
        "speedup": str(speedup),
    }
