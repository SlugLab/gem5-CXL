#!/usr/bin/env python3
"""Fail-closed normalization for the 24-cell timing evidence campaign."""

from __future__ import annotations

import dataclasses
import decimal
import hashlib
import json
import re
from pathlib import Path


WORKLOADS = (
    "pr_spmv",
    "gap_bc",
    "mcf",
    "amg_gather",
    "lulesh_scatter",
    "npb_cg",
)
LATENCIES = ("200ns", "500ns", "1us", "2us")
COORDINATES = tuple(
    (workload, latency)
    for workload in WORKLOADS
    for latency in LATENCIES
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CYCLE_MARKER = re.compile(r"EXPR FINISHED\s+(\d+)")


class EvidenceError(RuntimeError):
    """Raised when evidence is incomplete, inconsistent, or stale."""


@dataclasses.dataclass(frozen=True)
class CalibrationRow:
    latency: str
    gem5_round_trip_ns: str
    selected_link_latency: int
    core_period_ns: str
    link_period_ns: str
    m2ndp_round_trip_ns: str
    residual_ns: str
    residual_ps: str
    evidence_path: str
    evidence_sha256: str


def sha256_file(path: Path) -> str:
    path = Path(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise EvidenceError(f"required evidence file is unavailable: {path}") from error
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict:
    path = Path(path).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"{label} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return value


def _decimal(value, label: str, *, nonnegative: bool = True) -> decimal.Decimal:
    if not isinstance(value, str):
        raise EvidenceError(f"{label} must be an exact decimal string")
    try:
        result = decimal.Decimal(value)
    except decimal.InvalidOperation as error:
        raise EvidenceError(f"{label} must be an exact decimal string") from error
    if not result.is_finite() or (nonnegative and result < 0):
        raise EvidenceError(f"{label} must be a finite nonnegative decimal")
    return result


def _canonical_decimal(value: decimal.Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _integer(row: dict, field: str, *, positive: bool = False) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"{field} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise EvidenceError(f"{field} must be a {qualifier} integer")
    return value


def _digest(row: dict, field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceError(f"{field} is not a SHA-256 digest")
    return value


def _verify_path_hash(row: dict, path_field: str, hash_field: str, label: str) -> Path:
    raw_path = row.get(path_field)
    if not isinstance(raw_path, str) or not raw_path:
        raise EvidenceError(f"{label} path is missing")
    path = Path(raw_path).resolve()
    expected = _digest(row, hash_field)
    if sha256_file(path) != expected:
        raise EvidenceError(f"{label} SHA-256 differs")
    return path


def cycles_to_ns(cycles: int, period_ns: str) -> str:
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
        raise EvidenceError("cycles must be a nonnegative integer")
    period = _decimal(period_ns, "period_ns")
    if period <= 0:
        raise EvidenceError("period_ns must be positive")
    return _canonical_decimal(decimal.Decimal(cycles) * period)


def ticks_to_ns(ticks: int, sim_freq_hz: int) -> str:
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise EvidenceError("ticks must be a nonnegative integer")
    if (
        isinstance(sim_freq_hz, bool)
        or not isinstance(sim_freq_hz, int)
        or sim_freq_hz <= 0
    ):
        raise EvidenceError("sim_freq_hz must be a positive integer")
    numerator = decimal.Decimal(ticks) * decimal.Decimal(1_000_000_000)
    result = numerator / decimal.Decimal(sim_freq_hz)
    if result * decimal.Decimal(sim_freq_hz) != numerator:
        raise EvidenceError("tick-to-nanosecond conversion is not exactly representable")
    return _canonical_decimal(result)


def _calibration_row(
    record: dict, *, source: Path | None, verify_config: bool,
) -> CalibrationRow:
    if record.get("schema") != 1 or record.get("passed") is not True:
        raise EvidenceError("calibration status is not PASS")
    latency = record.get("cxl_link_delay")
    if latency not in LATENCIES or record.get("cxl_delay") != latency:
        raise EvidenceError("calibration latency identity differs")
    selected = _integer(record, "selected_link_latency", positive=True)
    core_period = record.get("core_period_ns")
    link_period = record.get("link_period_ns")
    _decimal(core_period, "calibration core_period_ns")
    _decimal(link_period, "calibration link_period_ns")
    if _decimal(core_period, "calibration core_period_ns") <= 0:
        raise EvidenceError("calibration core_period_ns must be positive")
    if _decimal(link_period, "calibration link_period_ns") <= 0:
        raise EvidenceError("calibration link_period_ns must be positive")

    target = record.get("gem5_microprobe_ns", record.get("target_ns"))
    measured = record.get("m2ndp_boundary_ns", record.get("measured_ns"))
    target_value = _decimal(target, "calibration gem5 round trip")
    measured_value = _decimal(measured, "calibration M2NDP round trip")
    residual = record.get("residual_ns")
    residual_value = _decimal(residual, "calibration residual_ns")
    if residual_value != abs(target_value - measured_value):
        raise EvidenceError("calibration residual differs from round trips")

    evidence_path = ""
    evidence_sha256 = ""
    if source is not None:
        source = Path(source).resolve()
        evidence_path = str(source)
        evidence_sha256 = sha256_file(source)
        if verify_config:
            config_root = source.parent / "config"
            for filename, field, label in (
                ("m2ndp.config", "derived_m2ndp_config_sha256", "M2NDP config"),
                ("cxl_link.icnt", "derived_cxl_link_config_sha256", "CXL link config"),
            ):
                expected = _digest(record, field)
                if sha256_file(config_root / filename) != expected:
                    raise EvidenceError(f"calibration {label} SHA-256 differs")

    return CalibrationRow(
        latency=latency,
        gem5_round_trip_ns=target,
        selected_link_latency=selected,
        core_period_ns=core_period,
        link_period_ns=link_period,
        m2ndp_round_trip_ns=measured,
        residual_ns=residual,
        residual_ps=_canonical_decimal(residual_value * decimal.Decimal(1000)),
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
    )


def load_calibration(path: Path) -> CalibrationRow:
    path = Path(path).resolve()
    return _calibration_row(
        _load_json(path, "calibration evidence"),
        source=path,
        verify_config=True,
    )


def _command_file(
    row: dict, *, binary_hash_field: str, config_hash_field: str, label: str,
) -> None:
    command = row.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise EvidenceError(f"{label} command is missing")
    binary = Path(command[0]).resolve()
    if sha256_file(binary) != _digest(row, binary_hash_field):
        raise EvidenceError(f"{label} binary SHA-256 differs")
    try:
        config = Path(command[command.index("--config") + 1]).resolve()
    except (ValueError, IndexError) as error:
        raise EvidenceError(f"{label} command config is missing") from error
    if sha256_file(config) != _digest(row, config_hash_field):
        raise EvidenceError(f"{label} config SHA-256 differs")


def _validate_functional(row: dict) -> None:
    functional = row.get("functional")
    if not isinstance(functional, dict):
        raise EvidenceError("M2NDP functional evidence is missing")
    if (
        functional.get("schema") != 1
        or functional.get("status") != "pass"
        or functional.get("returncode") != 0
        or functional.get("verification") != "pass"
        or functional.get("numeric_verification") != "pass"
    ):
        raise EvidenceError("M2NDP functional evidence did not pass")
    expected = _integer(functional, "expected_launches", positive=True)
    if _integer(functional, "completed_launches") != expected:
        raise EvidenceError("M2NDP functional launch count differs")
    if not isinstance(functional.get("bit_exact"), bool):
        raise EvidenceError("M2NDP functional bit_exact marker is missing")
    _command_file(
        functional, binary_hash_field="funcsim_sha256",
        config_hash_field="config_sha256", label="FuncSim",
    )
    _verify_path_hash(
        functional, "stdout_path", "stdout_sha256", "FuncSim stdout"
    )
    _verify_path_hash(
        functional, "stderr_path", "stderr_sha256", "FuncSim stderr"
    )


def load_m2ndp_cell(
    path: Path, workload: str, latency: str, *, expected_input_sha256: str,
) -> dict:
    if workload not in WORKLOADS or latency not in LATENCIES:
        raise EvidenceError("M2NDP coordinate is not in the 24-cell matrix")
    path = Path(path).resolve()
    row = _load_json(path, "M2NDP evidence")
    if row.get("schema") != 1 or row.get("status") != "pass":
        raise EvidenceError("M2NDP evidence status is not PASS")
    embedded_workload = row.get("workload")
    if embedded_workload is not None and embedded_workload != workload:
        raise EvidenceError("M2NDP workload differs from requested coordinate")
    if row.get("cxl_link_delay") != latency:
        raise EvidenceError("M2NDP latency differs from requested coordinate")
    if (
        row.get("returncode") != 0
        or row.get("verification") != "pass"
        or row.get("numeric_verification") != "pass"
        or row.get("memory_match") != "pass"
    ):
        raise EvidenceError("M2NDP timing evidence did not pass")
    if not isinstance(row.get("bit_exact"), bool):
        raise EvidenceError("M2NDP bit_exact marker is missing")
    cycles = _integer(row, "cycles", positive=True)
    expected = _integer(row, "expected_launches", positive=True)
    if _integer(row, "completed_launches") != expected:
        raise EvidenceError("M2NDP timing launch count differs")
    for field in (
        "ndpsim_sha256", "config_sha256", "package_sha256",
        "trace_sha256", "input_sha256", "patch_sha256",
    ):
        _digest(row, field)
    if not isinstance(expected_input_sha256, str) or not _SHA256.fullmatch(
        expected_input_sha256
    ):
        raise EvidenceError("expected input SHA-256 is invalid")
    if row["input_sha256"] != expected_input_sha256:
        raise EvidenceError("M2NDP input SHA-256 differs from requested coordinate")
    _validate_functional(row)
    _command_file(
        row, binary_hash_field="ndpsim_sha256",
        config_hash_field="config_sha256", label="NDPSim",
    )
    stdout = _verify_path_hash(
        row, "stdout_path", "stdout_sha256", "NDPSim stdout"
    )
    stderr = _verify_path_hash(
        row, "stderr_path", "stderr_sha256", "NDPSim stderr"
    )
    _verify_path_hash(row, "output_path", "output_sha256", "NDPSim output")
    combined = (
        stdout.read_text(encoding="utf-8", errors="replace")
        + "\n"
        + stderr.read_text(encoding="utf-8", errors="replace")
    )
    markers = _CYCLE_MARKER.findall(combined)
    if len(markers) != 1:
        raise EvidenceError("NDPSim log must contain exactly one EXPR FINISHED marker")
    if int(markers[0]) != cycles:
        raise EvidenceError("NDPSim cycle marker differs from recorded cycles")

    calibration_record = row.get("calibration")
    if not isinstance(calibration_record, dict):
        raise EvidenceError("M2NDP calibration evidence is missing")
    calibration = _calibration_row(
        calibration_record, source=None, verify_config=False
    )
    if calibration.latency != latency:
        raise EvidenceError("M2NDP calibration latency differs")
    calibration_path = row.get("calibration_evidence_path")
    if calibration_path is not None:
        calibration_source = _verify_path_hash(
            row, "calibration_evidence_path", "calibration_evidence_sha256",
            "calibration evidence",
        )
        external = _load_json(calibration_source, "calibration evidence")
        if external != calibration_record:
            raise EvidenceError("embedded M2NDP calibration differs from evidence file")
        calibration = load_calibration(calibration_source)

    if calibration.core_period_ns != "0.5":
        raise EvidenceError("M2NDP core period differs from the 0.5 ns contract")
    origin = row.get("execution_origin", "verified_reuse")
    if origin not in ("fresh", "verified_reuse"):
        raise EvidenceError("M2NDP execution origin is invalid")
    return {
        "schema": 1,
        "status": "pass",
        "workload": workload,
        "latency": latency,
        "cycles": cycles,
        "core_period_ns": calibration.core_period_ns,
        "kernel_time_ns": cycles_to_ns(cycles, calibration.core_period_ns),
        "execution_origin": origin,
        "functional_verification": row["functional"]["verification"],
        "numeric_verification": row["functional"]["numeric_verification"],
        "bit_exact": row["functional"]["bit_exact"],
        "calibration": dataclasses.asdict(calibration),
        "source_evidence_path": str(path),
        "source_evidence_sha256": sha256_file(path),
        "ndpsim_sha256": row["ndpsim_sha256"],
        "config_sha256": row["config_sha256"],
        "package_sha256": row["package_sha256"],
        "trace_sha256": row["trace_sha256"],
        "input_sha256": row["input_sha256"],
        "patch_sha256": row["patch_sha256"],
        "stdout_path": str(stdout),
        "stdout_sha256": row["stdout_sha256"],
        "stderr_path": str(stderr),
        "stderr_sha256": row["stderr_sha256"],
        "output_path": str(Path(row["output_path"]).resolve()),
        "output_sha256": row["output_sha256"],
        "command": row["command"],
    }
