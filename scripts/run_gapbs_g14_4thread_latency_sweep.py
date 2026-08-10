#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the resumable g14/four-thread real-CXL formal latency matrix."""

import argparse
import csv
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from scripts import build_gapbs_matched_pr_spmv_variants as variant_builder
    from scripts import cira_lead_policy
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
    from scripts import run_gapbs_g4_4thread_latency_sweep as shared
    from scripts import run_gapbs_matched_pr_spmv_variants as matched_runner
except ImportError:
    import build_gapbs_matched_pr_spmv_variants as variant_builder
    import cira_lead_policy
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts
    import run_gapbs_g4_4thread_latency_sweep as shared
    import run_gapbs_matched_pr_spmv_variants as matched_runner


REPO = Path(__file__).resolve().parents[1]
EXTERNAL_ROOT = Path("/mnt/disk0/gem5-CXL-g14-eval")
STABLE_LINK = REPO / "m5out/g14-real-cxl-eval"
MIN_FREE_BYTES = 100 * 1024**3
LATENCIES = ("200ns", "500ns", "1us", "2us")
LATENCY_NS = {"200ns": 200, "500ns": 500, "1us": 1000, "2us": 2000}
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")


class SweepError(RuntimeError):
    """The formal matrix contract or a matrix action failed."""


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    latency: str
    system: str


@dataclasses.dataclass(frozen=True)
class Options:
    root: Path
    graph: Path
    graph_manifest: Path
    policy: Path
    qualification: Path
    gem5: Path
    config: Path
    cxlmemuring: Path
    m2ndp_root: Path
    m5_library: Path
    baseline_build: Path
    cxx: str
    timeout: int
    resume: bool
    only_latency: str | None
    stop_after: str | None


@dataclasses.dataclass(frozen=True)
class RunPaths:
    root: Path
    status: Path
    runs: Path
    logs: Path
    variants: Path


def make_paths(options):
    root = Path(options.root).resolve()
    return RunPaths(
        root=root,
        status=root / "formal/status.json",
        runs=root / "formal/runs",
        logs=root / "formal/logs",
        variants=root / "formal/variants",
    )


def build_matrix():
    return tuple(
        MatrixEntry(latency, system)
        for latency in LATENCIES
        for system in SYSTEMS
    )


def lead_for_latency(selected_1us, latency):
    try:
        latency_ns = LATENCY_NS[latency]
    except KeyError as error:
        raise SweepError(f"unsupported latency: {latency}") from error
    try:
        return cira_lead_policy.lead_blocks_for_latency(
            int(selected_1us), latency_ns
        )
    except (ValueError, cira_lead_policy.LeadPolicyError) as error:
        raise SweepError(str(error)) from error


