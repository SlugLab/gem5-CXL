#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Prepare the hash-bound G14 six-workload timing registry."""

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import gap_bc_lazy_trace as gap_bc
    from scripts import lazy_work_trace as lazy
    from scripts import pr_spmv_lazy_trace as pr_spmv
    from scripts import stratified_timing as timing
    from scripts import timing_evidence_24cell as evidence
except ImportError:
    import canonical_work_trace as canonical
    import gap_bc_lazy_trace as gap_bc
    import lazy_work_trace as lazy
    import pr_spmv_lazy_trace as pr_spmv
    import stratified_timing as timing
    import timing_evidence_24cell as evidence


REPO = Path(__file__).resolve().parents[1]
G14_GRAPH = Path("/mnt/disk0/gem5-CXL-g14-eval/graphs/g14.sg")
G14_CSR = Path(
    "/mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/"
    "formal/g14/vanilla/csr"
)
G14_SHA256 = "72fb08147f63112b4ea3fcff8a14b1713fdf8b097b2cf459a1ecdc217baf6524"
G20_SHA256 = "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"

SHARED = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared"
)
REPLAY_BINARY = Path(
    "/mnt/disk0/gem5-CXL-eval/"
    "timing-evidence-24cell-20260904-tools/replay/trace_replay"
)
MCF_TRACE = SHARED / "mcf-pricing-window0-lazy-r1/trace.v2.json"
AMG_TRACE = (
    SHARED
    / "prepared-relaxed-20260829/spatter-probe-amg-window0/trace.meta.json"
)
AMG_FIXED = (
    SHARED
    / "prepared-relaxed-20260829/"
    "spatter-probe-amg-window0.fixed/trace.meta.json"
)
LULESH_TRACE = (
    SHARED
    / "prepared-relaxed-20260829/"
    "spatter-probe-lulesh-window0/trace.meta.json"
)
LULESH_FIXED = (
    SHARED
    / "prepared-relaxed-20260829/"
    "spatter-probe-lulesh-window0.fixed/trace.meta.json"
)
NPB_CG_TRACE = SHARED / "npb-cg-indexed-sparse-r1/cg-spmv-window0/trace.meta.json"
NPB_CG_FIXED = (
    SHARED
    / "npb-cg-indexed-sparse-r1/cg-spmv-window0.fixed/trace.meta.json"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


class RegistryError(RuntimeError):
    """A registry source or identity violates the G14 contract."""


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise RegistryError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RegistryError(f"{label} SHA-256 is invalid")
    return value


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256_file(path)}


def _validate_file_record(record, label):
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise RegistryError(f"{label} file record is invalid")
    expected = _digest(record["sha256"], label)
    path = Path(record["path"]).resolve()
    if sha256_file(path) != expected:
        raise RegistryError(f"{label} SHA-256 differs")
    return path


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryError(f"{label} JSON is invalid: {error}") from error
    if not isinstance(value, dict):
        raise RegistryError(f"{label} JSON must be an object")
    return value


def _trace_meta(path, label):
    value = _load_json(path, label)
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else value
    workload = meta.get("workload")
    input_sha256 = meta.get("input_sha256")
    if not isinstance(workload, str) or _SHA256.fullmatch(
        str(input_sha256)
    ) is None:
        raise RegistryError(f"{label} identity is missing")
    if path.name == "trace.v2.json":
        try:
            lazy.read_bundle(path.parent)
        except lazy.LazyTraceError as error:
            raise RegistryError(f"{label} lazy bundle differs: {error}") from error
    elif path.name == "trace.meta.json":
        trace_path = path.parent / value.get("trace_path", "")
        if (
            value.get("schema") != 1
            or value.get("trace_record_bytes") != canonical.TRACE_STRUCT.size
            or sha256_file(trace_path) != value.get("trace_sha256")
        ):
            raise RegistryError(f"{label} canonical payload differs")
    else:
        raise RegistryError(f"{label} descriptor filename is unsupported")
    return value, meta


