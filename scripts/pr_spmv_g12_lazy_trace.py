#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""G12-bounded schema-2 canonical trace for fixed-20 pull PageRank."""

import hashlib
import json
import os
import re
import struct
import sys
import tempfile
from array import array
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import lazy_work_trace as lazy
except ImportError:
    import canonical_work_trace as canonical
    import lazy_work_trace as lazy


PHASE_ITERATION = 400
BASES = {
    "offsets": 0x100000000,
    "neighbors": 0x200000000,
    "out_degrees": 0x300000000,
    "scores_a": 0x400000000,
    "scores_b": 0x500000000,
    "contributions": 0x600000000,
}

_F32 = struct.Struct("<f")
_U32 = struct.Struct("<I")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PageRankTraceError(lazy.LazyTraceError):
    """A PageRank input or expansion violates the canonical contract."""


def raw_f32(value):
    return _U32.unpack(_F32.pack(value))[0]


def f32_from_raw(value):
    return _F32.unpack(_U32.pack(value))[0]


def f32(value):
    return f32_from_raw(raw_f32(value))


def f32_sub(left, right):
    return f32(f32(left) - f32(right))


def f32_div(left, right):
    return f32(f32(left) / f32(right))


def _digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PageRankTraceError(f"{label} SHA-256 is invalid")
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _native_array(values, typecode, label):
    try:
        result = array(typecode, values)
    except (OverflowError, TypeError, ValueError) as error:
        raise PageRankTraceError(f"PageRank {label} values are invalid") from error
    return result


def _array_image(root, name, role, element_type, values):
    typecodes = {"u32": "I", "u64": "Q", "f32": "f"}
    values = values if isinstance(values, array) else _native_array(
        values, typecodes[element_type], name
    )
    widths = {"u32": 4, "u64": 8, "f32": 4}
    if values.itemsize != widths[element_type]:
        raise PageRankTraceError(f"PageRank {name} element width differs")
    if sys.byteorder != "little":
        values = array(values.typecode, values)
        values.byteswap()
    payload = memoryview(values).cast("B")
    relative = f"images/{name}.{element_type}"
    _atomic_bytes(root / relative, payload)
    return lazy.ArrayImage(
        name, role, element_type, len(values), BASES[name], relative,
        hashlib.sha256(payload).hexdigest(),
    )


def _validate_csr(offsets, neighbors, out_degrees):
    offsets = _native_array(offsets, "Q", "offsets")
    neighbors = _native_array(neighbors, "I", "neighbors")
    out_degrees = _native_array(out_degrees, "I", "out degrees")
    if len(offsets) < 2 or offsets[0] != 0:
        raise PageRankTraceError("PageRank offsets must start at zero")
    nodes = len(offsets) - 1
    if len(out_degrees) != nodes:
        raise PageRankTraceError("PageRank out-degree count differs")
    previous = 0
    for index, value in enumerate(offsets):
        if value < previous or value > len(neighbors):
            raise PageRankTraceError(f"PageRank offset {index} is invalid")
        previous = value
    if offsets[-1] != len(neighbors):
        raise PageRankTraceError("PageRank offsets do not cover the edges")
    if sum(out_degrees) != len(neighbors):
        raise PageRankTraceError("PageRank out degrees do not cover the edges")
    for edge, vertex in enumerate(neighbors):
        if vertex >= nodes:
            raise PageRankTraceError(
                f"PageRank neighbor {edge} is outside the graph"
            )
    return offsets, neighbors, out_degrees, nodes


def _read_native_array(path, typecode, expected_count, label):
    path = Path(path)
    itemsize = array(typecode).itemsize
    if path.stat().st_size != expected_count * itemsize:
        raise PageRankTraceError(f"PageRank {label} byte count differs")
    values = array(typecode)
    with path.open("rb") as stream:
        values.fromfile(stream, expected_count)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _control(invocation, opcode):
    return canonical.Operation(
        invocation.phase, opcode, invocation.work_items, 0,
        0, 0, invocation.work_items, 0,
    )


def _load(invocation, opcode, work_item, address, raw, dependency=0):
    operand1 = 0
    if dependency:
        operand1 = canonical.LOAD_DEPENDENCY_RELATIVE_FLAG | dependency
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0,
        address, raw, operand1, raw,
    )


def _store(invocation, work_item, address, value):
    raw = raw_f32(value)
    return canonical.Operation(
        invocation.phase, canonical.Opcode.STORE_F32, work_item, 0,
        address, raw, 0, raw,
    )


