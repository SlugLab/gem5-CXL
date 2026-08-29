#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Deterministic lazy canonical trace for GAP Brandes BC.

The trace follows GAP's serial verifier order.  That order is the numerical
authority for the M2NDP functional path; the parallel native benchmark is
checked with GAP's native numerical verifier rather than silently requiring a
different atomic accumulation order to be bit exact.
"""

import dataclasses
import hashlib
import math
import os
import struct
import tempfile
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import lazy_work_trace as lazy
except ImportError:
    import canonical_work_trace as canonical
    import lazy_work_trace as lazy


PHASE_RESET = 300
PHASE_SOURCE_INIT = 301
PHASE_BFS = 302
PHASE_REVERSE = 303
PHASE_NORMALIZE = 304
PHASE_NAMES = {
    PHASE_RESET: "bc_reset",
    PHASE_SOURCE_INIT: "bc_source_init",
    PHASE_BFS: "bc_bfs",
    PHASE_REVERSE: "bc_reverse",
    PHASE_NORMALIZE: "bc_normalize",
}

BASES = {
    "offsets": 0x100000000,
    "neighbors": 0x200000000,
    "depths": 0x300000000,
    "path_counts": 0x400000000,
    "deltas": 0x500000000,
    "scores": 0x600000000,
    "queue": 0x680000000,
}

_F32 = struct.Struct("<f")
_F64 = struct.Struct("<d")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_SHA256 = __import__("re").compile(r"[0-9a-f]{64}")


class BCTraceError(lazy.LazyTraceError):
    """A GAP BC input or expansion violates the canonical contract."""


def raw_f32(value):
    return _U32.unpack(_F32.pack(value))[0]


def f32_from_raw(value):
    return _F32.unpack(_U32.pack(value))[0]


def f32(value):
    return f32_from_raw(raw_f32(value))


def raw_f64(value):
    return _U64.unpack(_F64.pack(value))[0]


def f64_from_raw(value):
    return _F64.unpack(_U64.pack(value))[0]


def f64(value):
    return f64_from_raw(raw_f64(value))


def _digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BCTraceError(f"{label} SHA-256 is invalid")
    return value


def _validate_csr(offsets, neighbors, source):
    offsets = tuple(offsets)
    neighbors = tuple(neighbors)
    if len(offsets) < 2 or offsets[0] != 0:
        raise BCTraceError("BC offsets must start at zero")
    nodes = len(offsets) - 1
    previous = 0
    for index, value in enumerate(offsets):
        if (
            not isinstance(value, int) or isinstance(value, bool)
            or value < previous or value > len(neighbors)
        ):
            raise BCTraceError(f"BC offset {index} is invalid")
        previous = value
    if offsets[-1] != len(neighbors) or not neighbors:
        raise BCTraceError("BC offsets do not cover the directed edges")
    for edge, vertex in enumerate(neighbors):
        if (
            not isinstance(vertex, int) or isinstance(vertex, bool)
            or vertex < 0 or vertex >= nodes
        ):
            raise BCTraceError(f"BC neighbor {edge} is outside the graph")
    if (
        not isinstance(source, int) or isinstance(source, bool)
        or source < 0 or source >= nodes
    ):
        raise BCTraceError("BC source is outside the graph")
    return offsets, neighbors, nodes


def _reference(offsets, neighbors, source):
    nodes = len(offsets) - 1
    depths = [-1] * nodes
    path_counts = [0.0] * nodes
    depths[source] = 0
    path_counts[source] = 1.0
    queue = [source]
    cursor = 0
    while cursor < len(queue):
        vertex = queue[cursor]
        cursor += 1
        next_depth = depths[vertex] + 1
        for edge in range(offsets[vertex], offsets[vertex + 1]):
            neighbor = neighbors[edge]
            if depths[neighbor] == -1:
                depths[neighbor] = next_depth
                queue.append(neighbor)
            if depths[neighbor] == next_depth:
                path_counts[neighbor] = f64(
                    path_counts[neighbor] + path_counts[vertex]
                )
    by_depth = {}
    for vertex, depth in enumerate(depths):
        if depth >= 0:
            by_depth.setdefault(depth, []).append(vertex)
    reverse = [
        vertex
        for depth in range(max(by_depth, default=-1), -1, -1)
        for vertex in by_depth.get(depth, ())
    ]
    deltas = [f32(0.0)] * nodes
    scores = [f32(0.0)] * nodes
    for vertex in reverse:
        delta = f32(0.0)
        for edge in range(offsets[vertex], offsets[vertex + 1]):
            neighbor = neighbors[edge]
            if depths[neighbor] == depths[vertex] + 1:
                term = f64(
                    f64(path_counts[vertex] / path_counts[neighbor])
                    * f64(1.0 + float(deltas[neighbor]))
                )
                delta = f32(float(delta) + term)
        deltas[vertex] = delta
        scores[vertex] = f32(scores[vertex] + delta)
    maximum = max(scores)
    if not math.isfinite(maximum) or maximum <= 0:
        raise BCTraceError("BC normalization maximum is not positive")
    scores = [f32(value / maximum) for value in scores]
    return {
        "depths": tuple(depths),
        "path_counts": tuple(path_counts),
        "queue": tuple(queue),
        "reverse": tuple(reverse),
        "scores": tuple(scores),
        "maximum": maximum,
    }


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


def _array(root, name, role, element_type, values):
    formats = {"u32": "I", "u64": "Q", "f32": "f", "f64": "d"}
    values = tuple(values)
    payload = struct.pack(f"<{len(values)}{formats[element_type]}", *values)
    relative = f"images/{name}.{element_type}"
    _atomic_bytes(root / relative, payload)
    return lazy.ArrayImage(
        name, role, element_type, len(values), BASES[name], relative,
        hashlib.sha256(payload).hexdigest(),
    )


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


def _store_raw(invocation, opcode, work_item, address, raw):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, address, raw, 0, raw
    )


def _binary_f32(invocation, opcode, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, 0,
        raw_f32(left), raw_f32(right), raw_f32(result),
    )


def _binary_f64(invocation, opcode, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, 0,
        raw_f64(left), raw_f64(right), raw_f64(result),
    )


def _finish(invocation, operations):
    operations.extend((
        _control(invocation, canonical.Opcode.BARRIER),
        _control(invocation, canonical.Opcode.COMMIT),
    ))
    expected = invocation.parameters.get("record_count")
    if len(operations) != expected:
        raise BCTraceError(
            f"{invocation.kernel} primitive count {len(operations)} != {expected}"
        )
    return tuple(operations)


def expand_reset(state, invocation, _batch_work_items):
    nodes = invocation.parameters.get("nodes")
    if invocation.work_items != nodes:
        raise BCTraceError("BC reset node count differs")
    operations = []
    for vertex in range(nodes):
        depth_address, _ = state.load_raw("depths", vertex)
        path_address, _ = state.load_float("path_counts", vertex)
        delta_address, _ = state.load_float("deltas", vertex)
        state.store_raw("depths", vertex, 0xFFFFFFFF)
        state.store_float("path_counts", vertex, 0.0)
        state.store_float("deltas", vertex, 0.0)
        operations.extend((
            _store_raw(invocation, canonical.Opcode.STORE_U32, vertex,
                       depth_address, 0xFFFFFFFF),
            _store_raw(invocation, canonical.Opcode.STORE_F64, vertex,
                       path_address, raw_f64(0.0)),
            _store_raw(invocation, canonical.Opcode.STORE_F32, vertex,
                       delta_address, raw_f32(0.0)),
        ))
    yield from _finish(invocation, operations)


def expand_source_init(state, invocation, _batch_work_items):
    source = invocation.parameters.get("source")
    depth_address, _ = state.load_raw("depths", source)
    path_address, _ = state.load_float("path_counts", source)
    queue_address, _ = state.load_raw("queue", 0)
    state.store_raw("depths", source, 0)
    state.store_float("path_counts", source, 1.0)
    state.store_raw("queue", 0, source)
    operations = [
        _store_raw(invocation, canonical.Opcode.STORE_U32, 0,
                   depth_address, 0),
        _store_raw(invocation, canonical.Opcode.STORE_F64, 0,
                   path_address, raw_f64(1.0)),
        _store_raw(invocation, canonical.Opcode.STORE_U32, 0,
                   queue_address, source),
    ]
    yield from _finish(invocation, operations)


def expand_bfs_vertex(state, invocation, _batch_work_items):
    parameters = invocation.parameters
    vertex = parameters.get("vertex")
    queue_position = parameters.get("queue_position")
    row_start = parameters.get("row_start")
    row_stop = parameters.get("row_stop")
    queue_address, observed_vertex = state.load_raw("queue", queue_position)
    if observed_vertex != vertex:
        raise BCTraceError("BC queue order differs from descriptor")
    depth_u = state.load_raw("depths", vertex)[1]
    operations = [
        _load(invocation, canonical.Opcode.LOAD_U32, 0,
              queue_address, observed_vertex)
    ]
    for item, edge in enumerate(range(row_start, row_stop)):
        neighbor_address, neighbor = state.load_raw("neighbors", edge)
        depth_address, depth_v = state.load_raw("depths", neighbor)
        operations.extend((
            _load(invocation, canonical.Opcode.LOAD_U32, item,
                  neighbor_address, neighbor),
            _load(invocation, canonical.Opcode.LOAD_U32, item,
                  depth_address, depth_v, dependency=1),
        ))
        if depth_v == 0xFFFFFFFF:
            depth_v = depth_u + 1
            state.store_raw("depths", neighbor, depth_v)
            queue_slot = parameters["discoveries"][edge - row_start]
            if queue_slot < 0:
                raise BCTraceError("BC discovery slot is absent")
            queue_out_address, _ = state.load_raw("queue", queue_slot)
            state.store_raw("queue", queue_slot, neighbor)
            operations.extend((
                _store_raw(invocation, canonical.Opcode.STORE_U32, item,
                           depth_address, depth_v),
                _store_raw(invocation, canonical.Opcode.STORE_U32, item,
                           queue_out_address, neighbor),
            ))
        if depth_v == depth_u + 1:
            path_u_address, path_u = state.load_float("path_counts", vertex)
            path_v_address, path_v = state.load_float("path_counts", neighbor)
            updated = f64(path_v + path_u)
            state.store_float("path_counts", neighbor, updated)
            operations.extend((
                _load(invocation, canonical.Opcode.LOAD_F64, item,
                      path_u_address, raw_f64(path_u)),
                _load(invocation, canonical.Opcode.LOAD_F64, item,
                      path_v_address, raw_f64(path_v)),
                _binary_f64(invocation, canonical.Opcode.F64_ADD, item,
                            path_v, path_u, updated),
                _store_raw(invocation, canonical.Opcode.STORE_F64, item,
                           path_v_address, raw_f64(updated)),
            ))
    yield from _finish(invocation, operations)


def expand_reverse_vertex(state, invocation, _batch_work_items):
    parameters = invocation.parameters
    vertex = parameters.get("vertex")
    row_start = parameters.get("row_start")
    row_stop = parameters.get("row_stop")
    depth_address, depth_u = state.load_raw("depths", vertex)
    operations = [
        _load(invocation, canonical.Opcode.LOAD_U32, 0,
              depth_address, depth_u)
    ]
    delta = f32(0.0)
    for item, edge in enumerate(range(row_start, row_stop)):
        neighbor_address, neighbor = state.load_raw("neighbors", edge)
        depth_v_address, depth_v = state.load_raw("depths", neighbor)
        operations.extend((
            _load(invocation, canonical.Opcode.LOAD_U32, item,
                  neighbor_address, neighbor),
            _load(invocation, canonical.Opcode.LOAD_U32, item,
                  depth_v_address, depth_v, dependency=1),
        ))
        if depth_v != depth_u + 1:
            continue
        path_u_address, path_u = state.load_float("path_counts", vertex)
        path_v_address, path_v = state.load_float("path_counts", neighbor)
        delta_v_address, delta_v = state.load_float("deltas", neighbor)
        if path_v == 0.0:
            raise BCTraceError("BC successor path count is zero")
        ratio = f64(path_u / path_v)
        plus_one = f64(1.0 + float(delta_v))
        term = f64(ratio * plus_one)
        sum_double = f64(float(delta) + term)
        operations.extend((
            _load(invocation, canonical.Opcode.LOAD_F64, item,
                  path_u_address, raw_f64(path_u)),
            _load(invocation, canonical.Opcode.LOAD_F64, item,
                  path_v_address, raw_f64(path_v)),
            _binary_f64(invocation, canonical.Opcode.F64_DIV, item,
                        path_u, path_v, ratio),
            _load(invocation, canonical.Opcode.LOAD_F32, item,
                  delta_v_address, raw_f32(delta_v)),
            _binary_f64(invocation, canonical.Opcode.F64_ADD, item,
                        1.0, float(delta_v), plus_one),
            _binary_f64(invocation, canonical.Opcode.F64_MUL, item,
                        ratio, plus_one, term),
            _binary_f64(invocation, canonical.Opcode.F64_ADD, item,
                        float(delta), term, sum_double),
        ))
        delta = f32(sum_double)
    delta_address, _ = state.load_float("deltas", vertex)
    score_address, score = state.load_float("scores", vertex)
    updated_score = f32(score + delta)
    state.store_float("deltas", vertex, delta)
    state.store_float("scores", vertex, updated_score)
    fixed_item = invocation.work_items
    operations.extend((
        _store_raw(invocation, canonical.Opcode.STORE_F32, fixed_item,
                   delta_address, raw_f32(delta)),
        _load(invocation, canonical.Opcode.LOAD_F32, fixed_item,
              score_address, raw_f32(score)),
        _binary_f32(invocation, canonical.Opcode.F32_ADD, fixed_item,
                    score, delta, updated_score),
        _store_raw(invocation, canonical.Opcode.STORE_F32, fixed_item,
                   score_address, raw_f32(updated_score)),
    ))
    yield from _finish(invocation, operations)


def expand_normalize(state, invocation, _batch_work_items):
    nodes = invocation.parameters.get("nodes")
    maximum = f32_from_raw(invocation.parameters.get("maximum_raw"))
    if invocation.work_items != nodes or maximum <= 0:
        raise BCTraceError("BC normalization parameters differ")
    operations = []
    for vertex in range(nodes):
        address, score = state.load_float("scores", vertex)
        normalized = f32(score / maximum)
        state.store_float("scores", vertex, normalized)
        operations.extend((
            _load(invocation, canonical.Opcode.LOAD_F32, vertex,
                  address, raw_f32(score)),
            _binary_f32(invocation, canonical.Opcode.F32_DIV, vertex,
                        score, maximum, normalized),
            _store_raw(invocation, canonical.Opcode.STORE_F32, vertex,
                       address, raw_f32(normalized)),
        ))
    yield from _finish(invocation, operations)


EXPANDERS = {
    "gap_bc_reset": expand_reset,
    "gap_bc_source_init": expand_source_init,
    "gap_bc_bfs_vertex": expand_bfs_vertex,
    "gap_bc_reverse_vertex": expand_reverse_vertex,
    "gap_bc_normalize": expand_normalize,
}


def primitive_count(invocation):
    if invocation.kernel not in EXPANDERS:
        raise BCTraceError(f"unknown GAP BC kernel {invocation.kernel}")
    value = invocation.parameters.get("record_count")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BCTraceError("GAP BC primitive count is invalid")
    return value


def invocation_boundary_specs(bundle, invocation):
    if invocation.kernel != "gap_bc_normalize":
        return ()
    scores = next(array for array in bundle.arrays if array.name == "scores")
    return (("scores.final", 32, scores.count, scores.logical_base),)


def expand_slice(state, invocation, first, stop, batch_work_items=1024):
    if first != 0 or stop != invocation.work_items:
        raise lazy.LazyTraceError(
            f"partial GAP BC slice is not proved; require whole vertex for {invocation.kernel}"
        )
    try:
        expander = EXPANDERS[invocation.kernel]
    except KeyError as error:
        raise lazy.LazyTraceError(
            f"unknown GAP BC kernel {invocation.kernel}"
        ) from error
    yield from expander(state, invocation, batch_work_items)


def _raw_words(state, bundle, bits, count, base):
    for array in bundle.arrays:
        if array.logical_base == base:
            expected = 32 if array.element_type in {"u32", "f32"} else 64
            if bits != expected or count > array.count:
                raise BCTraceError("BC boundary shape differs")
            return [state.load_raw(array.name, index)[1] for index in range(count)]
    raise BCTraceError("BC boundary base is not declared")


def expanded_evidence(bundle, *, batch_work_items=1024):
    operation_digest = hashlib.sha256()
    sequence = 0
    boundaries = {}
    with lazy.MappedState(bundle) as state:
        for invocation in bundle.invocations:
            expander = EXPANDERS.get(invocation.kernel)
            if expander is None:
                raise BCTraceError(f"unknown GAP BC kernel {invocation.kernel}")
            for operation in expander(state, invocation, batch_work_items):
                lazy._validate_expanded_operation(bundle, invocation, operation)
                sequenced = dataclasses.replace(operation, sequence=sequence)
                operation_digest.update(canonical.TRACE_STRUCT.pack(
                    sequenced.phase, int(sequenced.opcode), 0,
                    sequenced.work_item, sequenced.sequence,
                    sequenced.address, sequenced.operand0,
                    sequenced.operand1, sequenced.result,
                ))
                sequence += 1
            for name, bits, count, base in invocation_boundary_specs(
                bundle, invocation
            ):
                words = _raw_words(state, bundle, bits, count, base)
                payload = b"".join(
                    word.to_bytes(bits // 8, "little") for word in words
                )
                boundaries[name] = {
                    "word_bits": bits,
                    "count": count,
                    "raw_words": words,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
    if sequence != bundle.dynamic_work["primitive_records"]:
        raise BCTraceError("GAP BC dynamic primitive count differs")
    commitments = bundle.meta.get("boundary_commitments", {})
    if {name: row["sha256"] for name, row in boundaries.items()} != commitments:
        raise BCTraceError("GAP BC boundary commitments differ")
    return {
        "operation_count": sequence,
        "operations_sha256": operation_digest.hexdigest(),
        "boundaries": boundaries,
    }


def build_bundle(
    root, *, offsets, neighbors, source, source_sha256,
    binary_sha256, config_sha256,
):
    offsets, neighbors, nodes = _validate_csr(offsets, neighbors, source)
    for value, label in (
        (source_sha256, "source"),
        (binary_sha256, "binary"),
        (config_sha256, "config"),
    ):
        _digest(value, label)
    root = Path(root).resolve()
    if root.exists():
        raise BCTraceError(f"fresh GAP BC bundle root required: {root}")
    (root / "images").mkdir(parents=True)
    reference = _reference(offsets, neighbors, source)
    arrays = (
        _array(root, "offsets", "input", "u64", offsets),
        _array(root, "neighbors", "input", "u32", neighbors),
        _array(root, "depths", "state", "u32", (0xFFFFFFFF,) * nodes),
        _array(root, "path_counts", "state", "f64", (0.0,) * nodes),
        _array(root, "deltas", "state", "f32", (0.0,) * nodes),
        _array(root, "scores", "state", "f32", (0.0,) * nodes),
        _array(root, "queue", "state", "u32", (0,) * nodes),
    )
    invocations = []

    def add(phase, kernel, iteration, work_items, parameters, record_count):
        parameters = {**parameters, "record_count": record_count}
        invocation = lazy.Invocation(
            len(invocations), phase, kernel, iteration,
            work_items, parameters,
        )
        invocations.append(invocation)

    add(PHASE_RESET, "gap_bc_reset", 0, nodes, {"nodes": nodes}, 3 * nodes + 2)
    add(PHASE_SOURCE_INIT, "gap_bc_source_init", 0, 1,
        {"source": source}, 5)

    depth_state = [-1] * nodes
    depth_state[source] = 0
    queue = [source]
    cursor = 0
    while cursor < len(queue):
        vertex = queue[cursor]
        row_start, row_stop = offsets[vertex], offsets[vertex + 1]
        discoveries = [-1] * (row_stop - row_start)
        count = 1
        for edge in range(row_start, row_stop):
            neighbor = neighbors[edge]
            count += 2
            if depth_state[neighbor] == -1:
                depth_state[neighbor] = depth_state[vertex] + 1
                queue.append(neighbor)
                discoveries[edge - row_start] = len(queue) - 1
                count += 2
            if depth_state[neighbor] == depth_state[vertex] + 1:
                count += 4
        count += 2
        add(PHASE_BFS, "gap_bc_bfs_vertex", cursor,
            max(1, row_stop - row_start), {
                "vertex": vertex, "queue_position": cursor,
                "row_start": row_start, "row_stop": row_stop,
                "discoveries": discoveries,
            }, count)
        cursor += 1
    if tuple(queue) != reference["queue"]:
        raise BCTraceError("BC reference queue construction differs")

    for reverse_index, vertex in enumerate(reference["reverse"]):
        row_start, row_stop = offsets[vertex], offsets[vertex + 1]
        successors = sum(
            reference["depths"][neighbors[edge]]
            == reference["depths"][vertex] + 1
            for edge in range(row_start, row_stop)
        )
        count = 1 + 2 * (row_stop - row_start) + 7 * successors + 4 + 2
        add(PHASE_REVERSE, "gap_bc_reverse_vertex", reverse_index,
            max(1, row_stop - row_start), {
                "vertex": vertex, "row_start": row_start,
                "row_stop": row_stop,
            }, count)
    add(PHASE_NORMALIZE, "gap_bc_normalize", 0, nodes, {
        "nodes": nodes,
        "maximum_raw": raw_f32(reference["maximum"]),
        "boundaries": ["scores"],
    }, 3 * nodes + 2)

    score_words = tuple(raw_f32(value) for value in reference["scores"])
    score_payload = struct.pack(f"<{nodes}I", *score_words)
    meta = {
        "schema": 2,
        "workload": "gap_bc",
        "source_sha256": source_sha256,
        "binary_sha256": binary_sha256,
        "config_sha256": config_sha256,
        "initial_scalars": {},
        "nodes": nodes,
        "directed_edges": len(neighbors),
        "source_vertex": source,
        "phase_names": PHASE_NAMES,
        "correctness_policy": "native-verified",
        "boundary_commitments": {
            "scores.final": hashlib.sha256(score_payload).hexdigest(),
        },
    }
    primitive_records = sum(primitive_count(row) for row in invocations)
    lazy.write_bundle(
        root, meta, arrays, invocations,
        {"primitive_records": primitive_records},
    )
    bundle = lazy.read_bundle(root)
    evidence = expanded_evidence(bundle)
    if evidence["boundaries"]["scores.final"]["raw_words"] != list(score_words):
        raise BCTraceError("GAP BC expanded scores differ from reference")
    return bundle
