#!/usr/bin/env python3
"""Publish deterministic progress or final host/CIRA timing artifacts."""

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
    "workload", "latency", "host_status", "cira_status",
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
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in value
        )
    ):
        raise PublishError(f"{name} must contain four nonnegative integers")
    return value


def _stage_value(
    state_row: dict, identity: runner.CampaignIdentity,
    workload: str, latency: str, stage: str,
) -> tuple[str, dict | None, dict | None]:
    stages = state_row.get("stages")
    item = stages.get(stage) if isinstance(stages, dict) else None
    if not isinstance(item, dict):
        raise PublishError(f"campaign stage is missing: {workload}:{latency} {stage}")
    status = item.get("status")
    if status not in ("pending", "running", "failed", "complete"):
        raise PublishError(f"campaign stage status differs: {workload}:{latency} {stage}")
    if status != "complete":
        return status, None, None
    if not runner.stage_complete(
        state_row, identity, workload, latency, stage
    ):
        raise PublishError(
            f"complete stage differs: {workload}:{latency} {stage}"
        )
    record = item["evidence"]
    value = _load_json(Path(record["path"]), f"{workload}:{latency} {stage}")
    _source_hash(value, f"{workload}:{latency} {stage}")
    return status, value, record


def _calibration_from_payload(
    row: dict, workload: str, latency: str,
) -> evidence.CalibrationRow:
    path = row.get("calibration_evidence_path")
    expected = row.get("calibration_evidence_sha256")
    if (
        not isinstance(path, str)
        or not isinstance(expected, str)
        or evidence.sha256_file(Path(path)) != expected
    ):
        raise PublishError(f"calibration identity differs for {workload}:{latency}")
    result = evidence.load_calibration(Path(path))
    if result.latency != latency or result.evidence_sha256 != expected:
        raise PublishError(f"calibration latency differs for {workload}:{latency}")
    return result


def _validate_host(host: dict, key: str) -> None:
    ticks = host.get("host_region_cumulative_ticks")
    entries = host.get("host_region_entry_count")
    sim_freq = host.get("sim_freq_hz")
    if (
        host.get("system") != "cira-inline"
        or host.get("offload_disabled") is not True
        or isinstance(ticks, bool) or not isinstance(ticks, int) or ticks <= 0
        or isinstance(entries, bool) or not isinstance(entries, int) or entries <= 0
        or isinstance(sim_freq, bool) or not isinstance(sim_freq, int)
        or sim_freq <= 0
    ):
        raise PublishError(f"host-inline timing differs for {key}")


def _validate_cira(cira: dict, key: str) -> None:
    generic = cira.get("generic_prefetch")
    descriptor = cira.get("pr_descriptor_metrics")
    if not isinstance(generic, dict) or not isinstance(descriptor, dict):
        raise PublishError(f"CIRA device timing is missing for {key}")
    first = generic.get("first_issue_tick")
    last = generic.get("last_completion_tick")
    busy = generic.get("busy_ticks")
    sim_freq = cira.get("sim_freq_hz")
    if (
        cira.get("system") != "cira"
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in (first, last, busy, sim_freq)
        )
        or first < 0 or last < first or busy <= 0
        or busy != last - first or sim_freq <= 0
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


def _read_cell(
    state: dict, identity: runner.CampaignIdentity,
    workload: str, latency: str,
) -> dict:
    key = f"{workload}:{latency}"
    state_row = state["cells"][key]
    host_status, host, host_record = _stage_value(
        state_row, identity, workload, latency, "host_inline"
    )
    cira_status, cira, cira_record = _stage_value(
        state_row, identity, workload, latency, "cira_runtime"
    )
    calibration = None
    for payload in (host, cira):
        if payload is None:
            continue
        candidate = _calibration_from_payload(payload, workload, latency)
        if calibration is not None and candidate != calibration:
            raise PublishError(f"calibration identity differs for {key}")
        calibration = candidate
    if host is not None:
        _validate_host(host, key)
    if cira is not None:
        _validate_cira(cira, key)
    if host is not None and cira is not None:
        if host.get("sim_freq_hz") != cira.get("sim_freq_hz"):
            raise PublishError(f"gem5 simFreq differs for {key}")
    return {
        "host_status": host_status, "cira_status": cira_status,
        "host": host, "cira": cira, "calibration": calibration,
        "records": {"host_inline": host_record, "cira_runtime": cira_record},
    }


def _csv_text(fields: tuple[str, ...], rows: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream, fieldnames=fields, lineterminator="\n", extrasaction="raise"
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _row(
    workload: str, latency: str, values: dict, output_records: dict,
) -> dict:
    row = {field: "" for field in FIELDS}
    row.update({
        "workload": workload, "latency": latency,
        "host_status": values["host_status"],
        "cira_status": values["cira_status"],
    })
    calibration = values["calibration"]
    if calibration is not None:
        row["selected_link_latency"] = calibration.selected_link_latency
        row["calibration_residual_ps"] = calibration.residual_ps
    host = values["host"]
    if host is not None:
        row.update({
            "host_region_cumulative_ticks": host["host_region_cumulative_ticks"],
            "host_region_cumulative_ns": evidence.ticks_to_ns(
                host["host_region_cumulative_ticks"], host["sim_freq_hz"]
            ),
            "host_region_entry_count": host["host_region_entry_count"],
        })
    cira = values["cira"]
    if cira is not None:
        generic = cira["generic_prefetch"]
        descriptor = cira["pr_descriptor_metrics"]
        row.update({
            "cira_device_first_issue_tick": generic["first_issue_tick"],
            "cira_device_last_completion_tick": generic["last_completion_tick"],
            "cira_device_busy_ticks": generic["busy_ticks"],
            "cira_device_busy_ns": evidence.ticks_to_ns(
                generic["busy_ticks"], cira["sim_freq_hz"]
            ),
            "pr_descriptor_applicable": "false",
            "pr_compute_ticks": descriptor["compute_ticks"],
            "pr_queue_stall_ticks": descriptor["queue_stall_ticks"],
        })
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
        ("host_inline", "host_inline"),
        ("cira_runtime", "cira_runtime"),
    ):
        record = output_records.get(name)
        if record is not None:
            row[f"{prefix}_evidence_path"] = record["path"]
            row[f"{prefix}_evidence_sha256"] = record["sha256"]
    return row


