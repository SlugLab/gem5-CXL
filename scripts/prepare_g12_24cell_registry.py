#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Prepare the hash-bound G12 six-workload timing registry and inputs."""

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
G12_GRAPH = Path("/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg")
G12_GRAPH_MANIFEST = Path(
    "/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json"
)
G12_CSR = Path(
    "/mnt/disk0/gem5-CXL-eval/"
    "pr-scaling-be84a6c362-g12-qualification-v2/scales/g12/m2ndp/csr"
)
SHARED_INPUTS = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json"
)
G12_SHA256 = "759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1"
G12_MANIFEST_SHA256 = (
    "8abefe654015fe287cb5507e06111abc3b9774d4a690b98373b06b2a4d649217"
)
G12_CSR_SHA256 = {
    "graph.meta.json": (
        "93e3321700687387a329ce47ab45a3a9b4d5c8b8ad331d8025ff75628c94ce13"
    ),
    "in_neighbors.i32": (
        "37466ffb237876aaaf73d43a35b231f4490c77b318b2949cb2bf9b2b85925845"
    ),
    "in_offsets.u64": (
        "fec988ecaa3887e5e8a74e579d0fc13ab226ac219c8689d0c2f3661e922d6bb6"
    ),
    "out_degree.u32": (
        "26c8e7ea51bd71631e07b10cbc87df9850b01ff0e81a54989a46a09462a7b484"
    ),
}
G12_NODES = 4096
G12_DIRECTED_EDGES = 96772

SHARED = SHARED_INPUTS.parent
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
_NON_GRAPH_WORKLOADS = ("mcf", "amg_gather", "lulesh_scatter", "npb_cg")


class RegistryError(RuntimeError):
    """A registry source or identity violates the G12 contract."""


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
            raise RegistryError("G12 graph identity differs")
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


def _validate_g12_inputs(
    graph_path, graph_sha256, graph_manifest, csr_records
):
    graph_path = Path(graph_path).resolve()
    if (
        graph_sha256 != G12_SHA256
        or sha256_file(graph_path) != G12_SHA256
    ):
        raise RegistryError("G12 graph identity differs")

    manifest_path = _validate_file_record(
        graph_manifest, "G12 graph manifest"
    )
    if graph_manifest["sha256"] != G12_MANIFEST_SHA256:
        raise RegistryError("G12 graph manifest SHA-256 differs")
    manifest = _load_json(manifest_path, "G12 graph manifest")
    if (
        manifest.get("scale") != 12
        or manifest.get("num_nodes") != G12_NODES
        or manifest.get("directed_edges") != G12_DIRECTED_EDGES
        or manifest.get("graph_sha256") != G12_SHA256
        or Path(manifest.get("graph", "")).resolve() != graph_path
    ):
        raise RegistryError("G12 graph manifest identity differs")

    if not isinstance(csr_records, dict) or set(csr_records) != set(
        G12_CSR_SHA256
    ):
        raise RegistryError("G12 CSR file set differs")
    checked_csr = {}
    for name, expected in G12_CSR_SHA256.items():
        path = _validate_file_record(csr_records[name], f"G12 CSR {name}")
        if csr_records[name]["sha256"] != expected:
            raise RegistryError(f"G12 CSR {name} SHA-256 differs")
        checked_csr[name] = copy.deepcopy(csr_records[name])

    expected_sizes = {
        "in_neighbors.i32": G12_DIRECTED_EDGES * 4,
        "in_offsets.u64": (G12_NODES + 1) * 8,
        "out_degree.u32": G12_NODES * 4,
    }
    for name, size in expected_sizes.items():
        if Path(checked_csr[name]["path"]).stat().st_size != size:
            raise RegistryError(f"G12 CSR {name} size differs")

    meta = _load_json(
        checked_csr["graph.meta.json"]["path"], "G12 CSR metadata"
    )
    if (
        meta.get("graph_sha256") != G12_SHA256
        or meta.get("num_nodes") != G12_NODES
        or meta.get("num_directed_edges") != G12_DIRECTED_EDGES
    ):
        raise RegistryError("G12 CSR identity differs")
    return graph_path, copy.deepcopy(graph_manifest), checked_csr