def _binary(invocation, opcode, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, 0,
        raw_f32(left), raw_f32(right), raw_f32(result),
    )


def _parameters(invocation):
    parameters = invocation.parameters
    nodes = parameters.get("nodes")
    source = parameters.get("source")
    destination = parameters.get("destination")
    if (
        invocation.kernel != "pr_spmv_iteration"
        or invocation.work_items != nodes
        or source not in {"scores_a", "scores_b"}
        or destination not in {"scores_a", "scores_b"}
        or source == destination
    ):
        raise PageRankTraceError("PageRank iteration parameters differ")
    return parameters, nodes, source, destination


def _iteration_operations(state, invocation):
    parameters, nodes, source, destination = _parameters(invocation)
    damping = f32_from_raw(parameters["damping_raw"])
    base_score = f32_from_raw(parameters["base_score_raw"])

    yield _control(invocation, canonical.Opcode.BARRIER)
    for vertex in range(nodes):
        score_address, score = state.load_float(source, vertex)
        degree_address, degree = state.load_raw("out_degrees", vertex)
        yield _load(
            invocation, canonical.Opcode.LOAD_F32, vertex,
            score_address, raw_f32(score),
        )
        yield _load(
            invocation, canonical.Opcode.LOAD_U32, vertex,
            degree_address, degree,
        )
        if degree:
            contribution = f32(score / f32(degree))
            yield _binary(
                invocation, canonical.Opcode.F32_DIV, vertex,
                score, f32(degree), contribution,
            )
        else:
            contribution = f32(0.0)
        contribution_address, _ = state.load_float("contributions", vertex)
        state.store_float("contributions", vertex, contribution)
        yield _store(invocation, vertex, contribution_address, contribution)

    yield _control(invocation, canonical.Opcode.BARRIER)
    for vertex in range(nodes):
        start_address, row_start = state.load_raw("offsets", vertex)
        stop_address, row_stop = state.load_raw("offsets", vertex + 1)
        yield _load(
            invocation, canonical.Opcode.LOAD_U64, vertex,
            start_address, row_start,
        )
        yield _load(
            invocation, canonical.Opcode.LOAD_U64, vertex,
            stop_address, row_stop,
        )
        incoming = f32(0.0)
        for edge in range(row_start, row_stop):
            neighbor_address, neighbor = state.load_raw("neighbors", edge)
            contribution_address, contribution = state.load_float(
                "contributions", neighbor
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_U32, vertex,
                neighbor_address, neighbor,
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F32, vertex,
                contribution_address, raw_f32(contribution), dependency=1,
            )
            updated = f32(incoming + contribution)
            yield _binary(
                invocation, canonical.Opcode.F32_ADD, vertex,
                incoming, contribution, updated,
            )
            incoming = updated
        product = f32(damping * incoming)
        result = f32(base_score + product)
        yield _binary(
            invocation, canonical.Opcode.F32_MUL, vertex,
            damping, incoming, product,
        )
        yield _binary(
            invocation, canonical.Opcode.F32_ADD, vertex,
            base_score, product, result,
        )
        destination_address, _ = state.load_float(destination, vertex)
        state.store_float(destination, vertex, result)
        yield _store(invocation, vertex, destination_address, result)
    yield _control(invocation, canonical.Opcode.BARRIER)
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_iteration(state, invocation, _batch_work_items):
    count = 0
    for operation in _iteration_operations(state, invocation):
        count += 1
        yield operation
    expected = invocation.parameters.get("record_count")
    if count != expected:
        raise PageRankTraceError(
            f"PageRank primitive count {count} != {expected}"
        )


EXPANDERS = {"pr_spmv_iteration": expand_iteration}


def fast_forward(state, invocation, first=0, stop=None):
    if stop is None:
        stop = invocation.work_items
    if first != 0 or stop != invocation.work_items:
        raise PageRankTraceError(
            "PageRank fast-forward requires a whole iteration"
        )
    parameters, nodes, source, destination = _parameters(invocation)
    damping = f32_from_raw(parameters["damping_raw"])
    base_score = f32_from_raw(parameters["base_score_raw"])
    for vertex in range(nodes):
        score = state.load_float(source, vertex)[1]
        degree = state.load_raw("out_degrees", vertex)[1]
        contribution = (
            f32(score / f32(degree)) if degree else f32(0.0)
        )
        state.store_float("contributions", vertex, contribution)
    for vertex in range(nodes):
        row_start = state.load_raw("offsets", vertex)[1]
        row_stop = state.load_raw("offsets", vertex + 1)[1]
        incoming = f32(0.0)
        for edge in range(row_start, row_stop):
            neighbor = state.load_raw("neighbors", edge)[1]
            contribution = state.load_float("contributions", neighbor)[1]
            incoming = f32(incoming + contribution)
        state.store_float(
            destination, vertex,
            f32(base_score + f32(damping * incoming)),
        )


