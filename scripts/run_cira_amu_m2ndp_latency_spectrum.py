#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run four immutable CXL-latency breadth campaigns and aggregate them."""

import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import cxl_latency_spectrum as latency
    from scripts import run_cira_amu_m2ndp_breadth as breadth
except ImportError:
    import cross_system_contract as contract
    import cxl_latency_spectrum as latency
    import run_cira_amu_m2ndp_breadth as breadth


REPO = Path(__file__).resolve().parents[1]
WORKLOADS = breadth.WORKLOADS
SYSTEMS = breadth.TIMING_SYSTEMS
REQUIRED_SHARED = ("inputs", "calibration", "prepared")


class SpectrumError(RuntimeError):
    """A shared object, child campaign, or aggregate gate failed."""


class SpectrumInputError(SpectrumError):
    """A required immutable spectrum input is missing or invalid."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SpectrumError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise SpectrumError(f"{label} must be a JSON object")
    return value


def coordinates():
    return tuple(
        (label, workload, system)
        for label in latency.LABELS
        for workload in WORKLOADS
        for system in SYSTEMS
    )


def validate_shared(shared):
    if (
        not isinstance(shared, dict)
        or set(shared) != set(REQUIRED_SHARED)
        or not contract.verify_named_hashes(shared)
    ):
        raise SpectrumError("shared objects are not content-addressed")
    return {
        name: {
            "path": str(Path(shared[name]["path"]).resolve()),
            "sha256": shared[name]["sha256"],
        }
        for name in REQUIRED_SHARED
    }


def new_state(shared, identity):
    shared = validate_shared(shared)
    if not isinstance(identity, contract.ExperimentIdentity):
        raise SpectrumError("aggregate identity has the wrong type")
    return {
        "schema": 1,
        "status": "planned",
        "identity": dataclasses.asdict(identity),
        "identity_sha256": identity.digest(),
        "shared": shared,
        "latencies": {
            label: {"status": "pending", "root": f"latency/{label}"}
            for label in latency.LABELS
        },
    }


def _child_identity(path):
    record = _load_json(path, "child identity")
    try:
        identity = contract.ExperimentIdentity(**record["identity"])
    except (KeyError, TypeError, contract.ContractError) as error:
        raise SpectrumError("child campaign identity is invalid") from error
    if (
        record.get("schema") != 1
        or record.get("digest") != identity.digest()
    ):
        raise SpectrumError("child campaign identity differs")
    return identity, identity.digest()


def validate_child(
    root, label, shared, *, expected_identity_sha256=None,
):
    if label not in latency.LABELS:
        raise SpectrumError(f"unsupported child latency: {label}")
    shared = validate_shared(shared)
    root = Path(root).resolve()
    inconclusive_path = root / "inconclusive.json"
    complete_path = root / "complete.json"
    if inconclusive_path.is_file():
        inconclusive = _load_json(
            inconclusive_path, "child inconclusive manifest"
        )
        if inconclusive.get("status") == "inconclusive":
            raise SpectrumError("child campaign is inconclusive")
    if not complete_path.is_file():
        raise SpectrumError("child complete manifest is missing")
    identity_path = root / "identity.json"
    if not identity_path.is_file():
        raise SpectrumError("child campaign identity is missing")
    identity, digest = _child_identity(identity_path)
    if expected_identity_sha256 is not None and digest != expected_identity_sha256:
        raise SpectrumError("child campaign identity differs")
    if (
        identity.input_manifest_sha256 != shared["inputs"]["sha256"]
        or identity.calibration_manifest_sha256
        != shared["calibration"]["sha256"]
        or identity.trace_sha256 != shared["prepared"]["sha256"]
    ):
        raise SpectrumError("child campaign shared identity differs")
    complete = _load_json(complete_path, "child complete manifest")
    if complete.get("status") == "inconclusive":
        raise SpectrumError("child campaign is inconclusive")
    if complete.get("status") != "complete":
        raise SpectrumError("child campaign is not complete")
    if (
        complete.get("identity_sha256") != digest
        or complete.get("cxl_link_delay") != label
        or complete.get("cxl_link_delay_ticks") != latency.ticks(label)
    ):
        raise SpectrumError("child campaign identity differs")
    evidence = complete.get("evidence_files")
    if evidence is not None and not contract.verify_named_hashes(evidence):
        raise SpectrumError("child campaign evidence hashes differ")
    return {
        "root": str(root),
        "identity_sha256": digest,
        "identity": dataclasses.asdict(identity),
        "complete": {
            "path": str(complete_path),
            "sha256": _sha256_file(complete_path),
        },
    }


def _command_record(command):
    if not isinstance(command, (list, tuple)) or not command or any(
        not isinstance(item, (str, int)) for item in command
    ):
        raise SpectrumError("child command is invalid")
    normalized = [str(item) for item in command]
    return {
        "argv": normalized,
        "sha256": hashlib.sha256(contract.canonical_json(normalized)).hexdigest(),
    }


def record_child(state, label, root, command):
    if state.get("status") != "planned" or label not in latency.LABELS:
        raise SpectrumError("child campaign cannot be recorded")
    try:
        row = state["latencies"][label]
    except (KeyError, TypeError) as error:
        raise SpectrumError("aggregate latency matrix differs") from error
    expected = row.get("identity_sha256")
    child = validate_child(
        root, label, state.get("shared"),
        expected_identity_sha256=expected,
    )
    command_record = _command_record(command)
    updated = {
        **row,
        "status": "complete",
        "child_root": child["root"],
        "identity_sha256": child["identity_sha256"],
        "command": command_record,
        "complete": child["complete"],
    }
    if row.get("status") == "complete" and row != updated:
        raise SpectrumError("completed child campaign record differs")
    state["latencies"][label] = updated
    return state


def complete_state(state, root):
    if state.get("status") != "planned" or set(
        state.get("latencies", {})
    ) != set(latency.LABELS):
        raise SpectrumError("aggregate state is invalid")
    validate_shared(state.get("shared"))
    if any(
        state["latencies"][label].get("status") != "complete"
        for label in latency.LABELS
    ):
        raise SpectrumError("all four latency campaigns must complete")
    for label in latency.LABELS:
        row = state["latencies"][label]
        child = validate_child(
            row.get("child_root"), label, state["shared"],
            expected_identity_sha256=row.get("identity_sha256"),
        )
        if child["complete"] != row.get("complete"):
            raise SpectrumError("child complete manifest hash differs")
        if _command_record(row.get("command", {}).get("argv")) != row.get(
            "command"
        ):
            raise SpectrumError("child command hash differs")
    state["status"] = "complete"
    state["coordinate_count"] = len(coordinates())
    contract.atomic_write_json(Path(root).resolve() / "complete.json", state)
    return state


def _record(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise SpectrumInputError(f"shared object is missing: {path}")
    return {"path": str(path), "sha256": _sha256_file(path)}


def _aggregate_identity(shared):
    code_paths = (
        Path(__file__).resolve(),
        Path(breadth.__file__).resolve(),
        Path(latency.__file__).resolve(),
    )
    code_sha256 = hashlib.sha256(contract.canonical_json({
        path.name: _sha256_file(path) for path in code_paths
    })).hexdigest()
    config_sha256 = hashlib.sha256(contract.canonical_json({
        "latencies": latency.LABELS,
        "workloads": WORKLOADS,
        "systems": SYSTEMS,
    })).hexdigest()
    return contract.ExperimentIdentity(
        code_sha256=code_sha256,
        input_manifest_sha256=shared["inputs"]["sha256"],
        calibration_manifest_sha256=shared["calibration"]["sha256"],
        trace_sha256=shared["prepared"]["sha256"],
        config_sha256=config_sha256,
    )


def _bind_prepared(child_root, prepared):
    child_root = Path(child_root).resolve()
    source = Path(prepared["path"]).resolve()
    directory = child_root / "prepared"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "manifest.json"
    if target.exists() or target.is_symlink():
        if (
            not target.is_file()
            or target.resolve() != source
            or _sha256_file(target) != prepared["sha256"]
        ):
            raise SpectrumError("child prepared-manifest binding differs")
        return target
    os.symlink(source, target)
    if _sha256_file(target) != prepared["sha256"]:
        raise SpectrumError("child prepared-manifest hash differs")
    return target


def _child_command(shared, root, label, *, resume):
    command = [
        sys.executable,
        str(Path(breadth.__file__).resolve()),
        "--inputs", shared["inputs"]["path"],
        "--calibration", shared["calibration"]["path"],
        "--root", str(Path(root).resolve()),
        "--cxl-link-delay", label,
    ]
    if resume:
        command.append("--resume")
    return command


def run(options):
    root = Path(options.root).resolve()
    shared = validate_shared({
        "inputs": _record(options.inputs),
        "calibration": _record(options.calibration),
        "prepared": _record(options.prepared),
    })
    identity = _aggregate_identity(shared)
    identity_path = root / "identity.json"
    state_path = root / "state.json"
    if options.resume:
        if not identity_path.is_file() or not state_path.is_file():
            raise SpectrumError("resume aggregate identity or state is missing")
        try:
            contract.bind_root(root, identity)
        except contract.ContractError as error:
            raise SpectrumError(str(error)) from error
        state = _load_json(state_path, "aggregate state")
        if (
            state.get("identity_sha256") != identity.digest()
            or validate_shared(state.get("shared")) != shared
            or state.get("status") != "planned"
        ):
            raise SpectrumError("resume aggregate identity differs")
    else:
        if state_path.exists() or identity_path.exists():
            raise SpectrumError("aggregate evidence root exists; use --resume")
        try:
            contract.bind_root(root, identity)
        except contract.ContractError as error:
            raise SpectrumError(str(error)) from error
        state = new_state(shared, identity)
        contract.atomic_write_json(state_path, state)
    for label in latency.LABELS:
        row = state["latencies"][label]
        child_root = root / row["root"]
        if row.get("status") == "complete":
            validate_child(
                child_root, label, shared,
                expected_identity_sha256=row.get("identity_sha256"),
            )
            continue
        _bind_prepared(child_root, shared["prepared"])
        resume_child = (child_root / "identity.json").is_file()
        command = _child_command(
            shared, child_root, label, resume=resume_child
        )
        row["command"] = _command_record(command)
        row["status"] = "running"
        contract.atomic_write_json(state_path, state)
        log = child_root / "spectrum-driver.log"
        with log.open("a", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=None,
            )
        row["status"] = "pending"
        if completed.returncode != 0:
            contract.atomic_write_json(state_path, state)
            raise SpectrumError(
                f"{label} child campaign exited {completed.returncode}; see {log}"
            )
        record_child(state, label, child_root, command)
        contract.atomic_write_json(state_path, state)
    return complete_state(state, root)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    root = Path(options.root).resolve()
    try:
        complete = run(options)
        print(
            f"SPECTRUM_COMPLETE coordinates={complete['coordinate_count']} "
            f"manifest={root / 'complete.json'}"
        )
        return 0
    except (
        SpectrumError, OSError, contract.ContractError
    ) as error:
        root.mkdir(parents=True, exist_ok=True)
        status = "failed_input" if isinstance(
            error, SpectrumInputError
        ) else "inconclusive" if "inconclusive" in str(error) else "failed"
        contract.atomic_write_json(root / f"{status}.json", {
            "schema": 1, "status": status, "error": str(error),
        })
        print(f"SPECTRUM_{status.upper()} error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
