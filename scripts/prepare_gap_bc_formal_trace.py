#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Prepare a hash-bound g20 GAP BC lazy trace from native verified evidence."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import gap_bc_lazy_trace as bc
except ImportError:
    import cross_system_contract as contract
    import gap_bc_lazy_trace as bc


SOURCE = Path(__file__).resolve()


class PrepareError(RuntimeError):
    """A formal BC input, native run, or trace identity failed closed."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_native_verification(stdout):
    sources = re.findall(r"^Source:\s*([0-9]+)\s*$", stdout, re.MULTILINE)
    passes = re.findall(
        r"^Verification:\s*PASS\s*$", stdout, re.MULTILINE
    )
    if len(sources) != 1:
        raise PrepareError("native BC source marker count differs")
    if len(passes) != 1:
        raise PrepareError("native BC verification did not pass exactly once")
    return int(sources[0])


def config_identity(source, *, threads, iterations):
    value = {
        "schema": 1,
        "benchmark": "gap_bc",
        "source": source,
        "threads": threads,
        "iterations": iterations,
        "all_memory_cxl": True,
        "fp_contract": False,
        "fast_math": False,
        "trace_order": "gap-serial-verifier",
    }
    return hashlib.sha256(contract.canonical_json(value)).hexdigest()


def _atomic_text(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_graph_manifest(path, graph):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrepareError(f"graph manifest is invalid: {error}") from error
    graph = Path(graph).resolve()
    if (
        not isinstance(value, dict) or value.get("schema") != 1
        or value.get("scale") != 20
        or value.get("num_nodes") != 1 << 20
        or Path(value.get("graph", "")).resolve() != graph
        or value.get("graph_sha256") != sha256_file(graph)
    ):
        raise PrepareError("graph manifest does not bind the formal g20 graph")
    return value


def prepare(options):
    root = Path(options.outdir).resolve()
    if root.exists():
        raise PrepareError(f"fresh GAP BC formal root required: {root}")
    root.mkdir(parents=True)
    logs = root / "logs"
    logs.mkdir()
    graph = Path(options.graph).resolve()
    native_bc = Path(options.native_bc).resolve()
    csr_root = Path(options.csr_root).resolve()
    manifest = _load_graph_manifest(options.graph_manifest, graph)
    if not native_bc.is_file() or not os.access(native_bc, os.X_OK):
        raise PrepareError("native GAP BC binary is unavailable")
    command = [
        str(native_bc), "-f", str(graph), "-n", "1", "-i", "1",
        "-l", "-v",
    ]
    environment = dict(os.environ)
    environment["OMP_NUM_THREADS"] = "4"
    completed = subprocess.run(
        command, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, env=environment,
    )
    _atomic_text(logs / "native.stdout.log", completed.stdout)
    _atomic_text(logs / "native.stderr.log", completed.stderr)
    if completed.returncode != 0:
        raise PrepareError(
            f"native GAP BC exited {completed.returncode}"
        )
    source = parse_native_verification(completed.stdout)
    if options.expected_source is not None and source != options.expected_source:
        raise PrepareError("native GAP BC source differs from expected source")
    configuration_sha256 = config_identity(source, threads=4, iterations=1)
    trace_root = root / "trace"
    bundle = bc.build_bundle_from_csr(
        trace_root, csr_root=csr_root, source=source,
        graph_sha256=manifest["graph_sha256"],
        source_sha256=sha256_file(bc.__file__),
        binary_sha256=sha256_file(native_bc),
        config_sha256=configuration_sha256,
    )
    descriptor = trace_root / "trace.v2.json"
    record = {
        "schema": 1,
        "status": "passed",
        "benchmark": "gap_bc",
        "graph_scale": 20,
        "graph": str(graph),
        "graph_sha256": manifest["graph_sha256"],
        "nodes": bundle.meta["nodes"],
        "directed_edges": bundle.meta["directed_edges"],
        "source_vertex": source,
        "threads": 4,
        "iterations": 1,
        "all_memory_cxl": True,
        "correctness_policy": "native-verified",
        "native_verification": "pass",
        "native_command": command,
        "native_binary": str(native_bc),
        "native_binary_sha256": sha256_file(native_bc),
        "native_stdout_sha256": sha256_file(logs / "native.stdout.log"),
        "native_stderr_sha256": sha256_file(logs / "native.stderr.log"),
        "csr_root": str(csr_root),
        "csr_meta_sha256": sha256_file(csr_root / "graph.meta.json"),
        "trace": str(trace_root),
        "trace_descriptor_sha256": sha256_file(descriptor),
        "trace_input_sha256": bundle.meta["input_sha256"],
        "trace_primitive_records": bundle.dynamic_work["primitive_records"],
        "trace_invocations": len(bundle.invocations),
        "boundary_commitments": bundle.meta["boundary_commitments"],
        "config_sha256": configuration_sha256,
        "builder_source_sha256": sha256_file(SOURCE),
        "trace_source_sha256": sha256_file(bc.__file__),
    }
    contract.atomic_write_json(root / "formal-record.json", record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--graph-manifest", type=Path, required=True)
    parser.add_argument("--csr-root", type=Path, required=True)
    parser.add_argument("--native-bc", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--expected-source", type=int)
    options = parser.parse_args(argv)
    try:
        record = prepare(options)
    except (PrepareError, bc.BCTraceError, OSError) as error:
        print(f"GAP_BC_PREPARE_FAILED error={error}", file=sys.stderr)
        return 1
    print(
        "GAP_BC_PREPARE_PASS "
        f"source={record['source_vertex']} "
        f"records={record['trace_primitive_records']} "
        f"manifest={Path(options.outdir).resolve() / 'formal-record.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