def fixed_controls(invocation):
    _parameters(invocation)
    return (
        _control(invocation, canonical.Opcode.BARRIER),
        _control(invocation, canonical.Opcode.BARRIER),
        _control(invocation, canonical.Opcode.BARRIER),
        _control(invocation, canonical.Opcode.COMMIT),
    )


def primitive_count(invocation):
    _parameters(invocation)
    value = invocation.parameters.get("record_count")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PageRankTraceError("PageRank primitive count is invalid")
    return value


def invocation_boundary_specs(bundle, invocation):
    if invocation.ordinal != len(bundle.invocations) - 1:
        return ()
    destination = invocation.parameters["destination"]
    scores = next(row for row in bundle.arrays if row.name == destination)
    return (("scores.final", 32, scores.count, scores.logical_base),)


def expand_slice(
    state, invocation, first, stop, batch_work_items=1024,
    *, include_controls=True,
):
    if first != 0 or stop != invocation.work_items:
        raise PageRankTraceError(
            "partial PageRank iteration slice is not proved"
        )
    if not include_controls:
        raise PageRankTraceError(
            "control-free PageRank expansion is not defined"
        )
    yield from expand_iteration(state, invocation, batch_work_items)


def _reference(offsets, neighbors, out_degrees, iterations):
    nodes = len(out_degrees)
    initial = f32_div(f32(1.0), f32(nodes))
    damping = f32(0.85)
    base_score = f32_div(f32_sub(f32(1.0), damping), f32(nodes))
    scores = [initial] * nodes
    next_scores = [f32(0.0)] * nodes
    contributions = [f32(0.0)] * nodes
    for _iteration in range(iterations):
        for vertex in range(nodes):
            degree = out_degrees[vertex]
            contributions[vertex] = (
                f32(scores[vertex] / f32(degree)) if degree else f32(0.0)
            )
        for vertex in range(nodes):
            incoming = f32(0.0)
            for edge in range(offsets[vertex], offsets[vertex + 1]):
                incoming = f32(incoming + contributions[neighbors[edge]])
            product = f32(damping * incoming)
            next_scores[vertex] = f32(base_score + product)
        scores, next_scores = next_scores, scores
    return initial, damping, base_score, scores


def _expanded_final_words(bundle):
    with lazy.MappedState(bundle) as state:
        for invocation in bundle.invocations:
            fast_forward(state, invocation)
        destination = bundle.invocations[-1].parameters["destination"]
        return [
            state.load_raw(destination, index)[1]
            for index in range(bundle.meta["nodes"])
        ]