def _validate_source(workload, row, graph_sha256):
    if not isinstance(row, dict):
        raise RegistryError(f"{workload} source record is invalid")
    expected_input = _digest(row.get("input_sha256"), f"{workload} input")
    trace_path = _validate_file_record(row.get("trace"), f"{workload} trace")
    trace_value, trace_meta = _trace_meta(trace_path, f"{workload} trace")
    if trace_meta["workload"] != workload:
        raise RegistryError(f"{workload} trace workload differs")
    if trace_meta["input_sha256"] != expected_input:
        raise RegistryError(f"{workload} trace input SHA-256 differs")
    _validate_file_record(
        row.get("window_manifest"), f"{workload} window manifest"
    )
    phase = row.get("phase")
    window_index = row.get("window_index")
    if (
        isinstance(phase, bool) or not isinstance(phase, int) or phase < 0
        or isinstance(window_index, bool) or not isinstance(window_index, int)
        or window_index < 0
    ):
        raise RegistryError(f"{workload} timing selection is invalid")
    if workload in {"pr_spmv", "gap_bc"}:
        if row.get("graph_sha256") != graph_sha256:
            raise RegistryError("G14 graph identity differs")
    fixed_record = row.get("fixed_trace")
    if fixed_record is not None:
        fixed_path = _validate_file_record(
            fixed_record, f"{workload} fixed trace"
        )
        fixed_value, fixed_meta = _trace_meta(
            fixed_path, f"{workload} fixed trace"
        )
        if fixed_meta["workload"] != workload:
            raise RegistryError(f"{workload} fixed trace workload differs")
        if fixed_meta["input_sha256"] != expected_input:
            raise RegistryError(
                f"{workload} fixed trace input SHA-256 differs"
            )
        prepared = trace_value.get("prepared_window", {})
        bound_fixed_sha256 = prepared.get("fixed_trace_sha256")
        binding_record = row.get("materialization_record")
        if bound_fixed_sha256 is None and binding_record is not None:
            binding_path = _validate_file_record(
                binding_record, f"{workload} materialization record"
            )
            binding = _load_json(
                binding_path, f"{workload} materialization record"
            )
            bound_fixed_sha256 = binding.get("fixed_trace_sha256")
            if (
                binding.get("source_trace_sha256")
                != trace_value.get("source_trace_sha256")
            ):
                raise RegistryError(
                    f"{workload} materialization source binding differs"
                )
        if bound_fixed_sha256 != fixed_value.get("trace_sha256"):
            raise RegistryError(f"{workload} fixed trace binding differs")
    return copy.deepcopy(row)


def _validate_graph(graph_path, graph_sha256, graph_scale):
    _digest(graph_sha256, "G14 graph")
    graph_path = Path(graph_path).resolve()
    if (
        graph_scale != 14
        or graph_sha256 != sha256_file(graph_path)
        or graph_sha256 == G20_SHA256
    ):
        raise RegistryError("G14 graph identity differs")
    return graph_path


def build_registry(
    *, graph_path, graph_sha256, sources, graph_scale=14,
):
    graph_path = _validate_graph(graph_path, graph_sha256, graph_scale)
    if not isinstance(sources, dict) or set(sources) != set(evidence.WORKLOADS):
        raise RegistryError("registry source workload set differs")
    checked = {
        workload: _validate_source(workload, sources[workload], graph_sha256)
        for workload in evidence.WORKLOADS
    }
    cells = {}
    for workload, latency in evidence.COORDINATES:
        row = copy.deepcopy(checked[workload])
        row.update({"workload": workload, "latency": latency})
        cells[f"{workload}:{latency}"] = row
    return {
        "schema": 1,
        "status": "verified",
        "graph": {
            "path": str(graph_path),
            "sha256": graph_sha256,
            "scale": graph_scale,
        },
        "source_records": checked,
        "cells": cells,
    }


def _atomic_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_registry(
    path, *, graph_path, graph_sha256, sources, graph_scale=14,
):
    value = build_registry(
        graph_path=graph_path, graph_sha256=graph_sha256,
        sources=sources, graph_scale=graph_scale,
    )
    _atomic_json(path, value)
    return value


def _phase_work(bundle, phase):
    count = sum(
        invocation.work_items
        for invocation in bundle.invocations
        if invocation.phase == phase
    )
    if count <= 0:
        raise RegistryError(f"lazy phase {phase} has no work")
    return count


def _write_plan(path, trace_path, phase, phase_name, work_items):
    plan = timing.make_plan(sha256_file(trace_path), phase_name, work_items)
    timing.write_plan(path, plan)
    return file_record(path)


def _prepared_source(trace, fixed=None):
    trace = Path(trace).resolve()
    value, meta = _trace_meta(trace, "prepared trace")
    prepared = value.get("prepared_window", {})
    phase = prepared.get("phase", value.get("phase"))
    window_index = prepared.get("window_index", value.get("window_index"))
    if phase is None:
        phases = value.get("phases", [])
        if len(phases) == 1:
            phase = phases[0].get("id")
    row = {
        "input_sha256": meta["input_sha256"],
        "trace": file_record(trace),
        # A fixed prepared trace bypasses plan parsing, but retaining the
        # dynamic descriptor here gives the registry a hash-bound selection.
        "window_manifest": file_record(trace),
        "phase": phase,
        "window_index": window_index,
    }
    if fixed is not None:
        row["fixed_trace"] = file_record(fixed)
        sidecar = trace.parent / "materialized-window.v2.json"
        if sidecar.is_file():
            row["materialization_record"] = file_record(sidecar)
    return row


