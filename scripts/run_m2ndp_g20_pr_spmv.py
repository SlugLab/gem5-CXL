#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run a bit-exact PageRank gem5-to-M2NDP experiment profile."""

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

try:
    from scripts import calibrate_m2ndp_cxl as calibration
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
    from scripts import m2ndp_pagerank_trace as pagerank_trace
    from scripts import m2ndp_results as results
except ImportError:
    import calibrate_m2ndp_cxl as calibration
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts
    import m2ndp_pagerank_trace as pagerank_trace
    import m2ndp_results as results


REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
ROI_CONFIG = (
    REPO / "configs/example/gem5_library/gapbs_roi_state.py"
)
PATCH = REPO / "util/m2ndp/patches/0001-funcsim-strict-sequence.patch"
STAGES = (
    "prepare_m2ndp",
    "build_gapbs",
    "graph_export",
    "gem5_baseline",
    "reference_pack",
    "trace_generate",
    "funcsim",
    "calibration",
    "ndpsim",
    "publish",
)
STAGE_LABELS = {
    "prepare_m2ndp": "M2NDP preparation",
    "build_gapbs": "GAPBS build",
    "graph_export": "graph export",
    "gem5_baseline": "gem5 baseline",
    "reference_pack": "reference pack",
    "trace_generate": "trace generation",
    "funcsim": "FuncSim",
    "calibration": "CXL calibration",
    "ndpsim": "NDPSim",
    "publish": "publication",
}


class StageCommandError(artifacts.EvidenceError):
    def __init__(self, stage, returncode, log):
        super().__init__(
            f"{STAGE_LABELS[stage]} exited {returncode}; see {log}"
        )
        self.returncode = returncode


@dataclasses.dataclass(frozen=True)
class Options:
    graph: Path
    graph_scale: int
    cxlmemuring: Path
    m2ndp_root: Path
    gem5: Path
    outdir: Path
    smoke_test: bool
    resume: bool
    timeout: int
    stop_after: str | None
    profile: str = "g20-2thread-1us"
    cxl_link_delay: str = "1us"


@dataclasses.dataclass(frozen=True)
class RunPaths:
    root: Path
    status: Path
    summary: Path
    manifest: Path
    logs: Path
    tools: Path
    prepare_state: Path
    build: Path
    reference_raw: Path
    csr: Path
    gem5_run: Path
    checkpoints: Path
    reference: Path
    trace: Path
    funcsim_dump: Path
    calibration: Path
    ndpsim_log: Path
    m5_library: Path


def make_paths(options):
    root = Path(options.outdir).resolve()
    gem5_root = Path(options.gem5).resolve().parents[2]
    return RunPaths(
        root=root,
        status=root / "status.json",
        summary=root / "summary.csv",
        manifest=root / "manifest.json",
        logs=root / "logs",
        tools=root / "tools",
        prepare_state=root / "prepare_m2ndp.json",
        build=root / "build",
        reference_raw=root / "reference/scores.raw",
        csr=root / "csr",
        gem5_run=root / "gem5/run",
        checkpoints=root / "gem5/checkpoints",
        reference=root / "reference/scores.m2pr",
        trace=root / "trace",
        funcsim_dump=root / "funcsim/scores.u32",
        calibration=root / "calibration",
        ndpsim_log=root / "logs/ndpsim.log",
        m5_library=gem5_root / "util/m5/build/x86/out/libm5.a",
    )


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stage_record():
    return {
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "command": [],
        "returncode": None,
        "inputs": {},
        "outputs": {},
        "log": None,
        "error": None,
    }


def _experiment_profile(options):
    profile = profiles.get_profile(options.profile)
    profiles.require_latency(profile, options.cxl_link_delay)
    return profile


def new_state(options):
    profile = _experiment_profile(options)
    return {
        "schema": 1,
        "contract": {
            "benchmark": "pr_spmv",
            "profile": profile.name,
            "graph": str(Path(options.graph).resolve()),
            "graph_scale": profile.graph_scale,
            "graph_sha256": profile.graph_sha256,
            "page_rank_iterations": profile.page_rank_iterations,
            "trials": profile.trials,
            "measured_trial": profile.measured_trial,
            "cpu": "timing",
            "cores": profile.cores,
            "threads": profile.threads,
            "all_memory_cxl": True,
            "cxl_link_delay": options.cxl_link_delay,
            "smoke_test": options.smoke_test,
        },
        "stages": {stage: _stage_record() for stage in STAGES},
    }


