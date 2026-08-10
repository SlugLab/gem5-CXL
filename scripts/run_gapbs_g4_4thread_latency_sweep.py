#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the formal g4/four-thread PageRank latency sweep."""

import argparse
import dataclasses
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

try:
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts


LATENCIES = ("200ns", "500ns", "1us", "2us")
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
REPO = Path(__file__).resolve().parents[1]


class SweepError(RuntimeError):
    """The sweep contract or a matrix entry failed."""


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    latency: str
    system: str


@dataclasses.dataclass(frozen=True)
class Options:
    graph: Path
    cxlmemuring: Path
    m2ndp_root: Path
    gem5: Path
    variants_build: Path
    outdir: Path
    timeout: int = 0
    resume: bool = False
    stop_after_latency: str | None = None


@dataclasses.dataclass(frozen=True)
class RunPaths:
    root: Path
    status: Path
    runs: Path
    logs: Path


def hash_named_paths(paths):
    """Hash a named immutable input/output set without float conversion."""
    return {
        name: artifacts.sha256_file(Path(path))
        for name, path in sorted(paths.items())
    }


def immutable_passed_record(*, command, input_hashes, output_hashes):
    return {
        "status": "passed",
        "command": [str(item) for item in command],
        "input_hashes": dict(sorted(input_hashes.items())),
        "output_hashes": dict(sorted(output_hashes.items())),
    }


def validate_immutable_passed_record(
    record, *, command, input_hashes, output_hashes
):
    if record.get("status") != "passed":
        raise SweepError("resume record is not passed")
    if record.get("command") != [str(item) for item in command]:
        raise SweepError("resume command differs from recorded command")
    if record.get("input_hashes") != dict(sorted(input_hashes.items())):
        raise SweepError("resume input or binary hash differs")
    if record.get("output_hashes") != dict(sorted(output_hashes.items())):
        raise SweepError("resume output hash differs")
    return record


def make_paths(options):
    root = Path(options.outdir).resolve()
    return RunPaths(
        root=root,
        status=root / "status.json",
        runs=root / "runs",
        logs=root / "logs",
    )


def build_matrix():
    return tuple(
        MatrixEntry(latency, system)
        for latency in LATENCIES
        for system in SYSTEMS
    )


def _m2ndp_command(entry, options, paths):
    run_root = paths.runs / entry.latency / "m2ndp"
    command = [
        sys.executable,
        str(REPO / "scripts/run_m2ndp_g20_pr_spmv.py"),
        "--graph",
        str(Path(options.graph).resolve()),
        "--graph-scale",
        "4",
        "--profile",
        "g4-4thread-sweep",
        "--cxl-link-delay",
        entry.latency,
        "--cxlmemuring",
        str(Path(options.cxlmemuring).resolve()),
        "--m2ndp-root",
        str(Path(options.m2ndp_root).resolve()),
        "--gem5",
        str(Path(options.gem5).resolve()),
        "--outdir",
        str(run_root),
        "--timeout",
        str(options.timeout),
    ]
    if (run_root / "status.json").exists():
        command.append("--resume")
    if entry.system == "vanilla":
        command.extend(("--stop-after", "gem5_baseline"))
    return command