def build_bundle(
    root, *, offsets, neighbors, out_degrees, graph_sha256,
    source_sha256, binary_sha256, config_sha256, graph_scale=12,
    iterations=20, verify_expansion=True,
):
    for value, label in (
        (graph_sha256, "graph"), (source_sha256, "source"),
        (binary_sha256, "binary"), (config_sha256, "config"),
    ):
        _digest(value, label)
    if graph_scale != 12:
        raise PageRankTraceError("G12 graph identity differs")
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations <= 0:
        raise PageRankTraceError("PageRank iteration count is invalid")
    offsets, neighbors, out_degrees, nodes = _validate_csr(
        offsets, neighbors, out_degrees
    )
    root = Path(root).resolve()
    if root.exists():
        raise PageRankTraceError(f"fresh PageRank bundle root required: {root}")
    (root / "images").mkdir(parents=True)
    initial, damping, base_score, final_scores = _reference(
        offsets, neighbors, out_degrees, iterations
    )
    arrays = (
        _array_image(root, "offsets", "input", "u64", offsets),
        _array_image(root, "neighbors", "input", "u32", neighbors),
        _array_image(root, "out_degrees", "input", "u32", out_degrees),
        _array_image(root, "scores_a", "state", "f32", array("f", [initial]) * nodes),
        _array_image(root, "scores_b", "state", "f32", array("f", [0.0]) * nodes),
        _array_image(root, "contributions", "state", "f32", array("f", [0.0]) * nodes),
    )
    nonzero_degrees = sum(value != 0 for value in out_degrees)
    per_iteration = 4 + 8 * nodes + nonzero_degrees + 3 * len(neighbors)
    invocations = tuple(
        lazy.Invocation(
            ordinal=index,
            phase=PHASE_ITERATION,
            kernel="pr_spmv_iteration",
            iteration=index,
            work_items=nodes,
            parameters={
                "nodes": nodes,
                "source": "scores_a" if index % 2 == 0 else "scores_b",
                "destination": "scores_b" if index % 2 == 0 else "scores_a",
                "damping_raw": raw_f32(damping),
                "base_score_raw": raw_f32(base_score),
                "record_count": per_iteration,
            },
        )
        for index in range(iterations)
    )
    final_payload = b"".join(
        raw_f32(value).to_bytes(4, "little") for value in final_scores
    )
    meta = {
        "schema": 2,
        "workload": "pr_spmv",
        "source_sha256": source_sha256,
        "binary_sha256": binary_sha256,
        "config_sha256": config_sha256,
        "initial_scalars": {},
        "graph_sha256": graph_sha256,
        "graph_scale": graph_scale,
        "iterations": iterations,
        "nodes": nodes,
        "directed_edges": len(neighbors),
        "phase_names": {PHASE_ITERATION: "pr_spmv_iteration"},
        "floating_point_contract": "strict-csr-order-f32-each-operation",
        "full_expansion_verified": verify_expansion,
        "generator_sha256": sha256_file(Path(__file__).resolve()),
        "boundary_commitments": {
            "scores.final": hashlib.sha256(final_payload).hexdigest(),
        },
    }
    lazy.write_bundle(
        root, meta, arrays, invocations,
        {"primitive_records": per_iteration * iterations},
    )
    bundle = lazy.read_bundle(root)
    if verify_expansion:
        observed = _expanded_final_words(bundle)
        expected = [raw_f32(value) for value in final_scores]
        if observed != expected:
            raise PageRankTraceError(
                "PageRank expanded scores differ from the reference"
            )
    return bundle


def build_bundle_from_csr(
    root, *, csr_root, graph_path, graph_sha256, source_sha256,
    binary_sha256, config_sha256, graph_scale=12, iterations=20,
):
    graph_path = Path(graph_path).resolve()
    _digest(graph_sha256, "graph")
    try:
        observed_graph_sha256 = sha256_file(graph_path)
    except OSError as error:
        raise PageRankTraceError(
            f"G12 graph identity differs: {error}"
        ) from error
    if graph_scale != 12 or observed_graph_sha256 != graph_sha256:
        raise PageRankTraceError("G12 graph identity differs")
    csr_root = Path(csr_root).resolve()
    try:
        metadata = json.loads(
            (csr_root / "graph.meta.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PageRankTraceError(
            f"PageRank graph metadata is invalid: {error}"
        ) from error
    if not isinstance(metadata, dict):
        raise PageRankTraceError("G12 graph identity differs")
    nodes = metadata.get("num_nodes")
    edges = metadata.get("num_directed_edges")
    if (
        metadata.get("schema") != 1
        or metadata.get("graph_sha256") != graph_sha256
        or metadata.get("directed") is not False
        or not isinstance(nodes, int) or isinstance(nodes, bool) or nodes <= 0
        or not isinstance(edges, int) or isinstance(edges, bool) or edges <= 0
    ):
        raise PageRankTraceError("G12 graph identity differs")
    try:
        offsets = _read_native_array(
            csr_root / "in_offsets.u64", "Q", nodes + 1, "offsets"
        )
        neighbors = _read_native_array(
            csr_root / "in_neighbors.i32", "I", edges, "neighbors"
        )
        out_degrees = _read_native_array(
            csr_root / "out_degree.u32", "I", nodes, "out degrees"
        )
    except OSError as error:
        raise PageRankTraceError(
            f"PageRank CSR input is unavailable: {error}"
        ) from error
    return build_bundle(
        root, offsets=offsets, neighbors=neighbors,
        out_degrees=out_degrees, graph_sha256=graph_sha256,
        source_sha256=source_sha256, binary_sha256=binary_sha256,
        config_sha256=config_sha256, graph_scale=graph_scale,
        iterations=iterations, verify_expansion=False,
    )