def hash_path(path):
    path = Path(path)
    if path.is_file():
        return artifacts.sha256_file(path)
    if not path.is_dir():
        raise artifacts.EvidenceError(f"hash input is missing: {path}")
    digest = hashlib.sha256()
    files = [item for item in sorted(path.rglob("*")) if item.is_file()]
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(bytes.fromhex(artifacts.sha256_file(item)))
    return digest.hexdigest()


def _path_key(path, outdir):
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(outdir).resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve_key(key, outdir):
    path = Path(key)
    return path if path.is_absolute() else Path(outdir) / path


def capture_hashes(paths, outdir):
    return {
        _path_key(path, outdir): hash_path(path)
        for path in paths
    }


def _hashes_match(mapping, outdir):
    for key, expected in mapping.items():
        path = _resolve_key(key, outdir)
        try:
            actual = hash_path(path)
        except (OSError, artifacts.EvidenceError):
            return False
        if actual != expected:
            return False
    return True


def should_run(stage, state, outdir):
    record = state["stages"][stage]
    if record["status"] != "passed":
        return True
    return not (
        _hashes_match(record.get("inputs", {}), outdir)
        and _hashes_match(record.get("outputs", {}), outdir)
    )


def _invalidate_from(state, stage):
    start = STAGES.index(stage)
    for name in STAGES[start:]:
        state["stages"][name] = _stage_record()


def invalidate_mismatched_stages(state, outdir):
    invalidate = None
    for stage in STAGES:
        record = state["stages"][stage]
        if invalidate is not None:
            state["stages"][stage] = _stage_record()
            continue
        if record["status"] == "passed":
            if should_run(stage, state, outdir):
                invalidate = stage
                state["stages"][stage] = _stage_record()
        elif record["status"] in {"failed", "running"}:
            invalidate = stage
            state["stages"][stage] = _stage_record()
        elif record["status"] == "pending":
            invalidate = stage
    return invalidate


def next_stage(state):
    for stage in STAGES:
        status = state["stages"][stage]["status"]
        if status == "failed":
            raise artifacts.EvidenceError(
                f"{STAGE_LABELS[stage]} failed; downstream stages are blocked"
            )
    for stage in STAGES:
        status = state["stages"][stage]["status"]
        if status != "passed":
            return stage
    return None


def gem5_command(options, paths):
    profile = _experiment_profile(options)
    command = [
        sys.executable,
        str(REPO / "scripts/compare_gapbs_cxl_amu_cira.py"),
        "--gem5",
        str(Path(options.gem5).resolve()),
        "--baseline-bin-dir",
        str(paths.build / "bin"),
        "--benchmarks",
        "pr_spmv",
        "--graph",
        str(Path(options.graph).resolve()),
        "--graph-scale",
        str(profile.graph_scale),
        "--profile",
        profile.name,
        "--iterations",
        str(profile.trials),
        "--measure-trial",
        str(profile.measured_trial),
        "--cpu",
        "timing",
        "--cores",
        str(profile.cores),
        "--checkpoint-root",
        str(paths.checkpoints),
        "--cxl-link-delay",
        options.cxl_link_delay,
        "--env",
        f"OMP_NUM_THREADS={profile.threads}",
        "--roi-work-events",
        "--verify",
        "--timeout",
        str(options.timeout),
        "--outdir",
        str(paths.gem5_run),
    ]
    if options.smoke_test:
        command.append("--smoke-test")
    return command


def _prepare_command(options, paths):
    return [
        sys.executable,
        str(REPO / "scripts/prepare_m2ndp.py"),
        "--m2ndp-root",
        str(Path(options.m2ndp_root).resolve()),
        "--tools-dir",
        str(paths.tools),
        "--state",
        str(paths.prepare_state),
        "--build",
    ]


def _build_command(options, paths):
    return [
        sys.executable,
        str(REPO / "scripts/build_gapbs_m2ndp_pr_spmv.py"),
        "--cxlmemuring",
        str(Path(options.cxlmemuring).resolve()),
        "--outdir",
        str(paths.build),
        "--reference-raw",
        str(paths.reference_raw),
        "--m5-library",
        str(paths.m5_library),
    ]