def _matched_command(entry, options, paths):
    return [
        sys.executable,
        str(REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py"),
        "--gem5",
        str(Path(options.gem5).resolve()),
        "--graph",
        str(Path(options.graph).resolve()),
        "--graph-scale",
        "4",
        "--profile",
        "g4-4thread-sweep",
        "--cxl-link-delay",
        entry.latency,
        "--variants-build",
        str(Path(options.variants_build).resolve()),
        "--kind",
        entry.system,
        "--checkpoint-root",
        str(paths.runs / entry.latency / "checkpoints"),
        "--outdir",
        str(paths.runs / entry.latency / entry.system),
        "--timeout",
        str(options.timeout),
    ]


def command_for_action(entry, options, paths):
    if entry.system in {"vanilla", "m2ndp"}:
        return _m2ndp_command(entry, options, paths)
    if entry.system in {"amu", "cira"}:
        return _matched_command(entry, options, paths)
    raise SweepError(f"unsupported system: {entry.system}")


def output_for_action(entry, paths):
    latency_root = paths.runs / entry.latency
    if entry.system == "vanilla":
        return latency_root / "m2ndp/gem5/run/summary.csv"
    if entry.system == "m2ndp":
        return latency_root / "m2ndp/summary.csv"
    if entry.system in {"amu", "cira"}:
        return latency_root / entry.system / "summary.csv"
    raise SweepError(f"unsupported system: {entry.system}")


def _pending_record():
    return {
        "status": "pending",
        "output": None,
        "output_sha256": None,
        "command": [],
        "log": None,
        "error": None,
    }


def new_state(options=None):
    profile = profiles.get_profile("g4-4thread-sweep")
    external = {}
    if options is not None:
        external = {
            "graph": str(Path(options.graph).resolve()),
            "cxlmemuring": str(Path(options.cxlmemuring).resolve()),
            "m2ndp_root": str(Path(options.m2ndp_root).resolve()),
            "gem5": str(Path(options.gem5).resolve()),
            "variants_build": str(Path(options.variants_build).resolve()),
        }
    return {
        "schema": 1,
        "contract": {
            "profile": profile.name,
            "graph_scale": profile.graph_scale,
            "graph_sha256": profile.graph_sha256,
            "cores": profile.cores,
            "threads": profile.threads,
            "trials": profile.trials,
            "measured_trial": profile.measured_trial,
            "page_rank_iterations": profile.page_rank_iterations,
            "latencies": list(profile.latencies),
            "all_memory_cxl": True,
            "stop_after_latency": (
                options.stop_after_latency if options is not None else None
            ),
            **external,
        },
        "latencies": {
            latency: {
                system: _pending_record() for system in SYSTEMS
            }
            for latency in LATENCIES
        },
    }


def _status(record):
    return record if isinstance(record, str) else record.get("status")


def next_action(state):
    for entry in build_matrix():
        record = state["latencies"][entry.latency][entry.system]
        if _status(record) == "failed":
            raise SweepError(f"{entry.latency}/{entry.system} failed")
    for entry in build_matrix():
        record = state["latencies"][entry.latency][entry.system]
        if _status(record) != "passed":
            return entry
    return None


def record_pass(state, latency, system, output):
    output = Path(output).resolve()
    if not output.is_file():
        raise SweepError(f"completed output is missing: {output}")
    current = state["latencies"][latency][system]
    base = current if isinstance(current, dict) else _pending_record()
    state["latencies"][latency][system] = {
        **base,
        "status": "passed",
        "output": str(output),
        "output_sha256": artifacts.sha256_file(output),
    }


def invalidate_changed_outputs(state, root):
    root = Path(root).resolve()
    invalidated = False
    for entry in build_matrix():
        record = state["latencies"][entry.latency][entry.system]
        if invalidated:
            state["latencies"][entry.latency][entry.system] = (
                _pending_record()
            )
            continue
        if _status(record) != "passed":
            continue
        output = Path(record.get("output", ""))
        if not output.is_absolute():
            output = root / output
        try:
            actual = artifacts.sha256_file(output)
        except OSError:
            actual = None
        if actual != record.get("output_sha256"):
            invalidated = True
            state["latencies"][entry.latency][entry.system] = (
                _pending_record()
            )
    return invalidated


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SweepError(f"invalid sweep state: {error}") from error
    if not isinstance(value, dict):
        raise SweepError("sweep state must be a JSON object")
    return value


def _reset_failed_for_resume(state):
    reset = False
    for entry in build_matrix():
        record = state["latencies"][entry.latency][entry.system]
        if _status(record) in {"failed", "running"}:
            reset = True
        if reset:
            state["latencies"][entry.latency][entry.system] = (
                _pending_record()
            )


def load_or_create_state(options, paths):
    expected = new_state(options)
    if paths.status.exists():
        if not options.resume:
            raise SweepError(f"sweep state exists; use --resume: {paths.status}")
        state = _load_json(paths.status)
        if (
            state.get("schema") != expected["schema"]
            or state.get("contract") != expected["contract"]
        ):
            raise SweepError("resume contract differs from recorded sweep")
        invalidate_changed_outputs(state, paths.root)
        _reset_failed_for_resume(state)
        artifacts.atomic_write_json(paths.status, state)
        return state
    if options.resume:
        raise SweepError(
            f"--resume requested but state is missing: {paths.status}"
        )
    artifacts.atomic_write_json(paths.status, expected)
    return expected


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def reached_stop_boundary(entry, options):
    return (
        options.stop_after_latency is not None
        and entry.latency == options.stop_after_latency
        and entry.system == SYSTEMS[-1]
    )


def run_action(entry, state, options, paths):
    command = command_for_action(entry, options, paths)
    log = paths.logs / f"{entry.latency}-{entry.system}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_pending_record(),
        "status": "running",
        "command": [str(item) for item in command],
        "log": str(log.resolve()),
        "started_at": _utc_now(),
    }
    state["latencies"][entry.latency][entry.system] = record
    artifacts.atomic_write_json(paths.status, state)
    with log.open("w", encoding="utf-8") as stream:
        try:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if options.timeout == 0 else options.timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
    if returncode != 0:
        message = f"{entry.latency}/{entry.system} exited {returncode}"
        record.update(
            status="failed",
            error=message,
            finished_at=_utc_now(),
            returncode=returncode,
        )
        artifacts.atomic_write_json(paths.status, state)
        raise SweepError(message)
    output = output_for_action(entry, paths)
    record_pass(state, entry.latency, entry.system, output)
    state["latencies"][entry.latency][entry.system].update(
        finished_at=_utc_now(),
        returncode=0,
    )
    artifacts.atomic_write_json(paths.status, state)
    return returncode


