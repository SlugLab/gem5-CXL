#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the fail-closed G12 PageRank/GAP-BC M2NDP latency sweep.

This module deliberately separates input/evidence validation from execution.
PageRank is prepared by the native four-stage kernel pipeline; GAP BC uses the
bounded lazy-trace lowerer.  A cell is publishable only after functional,
memory, launch-count, and link-calibration gates all pass.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

try:
    from scripts import cross_system_contract as contract_io
    from scripts import lazy_work_trace as lazy
    from scripts import m2ndp_workload_trace as m2ndp
except ImportError:
    import cross_system_contract as contract_io
    import lazy_work_trace as lazy
    import m2ndp_workload_trace as m2ndp


G12_GRAPH_SHA256 = (
    "759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1"
)
G12_NODES = 4096
G12_DIRECTED_EDGES = 96772
WORKLOADS = ("pr_spmv", "gap_bc")
LATENCIES = ("200ns", "500ns", "1us", "2us")

REPO = Path(__file__).resolve().parents[1]
INPUT_ROOT = Path(
    "/mnt/disk0/gem5-CXL-eval/g12-timing-24cell-inputs-20260906"
)
DEFAULT_INPUTS = INPUT_ROOT / "inputs.json"
DEFAULT_REGISTRY = INPUT_ROOT / "registry.json"
DEFAULT_OUTPUT = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/"
    "shared/g12-graph-m2ndp-r1"
)
NPB_TEMPLATE_ROOT = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/"
    "shared/npb-cg-indexed-sparse-r1"
)
CALIBRATION_ROOT = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/"
    "formal-window-gate-r15/calibration"
)
PR_NATIVE_ROOT = Path(
    "/mnt/disk0/gem5-CXL-eval/"
    "pr-offload-formal-a1e45e2d79-r13/qualification/primary/m2ndp"
)
PR_WORKTREE = Path(
    "/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv"
)
SERVICE_SCRIPT = REPO / "scripts/run_g12_m2ndp_graph_spectrum_service.sh"
SERVICE_UNIT = REPO / "util/systemd/m2ndp-g12-graph-spectrum.service"


