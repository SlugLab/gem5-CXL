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
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
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
_GEM5_CONTRACT = {
    "benchmark": "pr_spmv",
    "kind": "baseline",
    "status": "ok",
    "verification": "pass",
    "roi_cpu": "timing",
    "cores": "2",
    "cxl_link_delay": "1us",
    "all_memory_cxl": "True",
    "graph_sha256": artifacts.EXPECTED_G20_SHA256,
    "iterations": "2",
    "measured_trial": "1",
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
    start = _single_match(
        log,
        r"K0_INIT_TRIAL1[^\n]*?\b(?:at\s+)?cycle\s+(\d+)",
        "K0_INIT_TRIAL1",
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


def _validate_gem5_row(row):
    for field, expected in _GEM5_CONTRACT.items():
        actual = row.get(field)
        if actual != expected:
            raise artifacts.EvidenceError(
                f"gem5 {field}={actual!r}, expected {expected!r}"
            )
    ticks_text = row.get("sim_ticks", "")
    if not re.fullmatch(r"[0-9]+", ticks_text):
        raise artifacts.EvidenceError(
            f"gem5 sim_ticks is not a positive integer: {ticks_text!r}"
        )
    ticks = int(ticks_text)
    if ticks <= 0:
        raise artifacts.EvidenceError("gem5 sim_ticks must be positive")
    return ticks


def parse_gem5_summary(path):
    path = Path(path)
    if not path.is_file():
        raise artifacts.EvidenceError(f"gem5 summary is missing: {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise artifacts.EvidenceError(
            f"gem5 summary row count is {len(rows)}, expected 1"
        )
    ticks = _validate_gem5_row(rows[0])
    return Gem5Evidence(dict(rows[0]), ticks)


def _require_decimal_positive(value, name, *, allow_zero=False):
    value = decimal.Decimal(value)
    if not value.is_finite():
        raise artifacts.EvidenceError(f"{name} must be finite")
    if value < 0 or (not allow_zero and value == 0):
        raise artifacts.EvidenceError(f"{name} must be positive")
    return value


def _validate_provenance(provenance, calibration, funcsim, gem5):
    if provenance is None:
        raise artifacts.EvidenceError("provenance evidence is missing")
    for field in dataclasses.fields(provenance):
        value = getattr(provenance, field.name)
        if not _HASH_RE.fullmatch(value):
            raise artifacts.EvidenceError(
                f"provenance {field.name} is not a SHA-256"
            )
    if provenance.graph_sha256 != artifacts.EXPECTED_G20_SHA256:
        raise artifacts.EvidenceError("provenance graph hash is not g20")
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
):
    sim_ticks = _validate_gem5_row(gem5.row)
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
        provenance, calibration, funcsim, gem5
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
        "graph_sha256": provenance.graph_sha256,
        "gem5_binary_sha256": provenance.gem5_binary_sha256,
        "m2ndp_patch_sha256": provenance.m2ndp_patch_sha256,
        "m2ndp_config_sha256": provenance.m2ndp_config_sha256,
        "trace_sha256": provenance.trace_sha256,
        "iterations": "20",
        "trials": "2",
        "measured_trial": "1",
        "cores": "2",
        "all_memory_cxl": "True",
        "cxl_link_delay": "1us",
        "verification": "pass",
        "funcsim_strict": "pass",
        "funcsim_compared": str(funcsim.compared),
        "gem5_sim_ticks": str(sim_ticks),
        "ndpsim_start_cycle": str(ndpsim.start_cycle),
        "ndpsim_end_cycle": str(ndpsim.end_cycle),
        "ndpsim_measured_cycles": str(ndpsim.measured_cycles),
        "ndpsim_core_period_seconds": str(core_period),
        "gem5_seconds": str(gem5_seconds),
        "m2ndp_seconds": str(m2ndp_seconds),
        "speedup": str(speedup),
    }
