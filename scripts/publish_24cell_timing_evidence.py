#!/usr/bin/env python3
"""Publish the validated 24-cell campaign as deterministic sharing artifacts."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import json
import shutil
from pathlib import Path

try:
    from scripts import run_24cell_timing_evidence as runner
    from scripts import timing_evidence_24cell as evidence
except ImportError:
    import run_24cell_timing_evidence as runner
    import timing_evidence_24cell as evidence


FIELDS = (
    "workload", "latency",
    "m2ndp_cycles", "m2ndp_core_period_ns", "m2ndp_kernel_time_ns",
    "selected_link_latency", "calibration_residual_ps",
    "host_region_cumulative_ticks", "host_region_cumulative_ns",
    "host_region_entry_count",
    "cira_device_first_issue_tick", "cira_device_last_completion_tick",
    "cira_device_busy_ticks", "cira_device_busy_ns",
    "cira_busy_ticks_core0", "cira_busy_ticks_core1",
    "cira_busy_ticks_core2", "cira_busy_ticks_core3",
    "cira_issued_core0", "cira_issued_core1",
    "cira_issued_core2", "cira_issued_core3",
    "cira_completed_core0", "cira_completed_core1",
    "cira_completed_core2", "cira_completed_core3",
    "pr_descriptor_applicable", "pr_compute_ticks", "pr_queue_stall_ticks",
    "pr_compute_ticks_core0", "pr_compute_ticks_core1",
    "pr_compute_ticks_core2", "pr_compute_ticks_core3",
    "pr_queue_stall_ticks_core0", "pr_queue_stall_ticks_core1",
    "pr_queue_stall_ticks_core2", "pr_queue_stall_ticks_core3",
    "m2ndp_evidence_path", "m2ndp_evidence_sha256",
    "host_inline_evidence_path", "host_inline_evidence_sha256",
    "cira_runtime_evidence_path", "cira_runtime_evidence_sha256",
)

CALIBRATION_FIELDS = (
    "latency", "gem5_round_trip_ns", "selected_link_latency",
    "core_period_ns", "link_period_ns", "m2ndp_round_trip_ns",
    "residual_ns", "residual_ps", "evidence_sha256",
)


class PublishError(RuntimeError):
    """The source campaign cannot be published without misrepresenting data."""


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(f"{label} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise PublishError(f"{label} must be a JSON object")
    return value


def _identity(value: dict) -> runner.CampaignIdentity:
    fields = value.get("identity")
    if not isinstance(fields, dict):
        raise PublishError("campaign identity is missing")
    fields = dict(fields)
    try:
        fields["calibration_sha256"] = tuple(
            tuple(item) for item in fields["calibration_sha256"]
        )
        result = runner.CampaignIdentity(**fields)
    except (KeyError, TypeError, ValueError) as error:
        raise PublishError("campaign identity is invalid") from error
    if value.get("identity_sha256") != result.digest():
        raise PublishError("campaign identity digest differs")
    return result


def _source_hash(row: dict, label: str) -> None:
    path = row.get("source_evidence_path")
    expected = row.get("source_evidence_sha256")
    if not isinstance(path, str) or not isinstance(expected, str):
        raise PublishError(f"{label} source evidence record is missing")
    if evidence.sha256_file(Path(path)) != expected:
        raise PublishError(f"{label} source evidence SHA-256 differs")


def _vector(row: dict, name: str) -> list[int]:
    value = row.get(name)
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0
               for item in value)
    ):
        raise PublishError(f"{name} must contain four nonnegative integers")
    return value


def _read_cell(
    state: dict, identity: runner.CampaignIdentity,
    workload: str, latency: str,
) -> tuple[dict, dict, dict, evidence.CalibrationRow]:
    key = f"{workload}:{latency}"
    state_row = state["cells"][key]
    if not runner.cell_complete(state_row, identity, workload, latency):
        raise PublishError(f"campaign cell is not complete: {key}")
    records = state_row["evidence"]
    values = {}
    for name in ("m2ndp", "host_inline", "cira_runtime"):
        values[name] = _load_json(
            Path(records[name]["path"]), f"{key} {name} evidence"
        )
        _source_hash(values[name], f"{key} {name}")
    m2ndp = values["m2ndp"]
    host = values["host_inline"]
    cira = values["cira_runtime"]

    calibration_path = host.get("calibration_evidence_path")
    calibration_hash = host.get("calibration_evidence_sha256")
    if (
        not isinstance(calibration_path, str)
        or not isinstance(calibration_hash, str)
        or any(
            row.get("calibration_evidence_path") != calibration_path
            or row.get("calibration_evidence_sha256") != calibration_hash
            for row in (m2ndp, cira)
        )
        or evidence.sha256_file(Path(calibration_path)) != calibration_hash
    ):
        raise PublishError(f"calibration identity differs for {key}")
    calibration = evidence.load_calibration(Path(calibration_path))
    if calibration.latency != latency:
        raise PublishError(f"calibration latency differs for {key}")

    cycles = m2ndp.get("cycles")
    if (
        isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0
        or m2ndp.get("core_period_ns") != calibration.core_period_ns
        or m2ndp.get("kernel_time_ns")
        != evidence.cycles_to_ns(cycles, calibration.core_period_ns)
    ):
        raise PublishError(f"M2NDP timing differs for {key}")
    host_ticks = host.get("host_region_cumulative_ticks")
    entries = host.get("host_region_entry_count")
    if (
        host.get("system") != "cira-inline"
        or host.get("offload_disabled") is not True
        or isinstance(host_ticks, bool) or not isinstance(host_ticks, int)
        or host_ticks <= 0
        or isinstance(entries, bool) or not isinstance(entries, int) or entries <= 0
    ):
        raise PublishError(f"host-inline timing differs for {key}")
    if host.get("sim_freq_hz") != cira.get("sim_freq_hz"):
        raise PublishError(f"gem5 simFreq differs for {key}")

    generic = cira.get("generic_prefetch")
    descriptor = cira.get("pr_descriptor_metrics")
    if not isinstance(generic, dict) or not isinstance(descriptor, dict):
        raise PublishError(f"CIRA device timing is missing for {key}")
    first = generic.get("first_issue_tick")
    last = generic.get("last_completion_tick")
    busy = generic.get("busy_ticks")
    if (
        any(isinstance(item, bool) or not isinstance(item, int)
            for item in (first, last, busy))
        or first < 0 or last < first or busy <= 0 or busy != last - first
    ):
        raise PublishError(f"CIRA device busy span differs for {key}")
    _vector(generic, "busy_ticks_per_core")
    issued = _vector(cira, "issued_per_core")
    completed = _vector(cira, "completed_per_core")
    if issued != completed or any(value == 0 for value in issued):
        raise PublishError(f"CIRA per-core activity differs for {key}")
    if descriptor.get("applicable") is not False:
        raise PublishError(f"PR descriptor applicability differs for {key}")
    for name in ("compute_ticks", "queue_stall_ticks"):
        value = descriptor.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            raise PublishError(f"PR descriptor {name} differs for {key}")
    _vector(descriptor, "compute_ticks_per_core")
    _vector(descriptor, "queue_stall_ticks_per_core")
    return m2ndp, host, cira, calibration


def _csv_text(fields: tuple[str, ...], rows: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _row(
    workload: str, latency: str, m2ndp: dict, host: dict, cira: dict,
    calibration: evidence.CalibrationRow, paths: dict[str, dict],
) -> dict:
    generic = cira["generic_prefetch"]
    descriptor = cira["pr_descriptor_metrics"]
    row = {
        "workload": workload, "latency": latency,
        "m2ndp_cycles": m2ndp["cycles"],
        "m2ndp_core_period_ns": m2ndp["core_period_ns"],
        "m2ndp_kernel_time_ns": m2ndp["kernel_time_ns"],
        "selected_link_latency": calibration.selected_link_latency,
        "calibration_residual_ps": calibration.residual_ps,
        "host_region_cumulative_ticks": host["host_region_cumulative_ticks"],
        "host_region_cumulative_ns": evidence.ticks_to_ns(
            host["host_region_cumulative_ticks"], host["sim_freq_hz"]
        ),
        "host_region_entry_count": host["host_region_entry_count"],
        "cira_device_first_issue_tick": generic["first_issue_tick"],
        "cira_device_last_completion_tick": generic["last_completion_tick"],
        "cira_device_busy_ticks": generic["busy_ticks"],
        "cira_device_busy_ns": evidence.ticks_to_ns(
            generic["busy_ticks"], cira["sim_freq_hz"]
        ),
        "pr_descriptor_applicable": "false",
        "pr_compute_ticks": descriptor["compute_ticks"],
        "pr_queue_stall_ticks": descriptor["queue_stall_ticks"],
    }
    for core in range(4):
        row[f"cira_busy_ticks_core{core}"] = generic["busy_ticks_per_core"][core]
        row[f"cira_issued_core{core}"] = cira["issued_per_core"][core]
        row[f"cira_completed_core{core}"] = cira["completed_per_core"][core]
        row[f"pr_compute_ticks_core{core}"] = descriptor[
            "compute_ticks_per_core"
        ][core]
        row[f"pr_queue_stall_ticks_core{core}"] = descriptor[
            "queue_stall_ticks_per_core"
        ][core]
    for name, prefix in (
        ("m2ndp", "m2ndp"),
        ("host_inline", "host_inline"),
        ("cira_runtime", "cira_runtime"),
    ):
        row[f"{prefix}_evidence_path"] = paths[name]["path"]
        row[f"{prefix}_evidence_sha256"] = paths[name]["sha256"]
    return row


def _readme(rows: list[dict], calibrations: list[evidence.CalibrationRow]) -> str:
    lines = [
        "# 24-cell CIRA, host-inline, and M2NDP timing evidence",
        "",
        "All primary measurements retain integer cycles or ticks. Times in ns are exact",
        "conversions using the recorded 0.5 ns M2NDP core period and per-cell gem5",
        "`simFreq`. CIRA uses generic prefetch spans; PR descriptor fields are not",
        "applicable and are required to be zero for these cells.",
        "",
        "## Link calibration",
        "",
        "| CXL latency | link_latency | gem5 RT (ns) | M2NDP RT (ns) | residual (ps) |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in calibrations:
        lines.append(
            f"| {item.latency} | {item.selected_link_latency} | "
            f"{item.gem5_round_trip_ns} | {item.m2ndp_round_trip_ns} | "
            f"{item.residual_ps} |"
        )
    lines.extend((
        "", "## Per-cell timing", "",
        "| Workload | CXL latency | M2NDP kernel (ns) | Host inline (ns) | Entries | CIRA device (ns) |",
        "|---|---|---:|---:|---:|---:|",
    ))
    for row in rows:
        lines.append(
            f"| {row['workload']} | {row['latency']} | "
            f"{row['m2ndp_kernel_time_ns']} | "
            f"{row['host_region_cumulative_ns']} | "
            f"{row['host_region_entry_count']} | "
            f"{row['cira_device_busy_ns']} |"
        )
    return "\n".join(lines) + "\n"


def publish(source: Path, destination: Path) -> Path:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise PublishError(f"fresh publication root required: {destination}")
    state = _load_json(source / "complete.json", "complete campaign")
    if state.get("schema") != 1 or state.get("status") != "complete":
        raise PublishError("source campaign is not complete")
    identity = _identity(state)
    expected = {
        f"{workload}:{latency}" for workload, latency in evidence.COORDINATES
    }
    if set(state.get("cells", {})) != expected:
        raise PublishError("source campaign does not contain 24 complete cells")

    validated = []
    calibration_by_latency = {}
    for workload, latency in evidence.COORDINATES:
        values = _read_cell(state, identity, workload, latency)
        calibration = values[3]
        previous = calibration_by_latency.setdefault(latency, calibration)
        if previous != calibration:
            raise PublishError(f"calibration differs within latency {latency}")
        validated.append((workload, latency, *values))

    destination.mkdir(parents=True)
    rows = []
    generated = {}
    for workload, latency, m2ndp, host, cira, calibration in validated:
        source_records = state["cells"][f"{workload}:{latency}"]["evidence"]
        output_records = {}
        filenames = {
            "m2ndp": "m2ndp-evidence.json",
            "host_inline": "host-inline-evidence.json",
            "cira_runtime": "cira-runtime-evidence.json",
        }
        for name, filename in filenames.items():
            relative = Path("cells") / workload / latency / filename
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_records[name]["path"], target)
            digest = evidence.sha256_file(target)
            if digest != source_records[name]["sha256"]:
                raise PublishError(f"copied evidence differs: {relative}")
            output_records[name] = {
                "path": relative.as_posix(), "sha256": digest,
            }
            generated[relative.as_posix()] = digest
        rows.append(_row(
            workload, latency, m2ndp, host, cira, calibration, output_records
        ))

    calibration_rows = [
        {
            key: value for key, value in dataclasses.asdict(
                calibration_by_latency[latency]
            ).items() if key in CALIBRATION_FIELDS
        }
        for latency in evidence.LATENCIES
    ]
    outputs = {
        "timing-24cells.csv": _csv_text(FIELDS, rows),
        "calibration.csv": _csv_text(CALIBRATION_FIELDS, calibration_rows),
        "README.md": _readme(
            rows, [calibration_by_latency[item] for item in evidence.LATENCIES]
        ),
    }
    for name, text in outputs.items():
        path = destination / name
        path.write_text(text, encoding="utf-8")
        generated[name] = evidence.sha256_file(path)
    manifest = {
        "schema": 1, "status": "complete", "rows": len(rows),
        "identity": state["identity"],
        "identity_sha256": identity.digest(),
        "files": dict(sorted(generated.items())),
    }
    runner.atomic_write_json(destination / "manifest.json", manifest)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        publish(options.source, options.destination)
    except (PublishError, runner.CampaignError, evidence.EvidenceError) as error:
        print(f"TIMING_24CELL_PUBLISH_FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