def validate_options(options, paths):
    if options.timeout < 0:
        raise SweepError("--timeout must be nonnegative")
    for label, path in (
        ("graph", options.graph),
        ("gem5", options.gem5),
    ):
        if not Path(path).is_file():
            raise SweepError(f"{label} is missing: {path}")
    for label, path in (
        ("CXLMemUring", options.cxlmemuring),
        ("M2NDP", options.m2ndp_root),
        ("variant build", options.variants_build),
    ):
        if not Path(path).is_dir():
            raise SweepError(f"{label} is missing: {path}")
    profile = profiles.get_profile("g4-4thread-sweep")
    try:
        profiles.validate_graph(profile, options.graph)
    except profiles.ProfileError as error:
        raise SweepError(str(error)) from error
    variant_manifest = Path(options.variants_build) / "manifest.json"
    if not variant_manifest.is_file():
        raise SweepError(f"variant manifest is missing: {variant_manifest}")
    validate_cira_partition_contract(
        _load_json(variant_manifest), profile
    )
    if paths.root in {
        Path(options.cxlmemuring).resolve(),
        Path(options.m2ndp_root).resolve(),
        Path(options.variants_build).resolve(),
    }:
        raise SweepError("output root must be isolated from input roots")


def validate_cira_partition_contract(manifest, profile):
    try:
        distance = int(manifest["cira_prefetch_distance"])
    except (KeyError, TypeError, ValueError) as error:
        raise SweepError(
            "variant manifest lacks a valid CIRA prefetch distance"
        ) from error
    nodes = 1 << profile.graph_scale
    minimum_partition = nodes // profile.threads
    maximum_distance = minimum_partition - 1
    if distance < 1 or distance > maximum_distance:
        raise SweepError(
            f"CIRA prefetch distance {distance} does not fit the g4 "
            f"static thread partition of {minimum_partition} rows; "
            f"expected 1..{maximum_distance}"
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--variants-build", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-latency", choices=LATENCIES)
    args = parser.parse_args(argv)
    return Options(
        graph=args.graph.resolve(),
        cxlmemuring=args.cxlmemuring.resolve(),
        m2ndp_root=args.m2ndp_root.resolve(),
        gem5=args.gem5.resolve(),
        variants_build=args.variants_build.resolve(),
        outdir=args.outdir.resolve(),
        timeout=args.timeout,
        resume=args.resume,
        stop_after_latency=args.stop_after_latency,
    )


def main(argv=None):
    options = parse_args(argv)
    paths = make_paths(options)
    try:
        validate_options(options, paths)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.runs.mkdir(parents=True, exist_ok=True)
        paths.logs.mkdir(parents=True, exist_ok=True)
        state = load_or_create_state(options, paths)
        while True:
            entry = next_action(state)
            if entry is None:
                print(f"G4_SWEEP_COMPLETE status={paths.status}")
                return 0
            print(
                "G4_SWEEP_ACTION_BEGIN "
                f"latency={entry.latency} system={entry.system}",
                flush=True,
            )
            run_action(entry, state, options, paths)
            print(
                "G4_SWEEP_ACTION_PASS "
                f"latency={entry.latency} system={entry.system}",
                flush=True,
            )
            if reached_stop_boundary(entry, options):
                print(
                    "G4_SWEEP_STOP_BOUNDARY "
                    f"latency={entry.latency} status={paths.status}",
                    flush=True,
                )
                return 0
    except (
        SweepError,
        artifacts.EvidenceError,
        profiles.ProfileError,
        OSError,
        KeyError,
    ) as error:
        print(f"G4_SWEEP_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