def _graph_command(options, paths):
    return [
        str(paths.build / "bin/export_gapbs_graph"),
        str(Path(options.graph).resolve()),
        str(paths.csr),
    ]


def _funcsim_command(paths, node_count):
    trace_dir = paths.trace / "0"
    return [
        str(paths.tools / "bin/FuncSim"),
        "--sequence_file",
        str(paths.trace / "funcsim.sequence"),
        "--memory_map",
        str(trace_dir / "K0_INIT_input.data"),
        "--target_map",
        str(trace_dir / "K3_PULL_DAMP_output.data"),
        "--config",
        str(paths.trace / "functional.config"),
        "--strict_float32_base",
        f"0x{pagerank_trace.SCORES_ADDR:x}",
        "--strict_float32_count",
        str(node_count),
        "--dump_float32_bits",
        str(paths.funcsim_dump),
    ]


def _calibration_command(options, paths):
    return [
        sys.executable,
        str(REPO / "scripts/calibrate_m2ndp_cxl.py"),
        "--gem5",
        str(Path(options.gem5).resolve()),
        "--m5-library",
        str(paths.m5_library),
        "--m2ndp-root",
        str(Path(options.m2ndp_root).resolve()),
        "--m2ndp-tools",
        str(paths.tools),
        "--outdir",
        str(paths.calibration),
        "--cxl-delay",
        options.cxl_link_delay,
    ]


def _ndpsim_command(paths):
    return [
        str(paths.tools / "bin/NDPSim"),
        "--trace",
        str(paths.trace / "0"),
        "--num_hosts",
        "1",
        "--num_m2ndps",
        "1",
        "--config",
        str(paths.calibration / "config/m2ndp.config"),
        "--synthetic_memory",
        "false",
        "--serial_launch",
        "true",
    ]


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise artifacts.EvidenceError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise artifacts.EvidenceError(f"{label} must be a JSON object")
    return value


def _graph_meta(paths):
    return artifacts.load_graph_meta(paths.csr / "graph.meta.json")


def command_for_stage(stage, options, paths):
    if stage == "prepare_m2ndp":
        return _prepare_command(options, paths)
    if stage == "build_gapbs":
        return _build_command(options, paths)
    if stage == "graph_export":
        return _graph_command(options, paths)
    if stage == "gem5_baseline":
        return gem5_command(options, paths)
    if stage == "funcsim":
        return _funcsim_command(paths, _graph_meta(paths).num_nodes)
    if stage == "calibration":
        return _calibration_command(options, paths)
    if stage == "ndpsim":
        return _ndpsim_command(paths)
    return ["internal", stage]


def stage_input_paths(stage, options, paths):
    graph = Path(options.graph).resolve()
    mapping = {
        "prepare_m2ndp": [
            REPO / "scripts/prepare_m2ndp.py",
            PATCH,
        ],
        "build_gapbs": [
            REPO / "scripts/build_gapbs_m2ndp_pr_spmv.py",
            REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc",
            REPO / "util/m2ndp/export_gapbs_graph.cc",
            paths.m5_library,
            Path(options.cxlmemuring).resolve() / "bench/gapbs/src",
        ],
        "graph_export": [
            graph,
            paths.build / "bin/export_gapbs_graph",
        ],
        "gem5_baseline": [
            graph,
            paths.build / "bin/pr_spmv",
            Path(options.gem5).resolve(),
            CONFIG,
            ROI_CONFIG,
        ],
        "reference_pack": [
            graph,
            paths.csr / "graph.meta.json",
            paths.reference_raw,
            paths.build / "manifest.json",
            paths.gem5_run / "summary.csv",
        ],
        "trace_generate": [
            paths.csr,
            paths.reference,
            REPO / "scripts/m2ndp_pagerank_trace.py",
        ],
        "funcsim": [
            paths.tools / "bin/FuncSim",
            paths.trace,
            paths.reference_raw,
        ],
        "calibration": [
            Path(options.gem5).resolve(),
            paths.m5_library,
            paths.tools / "bin/M2NDPCXLProbe",
            Path(options.m2ndp_root).resolve()
            / calibration.M2NDP_CONFIG_RELATIVE,
            REPO / "scripts/calibrate_m2ndp_cxl.py",
        ],
        "ndpsim": [
            paths.tools / "bin/NDPSim",
            paths.trace,
            paths.calibration / "config",
            paths.calibration / "calibration.json",
        ],
        "publish": [
            paths.gem5_run / "summary.csv",
            paths.reference_raw,
            paths.reference,
            paths.trace,
            paths.funcsim_dump,
            paths.logs / "funcsim.log",
            paths.calibration / "calibration.json",
            paths.calibration / "config",
            paths.ndpsim_log,
            paths.prepare_state,
        ],
    }
    return mapping[stage]