def _readme(rows: list[dict], progress: bool) -> str:
    title = "G14 host-inline and CIRA timing progress" if progress else (
        "G14 24-cell host-inline and CIRA timing evidence"
    )
    lines = [
        f"# {title}", "",
        "All primary measurements retain integer gem5 ticks. Nanoseconds are",
        "exact conversions using the per-cell recorded `simFreq`. Blank numeric",
        "fields mean the corresponding stage is pending, running, or failed;",
        "consult `host_status` and `cira_status` rather than treating blanks as zero.",
        "", "| Workload | CXL latency | Host status | Host ticks | CIRA status | CIRA busy ticks |",
        "|---|---|---|---:|---|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['workload']} | {row['latency']} | {row['host_status']} | "
            f"{row['host_region_cumulative_ticks']} | {row['cira_status']} | "
            f"{row['cira_device_busy_ticks']} |"
        )
    return "\n".join(lines) + "\n"


def publish(source: Path, destination: Path, *, progress: bool = False) -> Path:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise PublishError(f"fresh publication root required: {destination}")
    state = _load_json(source / "state.json", "campaign state")
    if state.get("schema") != 1:
        raise PublishError("source campaign schema differs")
    identity = _identity(state)
    expected = {
        f"{workload}:{latency}" for workload, latency in evidence.COORDINATES
    }
    if set(state.get("cells", {})) != expected:
        raise PublishError("source campaign does not contain 24 cells")

    validated = []
    incomplete = []
    calibration_by_latency = {}
    for workload, latency in evidence.COORDINATES:
        values = _read_cell(state, identity, workload, latency)
        for stage, status in (
            ("host", values["host_status"]), ("CIRA", values["cira_status"]),
        ):
            if status != "complete":
                incomplete.append(f"{workload}:{latency}:{stage}={status}")
        calibration = values["calibration"]
        if calibration is not None:
            previous = calibration_by_latency.setdefault(latency, calibration)
            if previous != calibration:
                raise PublishError(f"calibration differs within latency {latency}")
        validated.append((workload, latency, values))
    if not progress and incomplete:
        raise PublishError("incomplete host/CIRA cells: " + ", ".join(incomplete))
    if not progress and state.get("status") != "complete":
        raise PublishError("source campaign is not complete")

    destination.mkdir(parents=True)
    rows = []
    generated = {}
    for workload, latency, values in validated:
        output_records = {}
        filenames = {
            "host_inline": "host-inline-evidence.json",
            "cira_runtime": "cira-runtime-evidence.json",
        }
        for name, filename in filenames.items():
            source_record = values["records"][name]
            if source_record is None:
                continue
            relative = Path("cells") / workload / latency / filename
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_record["path"], target)
            digest = evidence.sha256_file(target)
            if digest != source_record["sha256"]:
                raise PublishError(f"copied evidence differs: {relative}")
            output_records[name] = {
                "path": relative.as_posix(), "sha256": digest,
            }
            generated[relative.as_posix()] = digest
        rows.append(_row(workload, latency, values, output_records))

    calibration_rows = [
        {
            key: value for key, value in dataclasses.asdict(
                calibration_by_latency[latency]
            ).items() if key in CALIBRATION_FIELDS
        }
        for latency in evidence.LATENCIES if latency in calibration_by_latency
    ]
    csv_name = "timing-24cells-progress.csv" if progress else "timing-24cells.csv"
    outputs = {
        csv_name: _csv_text(FIELDS, rows),
        "calibration.csv": _csv_text(CALIBRATION_FIELDS, calibration_rows),
        "README.md": _readme(rows, progress),
    }
    for name, content in outputs.items():
        path = destination / name
        path.write_text(content, encoding="utf-8")
        generated[name] = evidence.sha256_file(path)
    manifest = {
        "schema": 1, "status": "progress" if progress else "complete",
        "mode": "progress" if progress else "final", "rows": len(rows),
        "identity": state["identity"],
        "identity_sha256": identity.digest(),
        "completed_host_stages": sum(
            row[2]["host_status"] == "complete" for row in validated
        ),
        "completed_cira_stages": sum(
            row[2]["cira_status"] == "complete" for row in validated
        ),
        "files": dict(sorted(generated.items())),
    }
    runner.atomic_write_json(destination / "manifest.json", manifest)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--progress", action="store_true")
    options = parser.parse_args(argv)
    try:
        publish(options.source, options.destination, progress=options.progress)
    except (PublishError, runner.CampaignError, evidence.EvidenceError) as error:
        print(f"TIMING_24CELL_PUBLISH_FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