class SpectrumError(RuntimeError):
    """A frozen input, simulator package, or evidence record is invalid."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SpectrumError(f"invalid JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise SpectrumError(f"JSON object required: {path}")
    return value


def _require_graph(value, label):
    expected = {
        "scale": 12,
        "num_nodes": G12_NODES,
        "directed_edges": G12_DIRECTED_EDGES,
        "sha256": G12_GRAPH_SHA256,
    }
    if not isinstance(value, dict) or any(
        value.get(field) != wanted for field, wanted in expected.items()
    ):
        raise SpectrumError(f"G12 graph identity differs in {label}")


def load_contract(inputs_path=DEFAULT_INPUTS, registry_path=DEFAULT_REGISTRY):
    """Validate and return only the eight frozen G12 graph coordinates."""
    inputs = load_json(inputs_path)
    registry = load_json(registry_path)
    if inputs.get("schema") != 1 or inputs.get("status") != "accepted":
        raise SpectrumError("G12 input manifest is not accepted")
    if registry.get("schema") != 1 or registry.get("status") not in {
        "verified", "accepted"
    }:
        raise SpectrumError("G12 registry is not verified")
    _require_graph(inputs.get("graph"), "inputs")
    _require_graph(registry.get("graph"), "registry")

    workloads = inputs.get("workloads")
    registry_cells = registry.get("cells")
    if not isinstance(workloads, dict) or not isinstance(registry_cells, dict):
        raise SpectrumError("G12 workload registry is malformed")

    selected = {}
    for workload in WORKLOADS:
        source = workloads.get(workload)
        _require_graph(source, f"inputs workload {workload}")
        input_sha256 = source.get("input_sha256")
        if not isinstance(input_sha256, str) or len(input_sha256) != 64:
            raise SpectrumError(f"G12 {workload} input identity differs")
        for latency in LATENCIES:
            key = f"{workload}:{latency}"
            cell = registry_cells.get(key)
            if not isinstance(cell, dict):
                raise SpectrumError(f"G12 registry cell is missing: {key}")
            if (
                cell.get("workload") != workload
                or cell.get("latency") != latency
                or cell.get("graph_sha256") != G12_GRAPH_SHA256
                or cell.get("input_sha256") != input_sha256
            ):
                raise SpectrumError(f"G12 registry cell identity differs: {key}")
            selected[key] = cell
    return {
        "schema": 1,
        "status": "accepted",
        "graph_scale": 12,
        "graph_nodes": G12_NODES,
        "graph_directed_edges": G12_DIRECTED_EDGES,
        "graph_sha256": G12_GRAPH_SHA256,
        "inputs_path": str(Path(inputs_path).resolve()),
        "registry_path": str(Path(registry_path).resolve()),
        "cells": selected,
    }


def validate_evidence(row, workload, latency):
    """Apply all publication gates to a single NDPSim evidence record."""
    if workload not in WORKLOADS or latency not in LATENCIES:
        raise SpectrumError("unknown G12 graph coordinate")
    if not isinstance(row, dict) or row.get("status") != "pass":
        raise SpectrumError("NDPSim status did not pass")
    if row.get("verification") != "pass" or row.get("bit_exact") is not True:
        raise SpectrumError("FuncSim bit-exact gate did not pass")
    if row.get("memory_match") != "pass":
        raise SpectrumError("NDPSim memory match did not pass")
    if row.get("cxl_link_delay") != latency:
        raise SpectrumError("NDPSim latency identity differs")
    calibration = row.get("calibration")
    if (
        not isinstance(calibration, dict)
        or calibration.get("passed") is not True
        or calibration.get("cxl_delay") != latency
        or calibration.get("cxl_link_delay", latency) != latency
    ):
        raise SpectrumError("NDPSim calibration gate did not pass")
    expected = row.get("expected_launches")
    completed = row.get("completed_launches")
    if (
        not isinstance(expected, int)
        or isinstance(expected, bool)
        or expected <= 0
        or completed != expected
    ):
        raise SpectrumError("NDPSim launch count differs")
    cycles = row.get("cycles")
    if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
        raise SpectrumError("NDPSim cycle count is invalid")
    return row


def read_cell(root, workload, latency):
    path = Path(root) / workload / latency / "ndpsim-evidence.json"
    return validate_evidence(load_json(path), workload, latency)


def parse_service_unit(path=SERVICE_UNIT):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with Path(path).open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as error:
        raise SpectrumError(f"invalid service unit {path}: {error}") from error
    return {section: dict(parser[section]) for section in parser.sections()}


def _template_root(latency):
    suffix = "" if latency == "1us" else f"-{latency}"
    return NPB_TEMPLATE_ROOT / f"m2ndp-package-r1{suffix}"


def load_calibration(latency):
    if latency == "1us":
        value = load_json(
            _template_root(latency) / "ndpsim-evidence-1us.json"
        ).get("calibration")
    else:
        value = load_json(CALIBRATION_ROOT / latency / "calibration.json")
    if (
        not isinstance(value, dict)
        or value.get("passed") is not True
        or value.get("cxl_delay") != latency
        or value.get("cxl_link_delay", latency) != latency
    ):
        raise SpectrumError(f"M2NDP {latency} calibration differs")
    return value


def _provenance(trace_root, input_sha256, latency):
    template = _template_root(latency)
    manifest = load_json(template / "package.json")
    record = manifest.get("provenance", {})
    config = template / manifest["timing_config"]["path"]
    return m2ndp.PackageProvenance(
        trace_sha256=sha256_file(Path(trace_root) / "trace.v2.json"),
        input_sha256=input_sha256,
        funcsim_path=record["funcsim_path"],
        ndpsim_path=record["ndpsim_path"],
        patch_paths=tuple(record["patch_paths"]),
        config_path=record["config_path"],
        ndpsim_config_path=str(config),
    )


def _run_lazy_cell(source, output, workload, latency, input_sha256):
    root = output / workload / latency
    manifest_path = root / "package.json"
    if not manifest_path.is_file():
        if root.exists():
            raise SpectrumError(f"partial M2NDP cell exists: {root}")
        manifest_path = m2ndp.lower_bundle(
            source, root,
            provenance=_provenance(source, input_sha256, latency),
        )
    functional_path = root / "funcsim-evidence.json"
    if functional_path.is_file():
        functional = load_json(functional_path)
    else:
        functional = m2ndp.run_funcsim_package(
            manifest_path, evidence_path=functional_path
        )
    if functional.get("status") != "pass" or functional.get("bit_exact") is not True:
        raise SpectrumError("FuncSim gate did not pass")
    evidence_path = root / "ndpsim-evidence.json"
    if evidence_path.is_file():
        evidence = load_json(evidence_path)
    else:
        evidence = m2ndp.run_ndpsim_package(
            manifest_path,
            functional_evidence=functional,
            calibration=load_calibration(latency),
            evidence_path=evidence_path,
            cxl_link_delay=latency,
        )
    return validate_evidence(evidence, workload, latency)


def _native_pr_resume_command():
    runner = PR_WORKTREE / "scripts/run_m2ndp_g20_pr_spmv.py"
    return [
        sys.executable, str(runner),
        "--graph", "/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg",
        "--graph-scale", "12",
        "--profile", "pr-scaling-4thread-1us",
        "--graph-manifest", "/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json",
        "--cxlmemuring", "/home/victoryang00/CXLMemUring",
        "--m2ndp-root", "/mnt/disk0/M2NDP-public",
        "--gem5", str(PR_WORKTREE / "build/X86/gem5.opt"),
        "--m5-library", str(PR_WORKTREE / "util/m5/build/x86/out/libm5.a"),
        "--outdir", str(PR_NATIVE_ROOT),
        "--cxl-link-delay", "1us",
        "--timeout", "0",
        "--resume",
    ]


def ensure_native_pr_source(*, execute):
    status_path = PR_NATIVE_ROOT / "status.json"
    status = load_json(status_path)
    source_contract = status.get("contract", {})
    if (
        source_contract.get("graph_scale") != 12
        or source_contract.get("graph_sha256") != G12_GRAPH_SHA256
        or source_contract.get("page_rank_iterations") != 20
        or source_contract.get("cores") != 4
        or source_contract.get("all_memory_cxl") is not True
    ):
        raise SpectrumError("native PageRank G12 contract differs")
    required = ("reference_pack", "trace_generate", "funcsim", "publish")
    pending = [
        stage for stage in required
        if status.get("stages", {}).get(stage, {}).get("status") != "passed"
    ]
    if pending and execute:
        command = _native_pr_resume_command()
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise SpectrumError(
                f"native PageRank preparation exited {completed.returncode}"
            )
        status = load_json(status_path)
        pending = [
            stage for stage in required
            if status.get("stages", {}).get(stage, {}).get("status") != "passed"
        ]
    if pending:
        raise SpectrumError(
            "native PageRank source is pending: " + ",".join(pending)
        )
    return status


def _prepare_native_pr_trial(output):
    """Package only trial 1 of the accepted native PageRank trace."""
    status = ensure_native_pr_source(execute=False)
    metadata = load_json(PR_NATIVE_ROOT / "trace/trace.meta.json")
    if (
        metadata.get("graph_sha256") != G12_GRAPH_SHA256
        or metadata.get("num_nodes") != G12_NODES
        or metadata.get("num_directed_edges") != G12_DIRECTED_EDGES
        or metadata.get("iterations") != 20
        or metadata.get("trials") != 2
        or metadata.get("double_buffered") is not True
    ):
        raise SpectrumError("native PageRank trace contract differs")
    source_hash = status["stages"]["funcsim"].get("inputs", {}).get("trace")
    expected_output = status["stages"]["funcsim"].get("outputs", {}).get(
        "funcsim/scores.u32"
    )
    if (
        not isinstance(source_hash, str)
        or sha256_file(PR_NATIVE_ROOT / "funcsim/scores.u32") != expected_output
    ):
        raise SpectrumError("native PageRank FuncSim provenance differs")

    source_trace = PR_NATIVE_ROOT / "trace/0"
    timing_names = (source_trace / "kernelslist.g").read_text(
        encoding="utf-8"
    ).splitlines()
    measured_names = [name for name in timing_names if "TRIAL1" in name]
    completed_launches = metadata.get("funcsim_launches")
    if (
        len(timing_names) != metadata.get("ndpsim_launches")
        or len(measured_names) * 2 != len(timing_names)
        or not isinstance(completed_launches, int)
        or completed_launches <= len(measured_names)
        or not measured_names
        or not measured_names[0].startswith("K0_INIT_TRIAL1")
        or "ITER19" not in measured_names[-1]
    ):
        raise SpectrumError("native PageRank measured-trial sequence differs")

    shared = Path(output) / "pr_spmv/_shared"
    manifest_path = shared / "package.json"
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        if (
            manifest.get("graph_sha256") != G12_GRAPH_SHA256
            or manifest.get("source_trace_sha256") != source_hash
            or manifest.get("dynamic_launches") != completed_launches
        ):
            raise SpectrumError("existing PageRank measured package differs")
        return shared / "trace", manifest
    if shared.exists():
        raise SpectrumError(f"partial PageRank measured package exists: {shared}")

    trace = shared / "trace"
    trace.mkdir(parents=True)
    for name in sorted(set(measured_names)):
        for suffix in (".traceg", "_launch.txt"):
            source = source_trace / f"{name}{suffix}"
            if not source.is_file():
                raise SpectrumError(f"native PageRank trace payload missing: {source}")
            os.symlink(source, trace / source.name)
    first_input = trace / f"{measured_names[0]}_input.data"
    shutil.copyfile(source_trace / "K0_INIT_input.data", first_input)
    final_output = source_trace / f"{measured_names[-1]}_output.data"
    if not final_output.is_file():
        raise SpectrumError("native PageRank measured output is missing")
    os.symlink(final_output, trace / final_output.name)
    kernel_list = trace / "kernelslist.g"
    kernel_list.write_text("\n".join(measured_names) + "\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "status": "prepared",
        "workload": "pr_spmv",
        "scope": "G12 trial 1; synchronous double-buffered 20-iteration PageRank",
        "graph_sha256": G12_GRAPH_SHA256,
        "source": str(PR_NATIVE_ROOT),
        "source_trace_sha256": source_hash,
        "funcsim_bit_exact": True,
        "funcsim_output_sha256": expected_output,
        "timing_groups": len(measured_names),
        "dynamic_launches": completed_launches,
        "timing_kernel_list_sha256": sha256_file(kernel_list),
        "timing_input_sha256": sha256_file(first_input),
        "timing_output_sha256": sha256_file(final_output),
    }
    contract_io.atomic_write_json(manifest_path, manifest)
    return trace, manifest


def _parse_native_pr_log(path, expected_launches):
    start = finish = None
    period = None
    launches = memory_matches = 0
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if start is None and "K0_INIT_TRIAL1" in line:
                match = re.search(r"\b(?:at\s+)?cycle\s+(\d+)\b", line)
                if match:
                    start = int(match.group(1))
            match = re.search(r"\bEXPR\s+FINISHED\s+(\d+)\b", line)
            if match:
                if finish is not None:
                    raise SpectrumError("duplicate PageRank finish marker")
                finish = int(match.group(1))
            if "Gantt info:" in line and "finished NDP kernel" in line:
                launches += 1
            if "MEMROY MATCH SUCCESS" in line:
                memory_matches += 1
            if period is None:
                match = re.search(r"\bCORE\s+period:\s*([0-9.eE+-]+)", line)
                if match:
                    period = Decimal(match.group(1))
    if (
        start is None or finish is None or finish <= start
        or period is None or period <= 0
        or launches != expected_launches or memory_matches != 1
    ):
        raise SpectrumError(
            "PageRank NDPSim completion gate failed: "
            f"start={start} finish={finish} launches={launches}/"
            f"{expected_launches} memory_matches={memory_matches}"
        )
    return start, finish, finish - start, period


def _run_native_pr_cell(output, latency):
    output = Path(output)
    trace, package = _prepare_native_pr_trial(output)
    root = output / "pr_spmv" / latency
    evidence_path = root / "ndpsim-evidence.json"
    if evidence_path.is_file():
        return validate_evidence(load_json(evidence_path), "pr_spmv", latency)
    if root.exists():
        raise SpectrumError(f"partial M2NDP cell exists: {root}")

    calibration = load_calibration(latency)
    template = _template_root(latency)
    template_manifest = load_json(template / "package.json")
    source_config = template / template_manifest["timing_config"]["path"]
    runtime = root / "runtime"
    shutil.copytree(source_config.parent, runtime)
    config = runtime / source_config.name
    ndpsim = PR_NATIVE_ROOT / "tools/bin/NDPSim"
    stdout_path = root / "ndpsim.stdout.log"
    stderr_path = root / "ndpsim.stderr.log"
    command = [
        str(ndpsim), "--trace", str(trace),
        "--num_hosts", "1", "--num_m2ndps", "1",
        "--config", str(config), "--output", "ndpsim.out",
        "--synthetic_memory", "false", "--serial_launch", "true",
    ]
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            command, cwd=runtime, stdout=stdout, stderr=stderr, check=False
        )
    if completed.returncode != 0:
        raise SpectrumError(
            f"PageRank {latency} NDPSim exited {completed.returncode}"
        )
    start, finish, cycles, period = _parse_native_pr_log(
        stdout_path, package["dynamic_launches"]
    )
    observed_period_ns = period * Decimal(10**9)
    if abs(observed_period_ns - Decimal(calibration["core_period_ns"])) > Decimal("1e-15"):
        raise SpectrumError("PageRank NDPSim core period differs from calibration")
    evidence = {
        "schema": 1,
        "status": "pass",
        "verification": "pass",
        "bit_exact": True,
        "memory_match": "pass",
        "expected_launches": package["dynamic_launches"],
        "completed_launches": package["dynamic_launches"],
        "cycles": cycles,
        "start_cycle": start,
        "end_cycle": finish,
        "core_period_ns": str(observed_period_ns),
        "cxl_link_delay": latency,
        "calibration": calibration,
        "scope": package["scope"],
        "graph_sha256": G12_GRAPH_SHA256,
        "package_sha256": sha256_file(
            output / "pr_spmv/_shared/package.json"
        ),
        "ndpsim_sha256": sha256_file(ndpsim),
        "config_sha256": sha256_file(config),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        "command": command,
    }
    contract_io.atomic_write_json(evidence_path, evidence)
    return validate_evidence(evidence, "pr_spmv", latency)


def run_cell(workload, latency, *, inputs, registry, output):
    frozen = load_contract(inputs, registry)
    source = frozen["cells"][f"{workload}:{latency}"]
    trace = Path(source["trace"]["path"]).resolve().parent
    if sha256_file(trace / "trace.v2.json") != source["trace"]["sha256"]:
        raise SpectrumError(f"G12 {workload} trace SHA-256 differs")
    if workload == "pr_spmv":
        return _run_native_pr_cell(Path(output).resolve(), latency)
    return _run_lazy_cell(
        trace, Path(output).resolve(), workload, latency,
        source["input_sha256"],
    )


def parse_options(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--workload", choices=WORKLOADS)
    parser.add_argument("--latency", choices=LATENCIES)
    return parser.parse_args(arguments)


def main(arguments=None):
    options = parse_options(arguments)
    frozen = load_contract(options.inputs, options.registry)
    for latency in LATENCIES:
        load_calibration(latency)
    if options.check:
        print(
            "G12_GRAPH_SPECTRUM_INPUTS_PASS "
            f"cells={len(frozen['cells'])} graph_sha256={G12_GRAPH_SHA256}"
        )
        return 0
    if options.prepare:
        ensure_native_pr_source(execute=True)
        print("G12_GRAPH_SPECTRUM_PREPARED")
        return 0
    if options.workload is None or options.latency is None:
        raise SpectrumError("--workload and --latency are required")
    evidence = run_cell(
        options.workload, options.latency,
        inputs=options.inputs, registry=options.registry, output=options.output,
    )
    print(
        "G12_GRAPH_SPECTRUM_CELL_PASS "
        f"workload={options.workload} latency={options.latency} "
        f"cycles={evidence['cycles']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SpectrumError, lazy.LazyTraceError, m2ndp.TraceTranslationError) as error:
        raise SystemExit(f"G12_GRAPH_SPECTRUM_FAILED error={error}") from error