def stage_output_paths(stage, paths):
    mapping = {
        "prepare_m2ndp": [paths.prepare_state, paths.tools],
        "build_gapbs": [paths.build],
        "graph_export": [paths.csr],
        "gem5_baseline": [
            paths.gem5_run / "summary.csv",
            paths.reference_raw,
        ],
        "reference_pack": [paths.reference],
        "trace_generate": [paths.trace],
        "funcsim": [paths.funcsim_dump, paths.logs / "funcsim.log"],
        "calibration": [
            paths.calibration / "calibration.json",
            paths.calibration / "config",
            paths.calibration / "samples.csv",
        ],
        "ndpsim": [paths.ndpsim_log],
        "publish": [paths.summary, paths.manifest],
    }
    return mapping[stage]


def _run_command(command, *, cwd, log, timeout, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    print("+", shlex.join(str(item) for item in command), flush=True)
    try:
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(
                [str(item) for item in command],
                cwd=cwd,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if timeout == 0 else timeout,
                check=False,
            )
        return completed.returncode
    except subprocess.TimeoutExpired as error:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(f"\nTIMEOUT: {error}\n")
        return 124


def _m2ndp_runtime_environment(tools):
    runtime_library = Path(tools).resolve() / "lib/libNDPSim_lib.so"
    if not runtime_library.is_file():
        raise artifacts.EvidenceError(
            f"M2NDP runtime library is missing: {runtime_library}"
        )
    environment = os.environ.copy()
    library_dir = str(runtime_library.parent)
    inherited = environment.get("LD_LIBRARY_PATH")
    environment["LD_LIBRARY_PATH"] = (
        library_dir
        if not inherited
        else os.pathsep.join((library_dir, inherited))
    )
    return environment


def _pack_reference(options, paths):
    profile = _experiment_profile(options)
    meta = _graph_meta(paths)
    gem5 = results.parse_gem5_summary(
        paths.gem5_run / "summary.csv",
        profile=profile,
        latency=options.cxl_link_delay,
        smoke_test=options.smoke_test,
    )
    build = _load_json(paths.build / "manifest.json", "build manifest")
    raw_size = paths.reference_raw.stat().st_size
    expected_size = meta.num_nodes * 4
    if raw_size != expected_size:
        raise artifacts.EvidenceError(
            f"gem5 raw reference size is {raw_size}, expected {expected_size}"
        )
    with paths.reference_raw.open("rb") as stream:
        os.fsync(stream.fileno())
    words = artifacts.BinaryArray(paths.reference_raw, "<I", meta.num_nodes)
    try:
        artifacts.write_reference(
            paths.reference,
            {
                "schema": 1,
                "graph_sha256": meta.graph_sha256,
                "num_nodes": meta.num_nodes,
                "iterations": profile.page_rank_iterations,
                "measured_trial": profile.measured_trial,
                "binary_sha256": build["binary_sha256"]["pr_spmv"],
                "source_sha256": build["matched_source_sha256"],
            },
            words,
        )
    finally:
        words.close()
    if gem5.sim_ticks <= 0:
        raise artifacts.EvidenceError("gem5 baseline has no positive ROI")


def _generate_trace(options, paths):
    profile = _experiment_profile(options)
    bundle = artifacts.load_graph_bundle(paths.csr)
    try:
        pagerank_trace.generate_trace(
            bundle=bundle,
            reference=artifacts.read_reference(paths.reference),
            outdir=paths.trace,
            trials=profile.trials,
            iterations=profile.page_rank_iterations,
        )
    finally:
        bundle.in_offsets.close()
        bundle.in_neighbors.close()
        bundle.out_degree.close()


def _parse_calibration(path):
    value = _load_json(path, "calibration artifact")
    required = (
        "passed",
        "request_bytes",
        "target_ns",
        "measured_ns",
        "residual_ns",
        "link_period_ns",
        "config_sha256",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise artifacts.EvidenceError(
            "calibration artifact missing " + ", ".join(missing)
        )
    return results.CalibrationEvidence(
        passed=value["passed"] is True,
        request_bytes=int(value["request_bytes"]),
        target_ns=Decimal(value["target_ns"]),
        measured_ns=Decimal(value["measured_ns"]),
        residual_ns=Decimal(value["residual_ns"]),
        link_period_ns=Decimal(value["link_period_ns"]),
        config_sha256=value["config_sha256"],
    )


def _validate_calibration_config(paths, evidence):
    actual = calibration.sha256_config_tree(
        paths.calibration / "config"
    )
    if actual != evidence.config_sha256:
        raise artifacts.EvidenceError(
            "calibrated M2NDP configuration hash mismatch"
        )


def _git_head(root):
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise artifacts.EvidenceError(
            f"cannot resolve repository commit for {root}: {error}"
        ) from error


def _publish(options, paths):
    profile = _experiment_profile(options)
    meta = _graph_meta(paths)
    gem5 = results.parse_gem5_summary(
        paths.gem5_run / "summary.csv",
        profile=profile,
        latency=options.cxl_link_delay,
        smoke_test=options.smoke_test,
    )
    funcsim_log = (paths.logs / "funcsim.log").read_text(
        encoding="utf-8", errors="replace"
    )
    funcsim = results.parse_funcsim(
        funcsim_log,
        returncode=0,
        expected_count=meta.num_nodes,
        dump_path=paths.funcsim_dump,
        reference_path=paths.reference_raw,
    )
    ndpsim_log = paths.ndpsim_log.read_text(
        encoding="utf-8", errors="replace"
    )
    ndpsim = results.parse_ndpsim(ndpsim_log, returncode=0)
    calibrated = _parse_calibration(
        paths.calibration / "calibration.json"
    )
    _validate_calibration_config(paths, calibrated)
    prepare = _load_json(paths.prepare_state, "M2NDP prepare state")
    provenance = results.ProvenanceEvidence(
        graph_sha256=meta.graph_sha256,
        gem5_binary_sha256=artifacts.sha256_file(options.gem5),
        trace_sha256=hash_path(paths.trace),
        m2ndp_patch_sha256=prepare["patch_sha256"],
        m2ndp_config_sha256=calibrated.config_sha256,
        reference_raw_sha256=artifacts.sha256_file(paths.reference_raw),
        funcsim_dump_sha256=artifacts.sha256_file(paths.funcsim_dump),
    )
    row = results.build_summary(
        gem5=gem5,
        funcsim=funcsim,
        ndpsim=ndpsim,
        calibration=calibrated,
        provenance=provenance,
        profile=profile,
        latency=options.cxl_link_delay,
        smoke_test=options.smoke_test,
    )
    artifacts.atomic_write_csv(
        paths.summary,
        tuple(row),
        [row],
    )
    build = _load_json(paths.build / "manifest.json", "build manifest")
    manifest_paths = {
        "gem5_binary": Path(options.gem5).resolve(),
        "matched_source": REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc",
        "copied_gapbs_source": paths.build / "src/gapbs",
        "graph": Path(options.graph).resolve(),
        "csr": paths.csr,
        "m2ndp_patch": PATCH,
        "m2ndp_tools": paths.tools,
        "trace": paths.trace,
        "m2ndp_config": paths.calibration / "config",
        "reference": paths.reference,
        "reference_raw": paths.reference_raw,
        "funcsim_dump": paths.funcsim_dump,
        "calibration": paths.calibration / "calibration.json",
        "gem5_log": paths.gem5_run
        / "pr_spmv/cxl_vanilla/gem5.log",
        "funcsim_log": paths.logs / "funcsim.log",
        "ndpsim_log": paths.ndpsim_log,
        "summary": paths.summary,
    }
    manifest = {
        "schema": 1,
        "contract": new_state(options)["contract"],
        "gem5_repository_commit": _git_head(REPO),
        "m2ndp_upstream_commit": prepare["upstream_commit"],
        "build_binary_sha256": build["binary_sha256"],
        "artifact_sha256": {
            name: hash_path(path)
            for name, path in manifest_paths.items()
        },
    }
    artifacts.atomic_write_json(paths.manifest, manifest)


def _execute_stage(stage, options, paths, command, log):
    if stage in {
        "prepare_m2ndp",
        "build_gapbs",
        "graph_export",
        "gem5_baseline",
        "funcsim",
        "calibration",
        "ndpsim",
    }:
        if stage == "funcsim":
            paths.funcsim_dump.parent.mkdir(parents=True, exist_ok=True)
        cwd = (
            Path(options.m2ndp_root).resolve()
            if stage == "ndpsim"
            else REPO
        )
        environment = (
            _m2ndp_runtime_environment(paths.tools)
            if stage in {"calibration", "ndpsim"}
            else None
        )
        returncode = _run_command(
            command,
            cwd=cwd,
            log=log,
            timeout=options.timeout,
            env=environment,
        )
        if returncode != 0:
            raise StageCommandError(stage, returncode, log)
        if stage == "graph_export":
            artifacts.finalize_graph_meta(
                paths.csr,
                options.graph,
                Path(log).read_text(encoding="utf-8"),
            )
            bundle = artifacts.load_graph_bundle(paths.csr)
            if options.smoke_test:
                artifacts.validate_publication_graph(bundle.meta, True)
            else:
                artifacts.validate_profile_graph(
                    bundle.meta, _experiment_profile(options)
                )
            bundle.in_offsets.close()
            bundle.in_neighbors.close()
            bundle.out_degree.close()
        elif stage == "gem5_baseline":
            results.parse_gem5_summary(
                paths.gem5_run / "summary.csv",
                profile=_experiment_profile(options),
                latency=options.cxl_link_delay,
                smoke_test=options.smoke_test,
            )
        elif stage == "funcsim":
            meta = _graph_meta(paths)
            results.parse_funcsim(
                Path(log).read_text(encoding="utf-8", errors="replace"),
                returncode=returncode,
                expected_count=meta.num_nodes,
                dump_path=paths.funcsim_dump,
                reference_path=paths.reference_raw,
            )
        elif stage == "calibration":
            calibrated = _parse_calibration(
                paths.calibration / "calibration.json"
            )
            if not calibrated.passed:
                raise artifacts.EvidenceError("CXL calibration failed")
            _validate_calibration_config(paths, calibrated)
        elif stage == "ndpsim":
            results.parse_ndpsim(
                Path(log).read_text(encoding="utf-8", errors="replace"),
                returncode=returncode,
            )
        return 0
    if stage == "reference_pack":
        _pack_reference(options, paths)
    elif stage == "trace_generate":
        _generate_trace(options, paths)
    elif stage == "publish":
        _publish(options, paths)
    else:
        raise artifacts.EvidenceError(f"unknown stage {stage}")
    Path(log).write_text(
        f"{STAGE_LABELS[stage]} completed internally\n",
        encoding="utf-8",
    )
    return 0


def _clean_retry_outputs(stage, paths):
    if stage in {
        "build_gapbs",
        "graph_export",
        "trace_generate",
        "calibration",
    }:
        target = {
            "build_gapbs": paths.build,
            "graph_export": paths.csr,
            "trace_generate": paths.trace,
            "calibration": paths.calibration,
        }[stage]
        if target.is_dir():
            shutil.rmtree(target)
    if stage == "publish":
        paths.summary.unlink(missing_ok=True)
        paths.manifest.unlink(missing_ok=True)


def run_stage(stage, state, options, paths):
    command = command_for_stage(stage, options, paths)
    log = paths.logs / f"{stage}.log"
    inputs = capture_hashes(
        stage_input_paths(stage, options, paths),
        paths.root,
    )
    record = state["stages"][stage]
    record.update(
        {
            "status": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "command": [str(item) for item in command],
            "returncode": None,
            "inputs": inputs,
            "outputs": {},
            "log": _path_key(log, paths.root),
            "error": None,
        }
    )
    artifacts.atomic_write_json(paths.status, state)
    try:
        _clean_retry_outputs(stage, paths)
        log.parent.mkdir(parents=True, exist_ok=True)
        returncode = _execute_stage(
            stage, options, paths, command, log
        )
        outputs = capture_hashes(
            stage_output_paths(stage, paths),
            paths.root,
        )
        record.update(
            {
                "status": "passed",
                "finished_at": _utc_now(),
                "returncode": returncode,
                "outputs": outputs,
            }
        )
        artifacts.atomic_write_json(paths.status, state)
    except BaseException as error:
        paths.summary.unlink(missing_ok=True)
        record.update(
            {
                "status": "failed",
                "finished_at": _utc_now(),
                "returncode": getattr(error, "returncode", 1),
                "error": str(error),
            }
        )
        artifacts.atomic_write_json(paths.status, state)
        raise


def _migrate_legacy_g20_contract(state, expected_contract):
    if expected_contract.get("profile") != "g20-2thread-1us":
        return False
    legacy_contract = dict(expected_contract)
    for field in ("profile", "graph_sha256", "threads"):
        legacy_contract.pop(field)
    if state.get("contract") != legacy_contract:
        return False
    state["contract"] = expected_contract
    return True


def _load_or_create_state(options, paths):
    if paths.status.exists():
        if not options.resume:
            raise artifacts.EvidenceError(
                f"run state exists; use --resume: {paths.status}"
            )
        state = _load_json(paths.status, "run state")
        expected_contract = new_state(options)["contract"]
        contract_matches = state.get("contract") == expected_contract
        if not contract_matches:
            contract_matches = _migrate_legacy_g20_contract(
                state, expected_contract
            )
        if state.get("schema") != 1 or not contract_matches:
            raise artifacts.EvidenceError(
                "resume contract differs from the recorded run"
            )
        invalidate_mismatched_stages(state, paths.root)
        artifacts.atomic_write_json(paths.status, state)
        return state
    if options.resume:
        raise artifacts.EvidenceError(
            f"--resume requested but state is missing: {paths.status}"
        )
    state = new_state(options)
    artifacts.atomic_write_json(paths.status, state)
    return state


def validate_options(options, paths):
    profile = _experiment_profile(options)
    if options.timeout < 0:
        raise artifacts.EvidenceError("--timeout must be nonnegative")
    if options.stop_after is not None and options.stop_after not in STAGES:
        raise artifacts.EvidenceError(
            f"unknown --stop-after stage: {options.stop_after}"
        )
    if options.graph_scale != profile.graph_scale and not options.smoke_test:
        raise artifacts.EvidenceError(
            "graph scale does not match experiment profile"
        )
    required_files = (
        ("graph", options.graph),
        ("gem5", options.gem5),
        ("m5 library", paths.m5_library),
    )
    for label, path in required_files:
        if not Path(path).is_file():
            raise artifacts.EvidenceError(f"{label} is missing: {path}")
    for label, path in (
        ("CXLMemUring", options.cxlmemuring),
        ("M2NDP", options.m2ndp_root),
    ):
        if not Path(path).is_dir():
            raise artifacts.EvidenceError(f"{label} is missing: {path}")
    if not options.smoke_test:
        try:
            profiles.validate_graph(profile, options.graph)
        except profiles.ProfileError as error:
            raise artifacts.EvidenceError(str(error)) from error


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--graph-scale", type=int, default=20)
    parser.add_argument(
        "--profile",
        choices=tuple(profiles.PROFILES),
        default="g20-2thread-1us",
    )
    parser.add_argument("--cxl-link-delay", default="1us")
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--stop-after", choices=STAGES)
    args = parser.parse_args(argv)
    return Options(
        graph=args.graph.resolve(),
        graph_scale=args.graph_scale,
        cxlmemuring=args.cxlmemuring.resolve(),
        m2ndp_root=args.m2ndp_root.resolve(),
        gem5=args.gem5.resolve(),
        outdir=args.outdir.resolve(),
        smoke_test=args.smoke_test,
        resume=args.resume,
        timeout=args.timeout,
        stop_after=args.stop_after,
        profile=args.profile,
        cxl_link_delay=args.cxl_link_delay,
    )


def main(argv=None):
    options = parse_args(argv)
    paths = make_paths(options)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    try:
        validate_options(options, paths)
        state = _load_or_create_state(options, paths)
        while True:
            stage = next_stage(state)
            if stage is None:
                print(f"M2NDP_RUN_COMPLETE summary={paths.summary}")
                return 0
            print(f"M2NDP_STAGE_BEGIN stage={stage}", flush=True)
            run_stage(stage, state, options, paths)
            print(f"M2NDP_STAGE_PASS stage={stage}", flush=True)
            if options.stop_after == stage:
                print(f"M2NDP_RUN_STOPPED stage={stage}", flush=True)
                return 0
    except (
        artifacts.EvidenceError,
        profiles.ProfileError,
        OSError,
        KeyError,
    ) as error:
        paths.summary.unlink(missing_ok=True)
        print(f"M2NDP_RUN_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
