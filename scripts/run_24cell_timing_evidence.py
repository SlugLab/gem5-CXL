#!/usr/bin/env python3
"""Run and resume the six-workload by four-latency timing campaign."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import m2ndp_workload_trace as m2ndp_trace
    from scripts import run_matched_breadth_gem5 as replay
    from scripts import timing_evidence_24cell as evidence
except ImportError:
    import m2ndp_workload_trace as m2ndp_trace
    import run_matched_breadth_gem5 as replay
    import timing_evidence_24cell as evidence


REPO = Path(__file__).resolve().parents[1]
GEM5_CONFIG = REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
MINIMUM_FREE_BYTES = 20 * 1024**3
_EVIDENCE_NAMES = (
    "m2ndp",
    "host_inline",
    "cira_runtime",
)


class CampaignError(RuntimeError):
    """Campaign identity, state, input, or execution is invalid."""


@dataclasses.dataclass(frozen=True)
class CampaignIdentity:
    repository_commit: str
    code_sha256: str
    input_manifest_sha256: str
    prepared_manifest_sha256: str
    replay_binary_sha256: str
    gem5_sha256: str
    m5_library_sha256: str
    funcsim_sha256: str
    ndpsim_sha256: str
    gem5_config_sha256: str
    calibration_sha256: tuple[tuple[str, str], ...]

    def digest(self) -> str:
        payload = json.dumps(
            dataclasses.asdict(self), sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def atomic_write_json(path: Path, value: dict) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def new_state(identity: CampaignIdentity) -> dict:
    if not isinstance(identity, CampaignIdentity):
        raise CampaignError("campaign identity has the wrong type")
    return {
        "schema": 1,
        "status": "running",
        "identity": dataclasses.asdict(identity),
        "identity_sha256": identity.digest(),
        "cells": {
            f"{workload}:{latency}": {
                "workload": workload,
                "latency": latency,
                "status": "pending",
            }
            for workload, latency in evidence.COORDINATES
        },
    }


def resume_state(state: dict, identity: CampaignIdentity) -> dict:
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise CampaignError("campaign state schema differs")
    if (
        state.get("identity") != dataclasses.asdict(identity)
        or state.get("identity_sha256") != identity.digest()
    ):
        raise CampaignError("campaign identity differs")
    expected = {
        f"{workload}:{latency}" for workload, latency in evidence.COORDINATES
    }
    if set(state.get("cells", {})) != expected:
        raise CampaignError("campaign state does not contain the exact 24-cell matrix")
    return state


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CampaignError(f"{label} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise CampaignError(f"{label} must be a JSON object")
    return value


def _validate_file_record(record: dict, label: str) -> Path:
    if not isinstance(record, dict):
        raise CampaignError(f"{label} file record is missing")
    path = record.get("path")
    expected = record.get("sha256")
    if not isinstance(path, str) or not isinstance(expected, str):
        raise CampaignError(f"{label} file record is invalid")
    resolved = Path(path).resolve()
    if evidence.sha256_file(resolved) != expected:
        raise CampaignError(f"{label} SHA-256 differs")
    return resolved


def _trace_identity(path: Path, label: str) -> tuple[str, str]:
    value = _load_json(path, label)
    identity = value.get("meta") if isinstance(value.get("meta"), dict) else value
    workload = identity.get("workload")
    input_sha256 = identity.get("input_sha256")
    if not isinstance(workload, str) or not isinstance(input_sha256, str):
        raise CampaignError(f"{label} identity is missing")
    return workload, input_sha256


def _validate_registry_cell(key: str, row: dict) -> dict:
    if not isinstance(row, dict):
        raise CampaignError(f"prepared cell {key} is invalid")
    try:
        workload, _latency = key.split(":", 1)
    except ValueError as error:
        raise CampaignError(f"prepared cell key is invalid: {key}") from error
    expected_input = row.get("input_sha256")
    if (
        not isinstance(expected_input, str)
        or len(expected_input) != 64
        or any(character not in "0123456789abcdef" for character in expected_input)
    ):
        raise CampaignError(f"prepared cell {key} input SHA-256 is invalid")
    trace_path = _validate_file_record(row.get("trace"), f"{key} trace")
    trace_workload, trace_input = _trace_identity(trace_path, f"{key} trace")
    if trace_workload != workload:
        raise CampaignError(f"prepared cell {key} trace workload differs")
    if trace_input != expected_input:
        raise CampaignError(f"prepared cell {key} trace input SHA-256 differs")
    _validate_file_record(row.get("window_manifest"), f"{key} window_manifest")
    fixed = row.get("fixed_trace")
    if fixed is not None:
        fixed_path = _validate_file_record(fixed, f"{key} fixed_trace")
        fixed_workload, fixed_input = _trace_identity(
            fixed_path, f"{key} fixed trace"
        )
        if fixed_workload != workload:
            raise CampaignError(f"prepared cell {key} fixed trace workload differs")
        if fixed_input != expected_input:
            raise CampaignError(
                f"prepared cell {key} fixed trace input SHA-256 differs"
            )
    phase = row.get("phase")
    window = row.get("window_index")
    if (
        isinstance(phase, bool) or not isinstance(phase, int) or phase < 0
        or isinstance(window, bool) or not isinstance(window, int) or window < 0
    ):
        raise CampaignError(f"prepared cell {key} window selection is invalid")
    reused = row.get("m2ndp_evidence")
    package = row.get("m2ndp_package")
    functional = row.get("functional_evidence")
    if reused is not None:
        _validate_file_record(reused, f"{key} M2NDP evidence")
    elif package is not None and functional is not None:
        _validate_file_record(package, f"{key} M2NDP package")
        _validate_file_record(functional, f"{key} FuncSim evidence")
    else:
        raise CampaignError(f"prepared cell {key} M2NDP source is missing")
    return row


def load_registry(path: Path) -> dict:
    value = _load_json(path, "prepared 24-cell registry")
    if value.get("schema") != 1 or value.get("status") != "verified":
        raise CampaignError("prepared 24-cell registry is not verified schema 1")
    cells = value.get("cells")
    expected = {
        f"{workload}:{latency}" for workload, latency in evidence.COORDINATES
    }
    if not isinstance(cells, dict) or set(cells) != expected:
        raise CampaignError("prepared registry does not contain the exact 24-cell matrix")
    return {key: _validate_registry_cell(key, cells[key]) for key in sorted(cells)}


def _evidence_record(path: Path) -> dict:
    return {"path": str(Path(path).resolve()), "sha256": evidence.sha256_file(path)}


def cell_complete(
    row: dict, identity: CampaignIdentity, workload: str, latency: str,
) -> bool:
    if (
        not isinstance(row, dict)
        or row.get("status") != "complete"
        or row.get("workload") != workload
        or row.get("latency") != latency
        or row.get("identity_sha256") != identity.digest()
    ):
        return False
    records = row.get("evidence")
    if not isinstance(records, dict) or set(records) != set(_EVIDENCE_NAMES):
        return False
    try:
        for name, record in records.items():
            path = _validate_file_record(record, f"{workload}:{latency} {name}")
            payload = _load_json(path, f"{workload}:{latency} {name}")
            if (
                payload.get("schema") != 1
                or payload.get("status") != "pass"
                or payload.get("workload") != workload
                or payload.get("latency") != latency
                or payload.get("campaign_identity_sha256") != identity.digest()
            ):
                return False
    except (CampaignError, evidence.EvidenceError):
        return False
    return True


def _require_replay_identity(result: dict, identity: CampaignIdentity, system: str) -> dict:
    if (
        not isinstance(result, dict)
        or result.get("schema") != 1
        or result.get("status") != "pass"
        or result.get("system") != system
        or result.get("binary_sha256") != identity.replay_binary_sha256
        or result.get("gem5_sha256") != identity.gem5_sha256
    ):
        raise CampaignError(f"{system} replay identity differs")
    row = result.get("row")
    if not isinstance(row, dict) or row.get("verification") != "pass":
        raise CampaignError(f"{system} replay evidence did not pass")
    return row


def _host_evidence(
    result: dict, identity: CampaignIdentity, workload: str, latency: str,
    calibration: evidence.CalibrationRow, source: dict,
) -> dict:
    row = _require_replay_identity(result, identity, "cira-inline")
    if (
        row.get("offload_disabled") is not True
        or row.get("host_region_entry_count", 0) <= 0
        or any(row.get(field) != 0 for field in ("issued_loads", "completed_loads"))
        or any(row.get("issued_per_core", ()))
        or any(row.get("completed_per_core", ()))
    ):
        raise CampaignError("cira-inline replay contains offload activity")
    ticks = row.get("host_region_cumulative_ticks")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks <= 0:
        raise CampaignError("cira-inline cumulative ticks are invalid")
    sim_freq = row.get("sim_freq_hz")
    if isinstance(sim_freq, bool) or not isinstance(sim_freq, int) or sim_freq <= 0:
        raise CampaignError("cira-inline simFreq is invalid")
    return {
        "schema": 1, "status": "pass", "workload": workload,
        "latency": latency, "campaign_identity_sha256": identity.digest(),
        "system": "cira-inline", "offload_disabled": True,
        "host_region_cumulative_ticks": ticks,
        "host_region_entry_count": row["host_region_entry_count"],
        "sim_freq_hz": sim_freq,
        "replay_binary_sha256": result["binary_sha256"],
        "gem5_sha256": result["gem5_sha256"],
        "config_sha256": result.get("config_sha256"),
        "calibration_evidence_path": calibration.evidence_path,
        "calibration_evidence_sha256": calibration.evidence_sha256,
        "source_command": result.get("command"),
        "source_evidence_path": source["path"],
        "source_evidence_sha256": source["sha256"],
    }


def _cira_evidence(
    result: dict, identity: CampaignIdentity, workload: str, latency: str,
    calibration: evidence.CalibrationRow, source: dict,
) -> dict:
    row = _require_replay_identity(result, identity, "cira")
    generic = row.get("generic_prefetch")
    descriptor = row.get("pr_descriptor_metrics")
    if not isinstance(generic, dict) or not isinstance(descriptor, dict):
        raise CampaignError("CIRA device timing metrics are missing")
    if (
        generic.get("busy_ticks", 0) <= 0
        or generic.get("last_completion_tick", -1)
        - generic.get("first_issue_tick", 0) != generic.get("busy_ticks")
        or len(generic.get("busy_ticks_per_core", ())) != 4
        or len(row.get("issued_per_core", ())) != 4
        or row.get("issued_per_core") != row.get("completed_per_core")
    ):
        raise CampaignError("CIRA generic device span is invalid")
    if descriptor.get("applicable") is not False or any(
        descriptor.get(name, 0)
        for name in ("compute_ticks", "queue_stall_ticks", "issued", "completed")
    ):
        raise CampaignError("matched CIRA replay used descriptor metrics")
    sim_freq = row.get("sim_freq_hz")
    if isinstance(sim_freq, bool) or not isinstance(sim_freq, int) or sim_freq <= 0:
        raise CampaignError("CIRA simFreq is invalid")
    return {
        "schema": 1, "status": "pass", "workload": workload,
        "latency": latency, "campaign_identity_sha256": identity.digest(),
        "system": "cira", "generic_prefetch": generic,
        "issued_prefetches": row.get("issued_prefetches"),
        "completed_prefetches": row.get("completed_prefetches"),
        "issued_per_core": row.get("issued_per_core"),
        "completed_per_core": row.get("completed_per_core"),
        "sim_freq_hz": sim_freq,
        "pr_descriptor_metrics": descriptor,
        "replay_binary_sha256": result["binary_sha256"],
        "gem5_sha256": result["gem5_sha256"],
        "config_sha256": result.get("config_sha256"),
        "calibration_evidence_path": calibration.evidence_path,
        "calibration_evidence_sha256": calibration.evidence_sha256,
        "source_command": result.get("command"),
        "source_evidence_path": source["path"],
        "source_evidence_sha256": source["sha256"],
    }


def _m2ndp_evidence(
    result: dict, identity: CampaignIdentity, workload: str, latency: str,
    calibration: evidence.CalibrationRow,
) -> dict:
    if (
        not isinstance(result, dict)
        or result.get("schema") != 1
        or result.get("status") != "pass"
        or result.get("workload") != workload
        or result.get("latency") != latency
        or result.get("core_period_ns") != calibration.core_period_ns
    ):
        raise CampaignError("normalized M2NDP evidence identity differs")
    return {
        **result,
        "campaign_identity_sha256": identity.digest(),
        "calibration_evidence_path": calibration.evidence_path,
        "calibration_evidence_sha256": calibration.evidence_sha256,
    }


def execute_cell(
    state: dict, identity: CampaignIdentity, workload: str, latency: str,
    registry_cell: dict, calibration: evidence.CalibrationRow, *, root: Path,
    replay_launcher, m2ndp_launcher,
) -> dict:
    key = f"{workload}:{latency}"
    resume_state(state, identity)
    if key not in state["cells"]:
        raise CampaignError(f"coordinate is not in campaign: {key}")
    current = state["cells"][key]
    if current.get("status") == "complete":
        if cell_complete(current, identity, workload, latency):
            return current
        raise CampaignError(f"stale complete cell cannot be resumed: {key}")
    _validate_registry_cell(key, registry_cell)
    calibration_hashes = dict(identity.calibration_sha256)
    if (
        calibration.latency != latency
        or calibration_hashes.get(latency) != calibration.evidence_sha256
    ):
        raise CampaignError(f"calibration identity differs for {key}")

    root = Path(root).resolve()
    cell_root = root / "cells" / workload / latency
    cell_root.mkdir(parents=True, exist_ok=True)
    prior_attempt = current.get("attempt", 0)
    if isinstance(prior_attempt, bool) or not isinstance(prior_attempt, int):
        raise CampaignError(f"invalid attempt counter for {key}")
    attempt = prior_attempt + 1
    attempt_root = cell_root / "attempts" / f"{attempt:04d}"
    if attempt_root.exists():
        raise CampaignError(f"attempt root already exists: {attempt_root}")
    attempt_root.mkdir(parents=True)
    state["cells"][key] = {
        "workload": workload, "latency": latency, "status": "running",
        "attempt": attempt,
    }
    atomic_write_json(root / "state.json", state)
    try:
        host_result = replay_launcher(
            system="cira-inline", workload=workload, latency=latency,
            cell=registry_cell, root=attempt_root / "raw-host-inline",
            require_device_timing=False,
        )
        cira_result = replay_launcher(
            system="cira", workload=workload, latency=latency,
            cell=registry_cell, root=attempt_root / "raw-cira",
            require_device_timing=True,
        )
        m2ndp_result = m2ndp_launcher(
            workload=workload, latency=latency, cell=registry_cell,
            root=attempt_root / "raw-m2ndp", calibration=calibration,
        )
        replay_sources = {}
        for name, result in (
            ("host_inline", host_result), ("cira_runtime", cira_result),
        ):
            source_path = attempt_root / f"{name}-replay-evidence.json"
            atomic_write_json(source_path, result)
            replay_sources[name] = _evidence_record(source_path)
        payloads = {
            "host_inline": _host_evidence(
                host_result, identity, workload, latency, calibration,
                replay_sources["host_inline"],
            ),
            "cira_runtime": _cira_evidence(
                cira_result, identity, workload, latency, calibration,
                replay_sources["cira_runtime"],
            ),
            "m2ndp": _m2ndp_evidence(
                m2ndp_result, identity, workload, latency, calibration
            ),
        }
        filenames = {
            "host_inline": "host-inline-evidence.json",
            "cira_runtime": "cira-runtime-evidence.json",
            "m2ndp": "m2ndp-evidence.json",
        }
        records = {}
        for name in _EVIDENCE_NAMES:
            path = cell_root / filenames[name]
            atomic_write_json(path, payloads[name])
            records[name] = _evidence_record(path)
        complete = {
            "workload": workload, "latency": latency, "status": "complete",
            "identity_sha256": identity.digest(), "attempt": attempt,
            "evidence": records,
        }
        state["cells"][key] = complete
        atomic_write_json(root / "state.json", state)
        return complete
    except Exception as error:
        state["cells"][key] = {
            "workload": workload, "latency": latency, "status": "failed",
            "attempt": attempt, "attempt_root": str(attempt_root),
            "error": f"{type(error).__name__}: {error}",
        }
        atomic_write_json(root / "state.json", state)
        raise


def _launch_replay(
    *, system: str, latency: str, cell: dict, root: Path,
    require_device_timing: bool, binary: Path, gem5: Path,
    calibration_paths: dict[str, Path], **_unused,
) -> dict:
    trace_meta = _validate_file_record(cell["trace"], "trace")
    fixed = cell.get("fixed_trace")
    fixed_root = None
    if fixed is not None:
        fixed_root = _validate_file_record(fixed, "fixed trace").parent
    options = argparse.Namespace(
        mode="window", system=system, trace=trace_meta.parent,
        fixed_trace=fixed_root,
        window_manifest=(
            None if fixed_root is not None
            else _validate_file_record(cell["window_manifest"], "window manifest")
        ),
        phase=(None if fixed_root is not None else cell["phase"]),
        window_index=(None if fixed_root is not None else cell["window_index"]),
        binary=Path(binary), gem5=Path(gem5), config=GEM5_CONFIG,
        calibration=calibration_paths[latency], cxl_link_delay=latency,
        outdir=Path(root), timeout=0,
        require_device_timing=require_device_timing,
    )
    return replay.run(options)


def _launch_m2ndp(
    *, workload: str, latency: str, cell: dict, root: Path,
    calibration: evidence.CalibrationRow, ndpsim: Path, **_unused,
) -> dict:
    reused = cell.get("m2ndp_evidence")
    if reused is not None:
        source = _validate_file_record(reused, "M2NDP reused evidence")
        return evidence.load_m2ndp_cell(
            source, workload, latency,
            expected_input_sha256=cell["input_sha256"],
        )
    package = _validate_file_record(cell["m2ndp_package"], "M2NDP package")
    functional_path = _validate_file_record(
        cell["functional_evidence"], "FuncSim evidence"
    )
    functional = _load_json(functional_path, "FuncSim evidence")
    calibration_record = _load_json(
        Path(calibration.evidence_path), "calibration evidence"
    )
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    raw_path = root / "ndpsim-evidence.json"
    raw = m2ndp_trace.run_ndpsim_package(
        package, functional_evidence=functional,
        calibration=calibration_record, ndpsim=ndpsim,
        evidence_path=raw_path, cxl_link_delay=latency,
    )
    raw.update({
        "workload": workload,
        "execution_origin": "fresh",
        "calibration_evidence_path": calibration.evidence_path,
        "calibration_evidence_sha256": calibration.evidence_sha256,
    })
    atomic_write_json(raw_path, raw)
    return evidence.load_m2ndp_cell(
        raw_path, workload, latency,
        expected_input_sha256=cell["input_sha256"],
    )


def require_free_space(path: Path, minimum: int = MINIMUM_FREE_BYTES) -> None:
    path = Path(path).resolve()
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if shutil.disk_usage(probe).free < minimum:
        raise CampaignError(f"less than {minimum} bytes free on {probe}")


def repository_commit(*, require_clean: bool = True) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout
    if require_clean and status:
        raise CampaignError("campaign checkout is dirty")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout.strip()


def _code_digest() -> str:
    paths = (
        Path(__file__).resolve(),
        REPO / "scripts/timing_evidence_24cell.py",
        REPO / "scripts/run_matched_breadth_gem5.py",
        REPO / "scripts/m2ndp_workload_trace.py",
        REPO / "util/amu/matched_workloads/trace_replay.cc",
        REPO / "src/mem/cira.hh",
        REPO / "src/mem/cira.cc",
    )
    payload = "\n".join(
        f"{path.relative_to(REPO)} {evidence.sha256_file(path)}"
        for path in sorted(paths)
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--calibration", action="append", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--m5-library", type=Path, required=True)
    parser.add_argument("--funcsim", type=Path, required=True)
    parser.add_argument("--ndpsim", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--coordinate",
        choices=[f"{workload}:{latency}" for workload, latency in evidence.COORDINATES],
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    options = parser.parse_args(argv)
    if len(options.calibration) != len(evidence.LATENCIES):
        parser.error("exactly four --calibration files are required")
    return options


def _load_calibrations(paths: list[Path]) -> dict[str, evidence.CalibrationRow]:
    rows = [evidence.load_calibration(path) for path in paths]
    result = {row.latency: row for row in rows}
    if set(result) != set(evidence.LATENCIES) or len(result) != len(rows):
        raise CampaignError("calibrations do not cover each latency exactly once")
    return result


def main(argv=None) -> int:
    options = parse_args(argv)
    root = options.root.resolve()
    try:
        require_free_space(root)
        require_free_space(REPO)
        registry = load_registry(options.prepared)
        calibrations = _load_calibrations(options.calibration)
        for path, label in (
            (options.inputs, "inputs"), (options.prepared, "prepared"),
            (options.gem5, "gem5"), (options.m5_library, "m5 library"),
            (options.funcsim, "FuncSim"), (options.ndpsim, "NDPSim"),
            (GEM5_CONFIG, "gem5 config"),
        ):
            if not Path(path).is_file():
                raise CampaignError(f"{label} is missing: {path}")

        state_path = root / "state.json"
        if options.resume or options.validate_only:
            state = _load_json(state_path, "campaign state")
            commit = state.get("identity", {}).get("repository_commit")
            if not isinstance(commit, str):
                raise CampaignError("campaign repository commit is missing")
            if repository_commit(require_clean=True) != commit:
                raise CampaignError("campaign repository commit differs")
            build = root / "tools/replay/trace_replay"
        else:
            commit = repository_commit(require_clean=True)
            root.mkdir(parents=True, exist_ok=False)
            build = replay.build_replay_binary(
                root / "tools/replay", m5_library=options.m5_library
            )
        identity = CampaignIdentity(
            repository_commit=commit, code_sha256=_code_digest(),
            input_manifest_sha256=evidence.sha256_file(options.inputs),
            prepared_manifest_sha256=evidence.sha256_file(options.prepared),
            replay_binary_sha256=evidence.sha256_file(build),
            gem5_sha256=evidence.sha256_file(options.gem5),
            m5_library_sha256=evidence.sha256_file(options.m5_library),
            funcsim_sha256=evidence.sha256_file(options.funcsim),
            ndpsim_sha256=evidence.sha256_file(options.ndpsim),
            gem5_config_sha256=evidence.sha256_file(GEM5_CONFIG),
            calibration_sha256=tuple(
                (latency, calibrations[latency].evidence_sha256)
                for latency in evidence.LATENCIES
            ),
        )
        if options.resume or options.validate_only:
            resume_state(state, identity)
        else:
            state = new_state(identity)
            atomic_write_json(state_path, state)
        coordinates = (
            [tuple(options.coordinate.split(":"))]
            if options.coordinate else list(evidence.COORDINATES)
        )
        if options.validate_only:
            incomplete = [
                f"{workload}:{latency}"
                for workload, latency in coordinates
                if not cell_complete(
                    state["cells"][f"{workload}:{latency}"], identity,
                    workload, latency,
                )
            ]
            if incomplete:
                raise CampaignError("incomplete cells: " + ", ".join(incomplete))
            return 0

        calibration_paths = {
            label: Path(row.evidence_path) for label, row in calibrations.items()
        }
        for workload, latency in coordinates:
            execute_cell(
                state, identity, workload, latency,
                registry[f"{workload}:{latency}"], calibrations[latency],
                root=root,
                replay_launcher=lambda **kwargs: _launch_replay(
                    **kwargs, binary=build, gem5=options.gem5,
                    calibration_paths=calibration_paths,
                ),
                m2ndp_launcher=lambda **kwargs: _launch_m2ndp(
                    **kwargs, ndpsim=options.ndpsim,
                ),
            )
        if all(row.get("status") == "complete" for row in state["cells"].values()):
            state["status"] = "complete"
            atomic_write_json(state_path, state)
            atomic_write_json(root / "complete.json", state)
        return 0
    except (CampaignError, evidence.EvidenceError, replay.ReplayError) as error:
        print(f"TIMING_24CELL_FAILED {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