def build_registry(
    *, graph_path, graph_sha256, graph_manifest, csr_records, sources,
    graph_scale=12,
):
    if graph_scale != 12:
        raise RegistryError("G12 graph identity differs")
    graph_path, checked_manifest, checked_csr = _validate_g12_inputs(
        graph_path, graph_sha256, graph_manifest, csr_records
    )
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
            "manifest": checked_manifest,
            "csr": checked_csr,
            "scale": graph_scale,
            "num_nodes": G12_NODES,
            "directed_edges": G12_DIRECTED_EDGES,
        },
        "source_records": checked,
        "cells": cells,
    }


def build_input_manifest(*, shared_catalog, graph_record, registry_sources):
    if not isinstance(shared_catalog, dict):
        raise RegistryError("shared input catalog is invalid")
    catalog_workloads = shared_catalog.get("workloads")
    if not isinstance(catalog_workloads, dict):
        raise RegistryError("shared input workload catalog is invalid")
    if not isinstance(registry_sources, dict) or set(registry_sources) != set(
        evidence.WORKLOADS
    ):
        raise RegistryError("input manifest source workload set differs")
    if (
        not isinstance(graph_record, dict)
        or graph_record.get("scale") != 12
        or graph_record.get("sha256") != G12_SHA256
        or graph_record.get("num_nodes") != G12_NODES
        or graph_record.get("directed_edges") != G12_DIRECTED_EDGES
    ):
        raise RegistryError("G12 input graph identity differs")

    workloads = {}
    for workload in ("pr_spmv", "gap_bc"):
        workloads[workload] = {
            "path": graph_record.get("path"),
            "sha256": graph_record["sha256"],
            "manifest": graph_record.get("manifest"),
            "manifest_sha256": graph_record.get("manifest_sha256"),
            "scale": 12,
            "num_nodes": G12_NODES,
            "directed_edges": G12_DIRECTED_EDGES,
            "input_sha256": registry_sources[workload]["input_sha256"],
        }
    for workload in _NON_GRAPH_WORKLOADS:
        if workload not in catalog_workloads:
            raise RegistryError(f"shared input {workload} is missing")
        workloads[workload] = {
            "input_sha256": registry_sources[workload]["input_sha256"],
            "source_input": copy.deepcopy(catalog_workloads[workload]),
        }
    return {
        "schema": 1,
        "status": "accepted",
        "graph": copy.deepcopy(graph_record),
        "workloads": workloads,
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
    path, *, graph_path, graph_sha256, graph_manifest, csr_records, sources,
    graph_scale=12,
):
    value = build_registry(
        graph_path=graph_path,
        graph_sha256=graph_sha256,
        graph_manifest=graph_manifest,
        csr_records=csr_records,
        sources=sources,
        graph_scale=graph_scale,
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


def _graph_record(graph_path, manifest_record):
    return {
        "path": str(Path(graph_path).resolve()),
        "sha256": G12_SHA256,
        "manifest": manifest_record["path"],
        "manifest_sha256": manifest_record["sha256"],
        "scale": 12,
        "num_nodes": G12_NODES,
        "directed_edges": G12_DIRECTED_EDGES,
    }


def prepare(output):
    output = Path(output).resolve()
    if output.exists():
        raise RegistryError(f"fresh G12 registry root required: {output}")
    output.mkdir(parents=True)

    graph_manifest = file_record(G12_GRAPH_MANIFEST)
    csr_records = {
        name: file_record(G12_CSR / name) for name in G12_CSR_SHA256
    }
    graph, _, _ = _validate_g12_inputs(
        G12_GRAPH, G12_SHA256, graph_manifest, csr_records
    )
    binary_sha256 = sha256_file(REPLAY_BINARY)
    source_sha256 = sha256_file(REPO / "util/pr_offload/gapbs_pr_spmv_offload.cc")
    config_sha256 = sha256_file(G12_CSR / "graph.meta.json")

    pr_root = output / "sources/pr_spmv"
    pr_bundle = pr_spmv.build_bundle_from_csr(
        pr_root,
        csr_root=G12_CSR,
        graph_path=graph,
        graph_sha256=G12_SHA256,
        source_sha256=source_sha256,
        binary_sha256=binary_sha256,
        config_sha256=config_sha256,
        graph_scale=12,
        iterations=20,
    )
    pr_plan = output / "windows/pr_spmv.json"
    pr_source = {
        "input_sha256": pr_bundle.meta["input_sha256"],
        "graph_sha256": G12_SHA256,
        "trace": file_record(pr_root / "trace.v2.json"),
        "window_manifest": _write_plan(
            pr_plan,
            pr_root / "trace.v2.json",
            pr_spmv.PHASE_ITERATION,
            "pr_spmv_iteration",
            _phase_work(pr_bundle, pr_spmv.PHASE_ITERATION),
        ),
        "phase": pr_spmv.PHASE_ITERATION,
        "window_index": 0,
    }

    bc_root = output / "sources/gap_bc"
    bc_bundle = gap_bc.build_bundle_from_csr(
        bc_root,
        csr_root=G12_CSR,
        source=0,
        graph_sha256=G12_SHA256,
        source_sha256=sha256_file(REPO / "scripts/gap_bc_lazy_trace.py"),
        binary_sha256=binary_sha256,
        config_sha256=config_sha256,
    )
    bc_plan = output / "windows/gap_bc.json"
    bc_source = {
        "input_sha256": bc_bundle.meta["input_sha256"],
        "graph_sha256": G12_SHA256,
        "trace": file_record(bc_root / "trace.v2.json"),
        "window_manifest": _write_plan(
            bc_plan,
            bc_root / "trace.v2.json",
            gap_bc.PHASE_BFS,
            "bc_bfs",
            _phase_work(bc_bundle, gap_bc.PHASE_BFS),
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
            mcf_plan,
            MCF_TRACE,
            401,
            "pricing_kernel",
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
    registry = build_registry(
        graph_path=graph,
        graph_sha256=G12_SHA256,
        graph_manifest=graph_manifest,
        csr_records=csr_records,
        sources=sources,
        graph_scale=12,
    )
    shared_catalog = _load_json(SHARED_INPUTS, "shared input catalog")
    inputs = build_input_manifest(
        shared_catalog=shared_catalog,
        graph_record=_graph_record(graph, graph_manifest),
        registry_sources=sources,
    )
    _atomic_json(output / "inputs.json", inputs)
    _atomic_json(output / "registry.json", registry)
    validate_prepared_root(output)
    return registry


def validate_input_manifest(value, registry):
    if not isinstance(value, dict) or set(value.get("workloads", {})) != set(
        evidence.WORKLOADS
    ):
        raise RegistryError("G12 input manifest workload set differs")
    graph = registry.get("graph", {})
    if value.get("graph") != _graph_record(
        graph.get("path"), graph.get("manifest", {})
    ):
        raise RegistryError("G12 input manifest graph differs")
    for workload in evidence.WORKLOADS:
        row = value["workloads"][workload]
        source = registry["source_records"][workload]
        if row.get("input_sha256") != source.get("input_sha256"):
            raise RegistryError("G12 input manifest differs from registry")
        if workload in {"pr_spmv", "gap_bc"} and (
            row.get("scale") != 12 or row.get("sha256") != G12_SHA256
        ):
            raise RegistryError("G12 input graph identity differs")
    return value


def validate_registry(path):
    value = _load_json(path, "G12 registry")
    graph = value.get("graph", {})
    rebuilt = build_registry(
        graph_path=graph.get("path"),
        graph_sha256=graph.get("sha256"),
        graph_manifest=graph.get("manifest"),
        csr_records=graph.get("csr"),
        graph_scale=graph.get("scale"),
        sources=value.get("source_records"),
    )
    if value != rebuilt:
        raise RegistryError("G12 registry cells differ from source records")
    return value


def validate_prepared_root(root):
    root = Path(root).resolve()
    registry = validate_registry(root / "registry.json")
    inputs = _load_json(root / "inputs.json", "G12 input manifest")
    validate_input_manifest(inputs, registry)
    return registry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--validate", type=Path)
    options = parser.parse_args(argv)
    value = (
        prepare(options.output)
        if options.output is not None
        else validate_prepared_root(Path(options.validate).resolve().parent)
    )
    print(f"G12_24CELL_REGISTRY_PASS cells={len(value['cells'])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RegistryError, lazy.LazyTraceError, timing.TimingError) as error:
        print(f"G12_24CELL_REGISTRY_FAIL: {error}", file=__import__("sys").stderr)
        raise SystemExit(1)