def prepare(output):
    output = Path(output).resolve()
    if output.exists():
        raise RegistryError(f"fresh G14 registry root required: {output}")
    output.mkdir(parents=True)
    graph = _validate_graph(G14_GRAPH, G14_SHA256, 14)
    binary_sha256 = sha256_file(REPLAY_BINARY)
    source_sha256 = sha256_file(REPO / "util/pr_offload/gapbs_pr_spmv_offload.cc")
    config_sha256 = sha256_file(G14_CSR / "graph.meta.json")

    pr_root = output / "sources/pr_spmv"
    pr_bundle = pr_spmv.build_bundle_from_csr(
        pr_root, csr_root=G14_CSR, graph_path=graph,
        graph_sha256=G14_SHA256, source_sha256=source_sha256,
        binary_sha256=binary_sha256, config_sha256=config_sha256,
        graph_scale=14, iterations=20,
    )
    pr_plan = output / "windows/pr_spmv.json"
    pr_source = {
        "input_sha256": pr_bundle.meta["input_sha256"],
        "graph_sha256": G14_SHA256,
        "trace": file_record(pr_root / "trace.v2.json"),
        "window_manifest": _write_plan(
            pr_plan, pr_root / "trace.v2.json", pr_spmv.PHASE_ITERATION,
            "pr_spmv_iteration", _phase_work(
                pr_bundle, pr_spmv.PHASE_ITERATION
            ),
        ),
        "phase": pr_spmv.PHASE_ITERATION,
        "window_index": 0,
    }

    bc_root = output / "sources/gap_bc"
    bc_bundle = gap_bc.build_bundle_from_csr(
        bc_root, csr_root=G14_CSR, source=0,
        graph_sha256=G14_SHA256,
        source_sha256=sha256_file(REPO / "scripts/gap_bc_lazy_trace.py"),
        binary_sha256=binary_sha256, config_sha256=config_sha256,
    )
    bc_plan = output / "windows/gap_bc.json"
    bc_source = {
        "input_sha256": bc_bundle.meta["input_sha256"],
        "graph_sha256": G14_SHA256,
        "trace": file_record(bc_root / "trace.v2.json"),
        "window_manifest": _write_plan(
            bc_plan, bc_root / "trace.v2.json", gap_bc.PHASE_BFS,
            "bc_bfs", _phase_work(bc_bundle, gap_bc.PHASE_BFS),
        ),
        "phase": gap_bc.PHASE_BFS,
        "window_index": 0,
    }

    mcf_bundle = lazy.read_bundle(MCF_TRACE.parent)
    mcf_plan = output / "windows/mcf.json"
    mcf_source = {
        "input_sha256": mcf_bundle.meta["input_sha256"],
        "trace": file_record(MCF_TRACE),
        "window_manifest": _write_plan(
            mcf_plan, MCF_TRACE, 401, "pricing_kernel",
            _phase_work(mcf_bundle, 401),
        ),
        "phase": 401,
        "window_index": 0,
    }

    sources = {
        "pr_spmv": pr_source,
        "gap_bc": bc_source,
        "mcf": mcf_source,
        "amg_gather": _prepared_source(AMG_TRACE, AMG_FIXED),
        "lulesh_scatter": _prepared_source(LULESH_TRACE, LULESH_FIXED),
        "npb_cg": _prepared_source(NPB_CG_TRACE, NPB_CG_FIXED),
    }
    return write_registry(
        output / "registry.json", graph_path=graph,
        graph_sha256=G14_SHA256, sources=sources, graph_scale=14,
    )


def validate_registry(path):
    value = _load_json(path, "G14 registry")
    graph = value.get("graph", {})
    rebuilt = build_registry(
        graph_path=graph.get("path"), graph_sha256=graph.get("sha256"),
        graph_scale=graph.get("scale"), sources=value.get("source_records"),
    )
    if value != rebuilt:
        raise RegistryError("G14 registry cells differ from source records")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--validate", type=Path)
    options = parser.parse_args(argv)
    value = (
        prepare(options.output)
        if options.output is not None
        else validate_registry(options.validate)
    )
    print(f"G14_24CELL_REGISTRY_PASS cells={len(value['cells'])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RegistryError, lazy.LazyTraceError, timing.TimingError) as error:
        print(f"G14_24CELL_REGISTRY_FAIL: {error}", file=__import__("sys").stderr)
        raise SystemExit(1)