def load_policy(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SweepError(f"invalid CIRA policy: {error}") from error
    required = {
        "schema", "source_profile", "selected_1us_lead_blocks",
        "row_block_size", "candidate_1us_lead_blocks", "result_hashes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != 1
        or value.get("row_block_size") != 64
        or value.get("selected_1us_lead_blocks")
        not in cira_lead_policy.CANDIDATE_1US_LEADS
        or value.get("candidate_1us_lead_blocks")
        != list(cira_lead_policy.CANDIDATE_1US_LEADS)
        or not isinstance(value.get("result_hashes"), dict)
        or not value["result_hashes"]
    ):
        raise SweepError("CIRA policy violates the qualification contract")
    return value


def require_external_root(
    root, *, stable_link=STABLE_LINK, expected_root=EXTERNAL_ROOT,
    min_free_bytes=MIN_FREE_BYTES
):
    root = Path(root).resolve()
    expected_root = Path(expected_root).resolve()
    stable_link = Path(stable_link)
    if root == Path("/"):
        raise SweepError("formal output cannot use the filesystem root")
    if root != expected_root:
        raise SweepError(
            f"formal root {root} is not the frozen external root {expected_root}"
        )
    if not stable_link.is_symlink() or stable_link.resolve() != root:
        raise SweepError(
            f"stable link {stable_link} must resolve exactly to {root}"
        )
    free = shutil.disk_usage(root).free
    if free < min_free_bytes:
        raise SweepError(
            f"external root has {free} free bytes; at least 100 GiB required"
        )
    return free


def passed_record(*, command, input_hashes, output_hashes):
    return shared.immutable_passed_record(
        command=command,
        input_hashes=input_hashes,
        output_hashes=output_hashes,
    )


def validate_passed_record(
    record, *, command, input_hashes, output_hashes
):
    try:
        return shared.validate_immutable_passed_record(
            record,
            command=command,
            input_hashes=input_hashes,
            output_hashes=output_hashes,
        )
    except shared.SweepError as error:
        raise SweepError(str(error)) from error


def _pending_record():
    return {
        "status": "pending",
        "command": [],
        "input_paths": {},
        "input_hashes": {},
        "output_paths": {},
        "output_hashes": {},
        "log": None,
        "returncode": None,
        "error": None,
    }


def new_state(contract=None):
    return {
        "schema": 1,
        "contract": contract or {},
        "latencies": {
            latency: {system: _pending_record() for system in SYSTEMS}
            for latency in LATENCIES
        },
    }


def _selected(entry, options):
    return options is None or (
        options.only_latency is None or entry.latency == options.only_latency
    )


def next_action(state, options=None):
    for entry in build_matrix():
        if not _selected(entry, options):
            continue
        record = state["latencies"][entry.latency][entry.system]
        if record.get("status") == "failed" and options is None:
            raise SweepError(f"{entry.latency}/{entry.system} failed")
        if record.get("status") != "passed":
            return entry
    return None


def _m2ndp_command(entry, options, paths):
    run_root = paths.runs / entry.latency / "m2ndp"
    command = [
        sys.executable,
        str(REPO / "scripts/run_m2ndp_g20_pr_spmv.py"),
        "--graph", str(Path(options.graph).resolve()),
        "--graph-scale", "14",
        "--profile", "g14-4thread-sweep",
        "--graph-manifest", str(Path(options.graph_manifest).resolve()),
        "--cxl-link-delay", entry.latency,
        "--cxlmemuring", str(Path(options.cxlmemuring).resolve()),
        "--m2ndp-root", str(Path(options.m2ndp_root).resolve()),
        "--gem5", str(Path(options.gem5).resolve()),
        "--outdir", str(run_root),
        "--timeout", str(options.timeout),
    ]
    if entry.system == "m2ndp":
        command.append("--resume")
    if entry.system == "vanilla":
        command.extend(("--stop-after", "gem5_baseline"))
    return command


def _matched_command(entry, options, paths):
    return [
        sys.executable,
        str(REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py"),
        "--gem5", str(Path(options.gem5).resolve()),
        "--config", str(Path(options.config).resolve()),
        "--graph", str(Path(options.graph).resolve()),
        "--graph-scale", "14",
        "--profile", "g14-4thread-sweep",
        "--graph-manifest", str(Path(options.graph_manifest).resolve()),
        "--cxl-link-delay", entry.latency,
        "--variants-build", str(paths.variants / entry.latency),
        "--kind", entry.system,
        "--checkpoint-root", str(paths.runs / entry.latency / "checkpoints"),
        "--outdir", str(paths.runs / entry.latency / entry.system),
        "--timeout", str(options.timeout),
    ]


def command_for_action(entry, options, paths):
    if entry.system in {"vanilla", "m2ndp"}:
        return _m2ndp_command(entry, options, paths)
    if entry.system in {"amu", "cira"}:
        return _matched_command(entry, options, paths)
    raise SweepError(f"unsupported system: {entry.system}")


def execution_command(entry, command, paths):
    """Add retry-only controls without changing the recorded experiment."""
    command = list(command)
    if entry.system == "vanilla":
        status = paths.runs / entry.latency / "m2ndp/status.json"
        if status.exists():
            command.append("--resume")
    return command


def output_for_action(entry, paths):
    latency_root = paths.runs / entry.latency
    if entry.system == "vanilla":
        return latency_root / "m2ndp/gem5/run/summary.csv"
    if entry.system == "m2ndp":
        return latency_root / "m2ndp/summary.csv"
    return latency_root / entry.system / "summary.csv"


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SweepError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise SweepError(f"{label} must be a JSON object")
    return value


def _read_one_row(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as error:
        raise SweepError(f"cannot read action summary: {error}") from error
    if len(rows) != 1:
        raise SweepError(f"{path}: expected exactly one summary row")
    return rows[0]


def _variant_manifest(options, paths, latency):
    policy = load_policy(options.policy)
    lead = lead_for_latency(policy["selected_1us_lead_blocks"], latency)
    build = paths.variants / latency
    manifest_path = build / "manifest.json"
    if not manifest_path.exists():
        variant_builder.main(
            [
                "--baseline-build", str(options.baseline_build),
                "--outdir", str(build),
                "--cxlmemuring", str(options.cxlmemuring),
                "--m5-library", str(options.m5_library),
                "--cxx", options.cxx,
                "--cira-prefetch-distance", str(lead),
                "--cira-row-batch", "64",
            ]
        )
    manifest, variants = matched_runner.load_manifest(manifest_path)
    if int(manifest.get("cira_lead_blocks", -1)) != lead:
        raise SweepError(
            f"{latency} variant lead does not match frozen policy scaling"
        )
    return manifest_path, variants


def _input_paths(entry, options, paths):
    inputs = {
        "graph": Path(options.graph),
        "graph_manifest": Path(options.graph_manifest),
        "policy": Path(options.policy),
        "qualification": Path(options.qualification),
        "gem5": Path(options.gem5),
        "config": Path(options.config),
    }
    if entry.system in {"amu", "cira"}:
        manifest_path, variants = _variant_manifest(
            options, paths, entry.latency
        )
        inputs["variant_manifest"] = manifest_path
        inputs["binary"] = Path(variants[entry.system]["binary"])
        inputs["baseline_manifest"] = Path(options.baseline_build) / "manifest.json"
    return {name: Path(path).resolve() for name, path in inputs.items()}


def _summary_evidence_paths(entry, paths):
    summary = output_for_action(entry, paths)
    row = _read_one_row(summary)
    output = {"summary": summary.resolve()}
    if entry.system == "m2ndp":
        root = paths.runs / entry.latency / "m2ndp"
        required = {
            "m2ndp_manifest": root / "manifest.json",
            "m2ndp_status": root / "status.json",
            "raw": root / "reference/scores.raw",
            "funcsim_raw": root / "funcsim/scores.u32",
            "calibration": root / "calibration/calibration.json",
            "workload_manifest": root / "build/manifest.json",
            "workload_binary": root / "build/bin/pr_spmv",
        }
        output.update({name: path.resolve() for name, path in required.items()})
        baseline_summary = root / "gem5/run/summary.csv"
        baseline = _read_one_row(baseline_summary)
        output["baseline_summary"] = baseline_summary.resolve()
        output["config"] = (Path(baseline["run_dir"]) / "config.ini").resolve()
        output["checkpoint"] = Path(baseline["checkpoint_manifest"]).resolve()
        return output
    if entry.system == "vanilla":
        root = paths.runs / entry.latency / "m2ndp"
        raw = root / "reference/scores.raw"
        output["workload_manifest"] = (root / "build/manifest.json").resolve()
        output["workload_binary"] = (root / "build/bin/pr_spmv").resolve()
    else:
        manifest = _load_json(
            paths.variants / entry.latency / "manifest.json",
            "variant manifest",
        )
        variants = {item["kind"]: item for item in manifest["variants"]}
        raw = Path(variants[entry.system]["reference_raw"])
    output["raw"] = raw.resolve()
    output["config"] = (Path(row["run_dir"]) / "config.ini").resolve()
    output["checkpoint"] = Path(row["checkpoint_manifest"]).resolve()
    return output


def _validate_bit_exact(entry, paths, outputs):
    if entry.system not in {"amu", "cira"}:
        return
    vanilla_raw = (
        paths.runs / entry.latency / "m2ndp/reference/scores.raw"
    )
    if artifacts.sha256_file(vanilla_raw) != artifacts.sha256_file(outputs["raw"]):
        raise SweepError(f"{entry.latency}/{entry.system} raw vector is not bit-exact")


def _state_contract(options):
    files = {
        "graph": options.graph,
        "graph_manifest": options.graph_manifest,
        "policy": options.policy,
        "qualification": options.qualification,
        "gem5": options.gem5,
        "config": options.config,
        "baseline_manifest": Path(options.baseline_build) / "manifest.json",
    }
    return {
        "profile": "g14-4thread-sweep",
        "graph_scale": 14,
        "cores": 4,
        "threads": 4,
        "trials": 2,
        "measured_trial": 1,
        "page_rank_iterations": 20,
        "latencies": list(LATENCIES),
        "systems": list(SYSTEMS),
        "all_memory_cxl": True,
        "paths": {name: str(Path(path).resolve()) for name, path in files.items()},
        "hashes": shared.hash_named_paths(files),
    }


def _load_or_create_state(options, paths):
    contract = _state_contract(options)
    if paths.status.exists():
        if not options.resume:
            raise SweepError(f"formal state exists; use --resume: {paths.status}")
        state = _load_json(paths.status, "formal state")
        if state.get("schema") != 1 or state.get("contract") != contract:
            raise SweepError("resume contract differs from recorded formal sweep")
        return state
    state = new_state(contract)
    artifacts.atomic_write_json(paths.status, state)
    return state


def _validate_or_run(entry, options, paths, state):
    command = command_for_action(entry, options, paths)
    input_paths = _input_paths(entry, options, paths)
    input_hashes = shared.hash_named_paths(input_paths)
    record = state["latencies"][entry.latency][entry.system]
    if record.get("status") == "passed":
        output_paths = {
            name: Path(path) for name, path in record["output_paths"].items()
        }
        validate_passed_record(
            record, command=command, input_hashes=input_hashes,
            output_hashes=shared.hash_named_paths(output_paths),
        )
        return
    log = paths.logs / f"{entry.latency}-{entry.system}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    state["latencies"][entry.latency][entry.system] = {
        **_pending_record(),
        "status": "running",
        "command": [str(item) for item in command],
        "input_paths": {name: str(path) for name, path in input_paths.items()},
        "input_hashes": input_hashes,
        "log": str(log.resolve()),
    }
    artifacts.atomic_write_json(paths.status, state)
    with log.open("a", encoding="utf-8") as stream:
        try:
            completed = subprocess.run(
                execution_command(entry, command, paths), cwd=REPO,
                stdout=stream, stderr=subprocess.STDOUT,
                text=True, timeout=None if options.timeout == 0 else options.timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
    if returncode != 0:
        message = f"{entry.latency}/{entry.system} exited {returncode}"
        state["latencies"][entry.latency][entry.system].update(
            status="failed", returncode=returncode, error=message
        )
        artifacts.atomic_write_json(paths.status, state)
        raise SweepError(message)
    outputs = _summary_evidence_paths(entry, paths)
    _validate_bit_exact(entry, paths, outputs)
    output_hashes = shared.hash_named_paths(outputs)
    state["latencies"][entry.latency][entry.system] = {
        **passed_record(
            command=command, input_hashes=input_hashes,
            output_hashes=output_hashes,
        ),
        "input_paths": {name: str(path) for name, path in input_paths.items()},
        "output_paths": {name: str(path) for name, path in outputs.items()},
        "log": str(log.resolve()),
        "returncode": 0,
        "error": None,
    }
    artifacts.atomic_write_json(paths.status, state)


def _reached_stop(entry, options):
    return options.stop_after is not None and entry.system == options.stop_after


def _validate_options(options):
    if options.timeout < 0:
        raise SweepError("--timeout must be nonnegative")
    require_external_root(options.root)
    for label, path in (
        ("graph", options.graph), ("graph manifest", options.graph_manifest),
        ("policy", options.policy), ("qualification", options.qualification),
        ("gem5", options.gem5), ("config", options.config),
        ("m5 library", options.m5_library),
        ("baseline build manifest", Path(options.baseline_build) / "manifest.json"),
    ):
        if not Path(path).is_file():
            raise SweepError(f"{label} is missing: {path}")
    for label, path in (
        ("CXLMemUring", options.cxlmemuring),
        ("M2NDP", options.m2ndp_root),
    ):
        if not Path(path).is_dir():
            raise SweepError(f"{label} is missing: {path}")
    profiles.load_frozen_profile(
        "g14-4thread-sweep", options.graph_manifest
    )
    manifest = profiles.load_graph_manifest(options.graph_manifest)
    if Path(manifest.graph).resolve() != Path(options.graph).resolve():
        raise SweepError("g14 graph path differs from frozen manifest")
    load_policy(options.policy)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXTERNAL_ROOT)
    parser.add_argument("--gem5", type=Path, default=REPO / "build/X86/gem5.opt")
    parser.add_argument(
        "--config", type=Path,
        default=REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py",
    )
    parser.add_argument(
        "--cxlmemuring", type=Path,
        default=Path("/home/victoryang00/CXLMemUring"),
    )
    parser.add_argument(
        "--m2ndp-root", type=Path,
        default=REPO / "m5out/m2ndp/source",
    )
    parser.add_argument(
        "--m5-library", type=Path,
        default=REPO / "util/m5/build/x86/out/libm5.a",
    )
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only-latency", choices=LATENCIES)
    parser.add_argument("--stop-after", choices=SYSTEMS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    g14_baseline = root / "qualification/g14-preformal/baseline-build"
    g12_baseline = root / "qualification/g12/baseline-build"
    baseline_build = (
        g14_baseline if (g14_baseline / "manifest.json").is_file()
        else g12_baseline
    )
    return Options(
        root=root,
        graph=root / "graphs/g14.sg",
        graph_manifest=root / "graphs/g14.manifest.json",
        policy=root / "policy/cira-lead.json",
        qualification=root / "qualification/qualification.json",
        gem5=args.gem5.resolve(),
        config=args.config.resolve(),
        cxlmemuring=args.cxlmemuring.resolve(),
        m2ndp_root=args.m2ndp_root.resolve(),
        m5_library=args.m5_library.resolve(),
        baseline_build=baseline_build,
        cxx=args.cxx,
        timeout=args.timeout,
        resume=args.resume,
        only_latency=args.only_latency,
        stop_after=args.stop_after,
    )


def main(argv=None):
    options = parse_args(argv)
    paths = make_paths(options)
    try:
        _validate_options(options)
        paths.runs.mkdir(parents=True, exist_ok=True)
        paths.logs.mkdir(parents=True, exist_ok=True)
        paths.variants.mkdir(parents=True, exist_ok=True)
        state = _load_or_create_state(options, paths)
        while True:
            entry = next_action(state, options)
            if entry is None:
                print(f"G14_SWEEP_COMPLETE status={paths.status}")
                return 0
            if entry.system == SYSTEMS[0]:
                require_external_root(options.root)
            print(
                "G14_SWEEP_ACTION_BEGIN "
                f"latency={entry.latency} system={entry.system}", flush=True,
            )
            _validate_or_run(entry, options, paths, state)
            print(
                "G14_SWEEP_ACTION_PASS "
                f"latency={entry.latency} system={entry.system}", flush=True,
            )
            if _reached_stop(entry, options):
                print(
                    "G14_SWEEP_STOP_BOUNDARY "
                    f"latency={entry.latency} system={entry.system} "
                    f"status={paths.status}", flush=True,
                )
                return 0
    except (
        SweepError, shared.SweepError, artifacts.EvidenceError,
        profiles.ProfileError, matched_runner.VariantRunError,
        variant_builder.VariantEvidenceError, OSError, KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"G14_SWEEP_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
