#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Exact prefix indexes for formal NPB lazy invocation streams."""

from __future__ import annotations

import dataclasses
import argparse
import hashlib
import os
import shutil
import struct
import sys
import tempfile
from bisect import bisect_right
from pathlib import Path

from scripts import cross_system_contract as evidence_contract
from scripts import canonical_work_trace as canonical
from scripts import indexed_window_contract as contract
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb


_UINT64_MAX = (1 << 64) - 1
_PLAN_HEADER = struct.Struct("<8sQQ32s")
_PLAN_REQUEST = struct.Struct("<QQQQ")
_PLAN_MAGIC = b"NPBSPN01"
_PLAN_SCHEMA = 1
ARRAY_REQUEST = 1
SCALAR_REQUEST = 2


class IndexError(RuntimeError):
    """An NPB prefix index or seek coordinate is invalid."""


@dataclasses.dataclass(frozen=True)
class LocatedPrimitive:
    segment: contract.IndexSegment
    local_offset: int


@dataclasses.dataclass(frozen=True)
class ArrayRequest:
    ordinal: int
    array_id: int
    index: int


@dataclasses.dataclass(frozen=True)
class ScalarRequest:
    ordinal: int
    scalar_id: int


@dataclasses.dataclass(frozen=True)
class SparseCapturePlan:
    descriptor_sha256: str
    entries: tuple[ArrayRequest | ScalarRequest, ...]


def _request_key(entry):
    if isinstance(entry, ArrayRequest):
        return entry.ordinal, ARRAY_REQUEST, entry.array_id, entry.index
    if isinstance(entry, ScalarRequest):
        return entry.ordinal, SCALAR_REQUEST, entry.scalar_id, 0
    raise IndexError("NPB sparse plan request type is invalid")


def validate_sparse_capture_plan(plan):
    """Require one canonical ordered set of exact sparse-state requests."""

    if not isinstance(plan, SparseCapturePlan):
        raise IndexError("NPB sparse capture plan type is invalid")
    try:
        bytes.fromhex(plan.descriptor_sha256)
    except (TypeError, ValueError) as error:
        raise IndexError("NPB sparse plan descriptor SHA-256 is invalid") from error
    if len(plan.descriptor_sha256) != 64 or plan.descriptor_sha256.lower() != (
        plan.descriptor_sha256
    ):
        raise IndexError("NPB sparse plan descriptor SHA-256 is invalid")
    if not isinstance(plan.entries, tuple) or not plan.entries:
        raise IndexError("NPB sparse plan requests are missing")
    keys = []
    for entry in plan.entries:
        key = _request_key(entry)
        for value in key:
            if (
                not isinstance(value, int) or isinstance(value, bool)
                or value < 0 or value > _UINT64_MAX
            ):
                raise IndexError("NPB sparse plan request is outside uint64")
        if key[2] == 0:
            raise IndexError("NPB sparse plan request id must be positive")
        keys.append(key)
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise IndexError("NPB sparse plan requests must be uniquely ordered")
    return plan


def _plan_payload(plan):
    validate_sparse_capture_plan(plan)
    payload = bytearray(_PLAN_HEADER.pack(
        _PLAN_MAGIC, _PLAN_SCHEMA, len(plan.entries),
        bytes.fromhex(plan.descriptor_sha256),
    ))
    for entry in plan.entries:
        payload.extend(_PLAN_REQUEST.pack(*_request_key(entry)))
    return bytes(payload)


def write_sparse_capture_plan(path, plan):
    """Atomically write the native little-endian sparse capture plan."""

    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _plan_payload(plan)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def read_sparse_capture_plan(path):
    """Read a plan without accepting truncation, extension, or reordering."""

    path = Path(path).resolve()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise IndexError(f"cannot read NPB sparse capture plan: {error}") from error
    if len(payload) < _PLAN_HEADER.size:
        raise IndexError("NPB sparse capture plan header is truncated")
    magic, schema, count, descriptor = _PLAN_HEADER.unpack_from(payload)
    if magic != _PLAN_MAGIC or schema != _PLAN_SCHEMA:
        raise IndexError("NPB sparse capture plan header is invalid")
    expected = _PLAN_HEADER.size + count * _PLAN_REQUEST.size
    if count == 0 or expected != len(payload):
        raise IndexError("NPB sparse capture plan record count differs")
    entries = []
    cursor = _PLAN_HEADER.size
    for _ in range(count):
        ordinal, kind, request_id, index = _PLAN_REQUEST.unpack_from(
            payload, cursor
        )
        cursor += _PLAN_REQUEST.size
        if kind == ARRAY_REQUEST:
            entries.append(ArrayRequest(ordinal, request_id, index))
        elif kind == SCALAR_REQUEST and index == 0:
            entries.append(ScalarRequest(ordinal, request_id))
        else:
            raise IndexError("NPB sparse capture plan request kind is invalid")
    plan = SparseCapturePlan(descriptor.hex(), tuple(entries))
    validate_sparse_capture_plan(plan)
    if _plan_payload(plan) != payload:
        raise IndexError("NPB sparse capture plan encoding differs")
    return plan


def migrate_cg_descriptor(source_root, outdir):
    """Add exact CG SpMV cardinality while hard-linking immutable images."""

    source = lazy.read_bundle(source_root)
    if source.meta.get("workload") != "npb_cg":
        raise IndexError("NPB CG descriptor migration workload differs")
    try:
        nonzeros = next(
            array.count for array in source.arrays if array.name == "colidx"
        )
    except StopIteration as error:
        raise IndexError("NPB CG colidx image is missing") from error
    if nonzeros <= 0:
        raise IndexError("NPB CG nonzero count is invalid")
    invocations = tuple(
        dataclasses.replace(
            invocation,
            parameters={**invocation.parameters, "nonzeros": nonzeros},
        )
        if invocation.kernel == "npb_cg_spmv" else invocation
        for invocation in source.invocations
    )
    if not any(
        invocation.kernel == "npb_cg_spmv"
        for invocation in invocations
    ):
        raise IndexError("NPB CG descriptor has no SpMV invocation")
    try:
        primitive_records = sum(npb.primitive_count(row) for row in invocations)
    except lazy.LazyTraceError as error:
        raise IndexError(f"migrated NPB CG cardinality is invalid: {error}") from error
    if primitive_records != source.dynamic_work.get("primitive_records"):
        raise IndexError(
            "migrated NPB CG primitive count differs from source descriptor"
        )
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise IndexError(f"fresh NPB CG migration root required: {outdir}")
    outdir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(
        prefix=f".{outdir.name}.", dir=outdir.parent
    ))
    try:
        for array in source.arrays:
            source_path = source.root / array.path
            target = stage / array.path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_path, target)
            except OSError as error:
                raise IndexError(
                    "NPB CG migration requires same-filesystem hard links"
                ) from error
        meta = dict(source.meta)
        meta["descriptor_migration"] = {
            "kind": "cg-spmv-nonzeros-v1",
            "source_descriptor_sha256": _sha256_file(
                source.root / "trace.v2.json"
            ),
            "generator_sha256": _generator_sha256(),
        }
        lazy.write_bundle(
            stage, meta, source.arrays, invocations,
            dict(source.dynamic_work),
        )
        migrated = lazy.read_bundle(stage)
        build_index(migrated)
        os.replace(stage, outdir)
        return lazy.read_bundle(outdir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def cg_spmv_sparse_plan(
    bundle, ordinal, *, row_first, row_stop, array_ids
):
    """Select the exact live words read by one bounded CG SpMV row slice."""

    if not isinstance(bundle, lazy.LazyBundle):
        raise IndexError("NPB CG sparse plan bundle type differs")
    if bundle.meta.get("workload") != "npb_cg":
        raise IndexError("NPB CG sparse plan workload differs")
    if (
        not isinstance(ordinal, int) or isinstance(ordinal, bool)
        or ordinal < 0 or ordinal >= len(bundle.invocations)
    ):
        raise IndexError("NPB CG sparse plan ordinal is invalid")
    invocation = bundle.invocations[ordinal]
    if invocation.kernel != "npb_cg_spmv":
        raise IndexError("NPB CG sparse plan invocation is not SpMV")
    if (
        not isinstance(row_first, int) or isinstance(row_first, bool)
        or not isinstance(row_stop, int) or isinstance(row_stop, bool)
        or not 0 <= row_first < row_stop <= invocation.work_items
    ):
        raise IndexError("NPB CG sparse plan row slice is invalid")
    expected_ids = {"rowstr", "colidx", "a", "p", "q"}
    if not isinstance(array_ids, dict) or set(array_ids) != expected_ids:
        raise IndexError("NPB CG sparse plan array id mapping differs")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in array_ids.values()
    ) or len(set(array_ids.values())) != len(array_ids):
        raise IndexError("NPB CG sparse plan array ids are invalid")
    parameters = invocation.parameters
    semantic_names = {
        "rowstr": parameters.get("rowstr"),
        "colidx": parameters.get("colidx"),
        "a": parameters.get("values"),
        "p": parameters.get("source"),
        "q": parameters.get("destination"),
    }
    available = {array.name: array for array in bundle.arrays}
    if any(name not in available for name in semantic_names.values()):
        raise IndexError("NPB CG sparse plan array binding is missing")
    edge_base = parameters.get("edge_base")
    column_base = parameters.get("column_base")
    if edge_base not in (0, 1) or column_base not in (0, 1):
        raise IndexError("NPB CG sparse plan index base differs")
    requests = set()
    with lazy.MappedState(bundle) as state:
        offsets = []
        for row in range(row_first, row_stop + 1):
            _address, value = state.load_raw(semantic_names["rowstr"], row)
            offsets.append(value)
            requests.add((ordinal, ARRAY_REQUEST, array_ids["rowstr"], row))
        if any(right < left for left, right in zip(offsets, offsets[1:])):
            raise IndexError("NPB CG sparse plan row offsets decrease")
        edge_first = offsets[0] - edge_base
        edge_stop = offsets[-1] - edge_base
        if edge_first < 0 or edge_stop < edge_first:
            raise IndexError("NPB CG sparse plan edge range is invalid")
        for edge in range(edge_first, edge_stop):
            _address, column = state.load_raw(
                semantic_names["colidx"], edge
            )
            source_index = column - column_base
            if source_index < 0:
                raise IndexError("NPB CG sparse plan column is invalid")
            requests.add((ordinal, ARRAY_REQUEST, array_ids["colidx"], edge))
            requests.add((ordinal, ARRAY_REQUEST, array_ids["a"], edge))
            requests.add((ordinal, ARRAY_REQUEST, array_ids["p"], source_index))
        for row in range(row_first, row_stop):
            requests.add((ordinal, ARRAY_REQUEST, array_ids["q"], row))
    entries = tuple(
        ArrayRequest(request_ordinal, request_id, index)
        for request_ordinal, kind, request_id, index in sorted(requests)
        if kind == ARRAY_REQUEST
    )
    return validate_sparse_capture_plan(SparseCapturePlan(
        descriptor_sha256=_sha256_file(bundle.root / "trace.v2.json"),
        entries=entries,
    ))


def materialize_cg_spmv_window(
    bundle, plan, capture, *, ordinal, row_first, measure_start, row_stop,
    array_ids, outdir, plan_path, capture_path, batch_work_items=1024,
):
    """Materialize one exact CG SpMV window from native live sparse state."""

    validate_sparse_capture_plan(plan)
    if not 0 <= row_first < measure_start < row_stop:
        raise IndexError("NPB CG materialized window coordinates are invalid")
    expected_plan = cg_spmv_sparse_plan(
        bundle, ordinal, row_first=row_first, row_stop=row_stop,
        array_ids=array_ids,
    )
    if plan != expected_plan:
        raise IndexError("NPB CG sparse plan differs from requested window")
    plan_path = Path(plan_path).resolve()
    capture_path = Path(capture_path).resolve()
    if _sha256_file(plan_path) != getattr(capture, "plan_sha256", None):
        raise IndexError("NPB CG sparse capture plan SHA-256 differs")
    if _sha256_file(capture_path) != getattr(capture, "capture_sha256", None):
        raise IndexError("NPB CG sparse capture file SHA-256 differs")
    if (
        getattr(capture, "descriptor_sha256", None)
        != plan.descriptor_sha256
        or getattr(capture, "request_count", None) != len(plan.entries)
        or len(getattr(capture, "records", ())) != len(plan.entries)
    ):
        raise IndexError("NPB CG sparse capture identity differs")
    records = tuple(capture.records)
    for request, record in zip(plan.entries, records):
        if _request_key(request) != (
            record.ordinal, record.kind, record.request_id, record.index
        ):
            raise IndexError("NPB CG sparse capture record differs from plan")
    invocation = bundle.invocations[ordinal]
    semantic_names = {
        "rowstr": invocation.parameters["rowstr"],
        "colidx": invocation.parameters["colidx"],
        "a": invocation.parameters["values"],
        "p": invocation.parameters["source"],
        "q": invocation.parameters["destination"],
    }
    arrays = {array.name: array for array in bundle.arrays}
    by_id = {
        array_ids[semantic]: arrays[name]
        for semantic, name in semantic_names.items()
    }
    with lazy.MappedState(bundle) as state:
        for request, record in zip(plan.entries, records):
            if not isinstance(request, ArrayRequest):
                raise IndexError("NPB CG SpMV window cannot request scalars")
            array = by_id.get(request.array_id)
            if array is None or request.index >= array.count:
                raise IndexError("NPB CG sparse capture array binding differs")
            bits = 32 if array.element_type in {"u32", "f32"} else 64
            if bits == 32 and record.raw_word >= 1 << 32:
                raise IndexError("NPB CG sparse capture uint32 word is invalid")
            if array.role == "input":
                _address, expected = state.load_raw(array.name, request.index)
                if record.raw_word != expected:
                    raise IndexError(
                        "NPB CG immutable sparse word differs from source image"
                    )
            else:
                state.store_raw(array.name, request.index, record.raw_word)

        def partitioned_operations():
            sequence = 0
            yield True, dataclasses.replace(npb._control(
                invocation, canonical.Opcode.BARRIER,
                invocation.work_items,
            ), sequence=sequence)
            sequence += 1
            for operation in npb.expand_slice(
                state, invocation, row_first, row_stop, batch_work_items
            ):
                lazy._validate_expanded_operation(
                    bundle, invocation, operation
                )
                yield False, dataclasses.replace(
                    operation, sequence=sequence,
                    work_item=operation.work_item - row_first
                )
                sequence += 1
            yield True, dataclasses.replace(npb._control(
                invocation, canonical.Opcode.COMMIT,
                invocation.work_items,
            ), sequence=sequence)

        try:
            from scripts import run_matched_breadth_gem5 as replay
        except ImportError:
            import run_matched_breadth_gem5 as replay
        outdir = Path(outdir).resolve()
        fixed_root = outdir.with_name(outdir.name + ".fixed")
        dynamic_record, fixed_record = replay._write_partitioned_payload(
            outdir, fixed_root, partitioned_operations()
        )
    # Canonical LOAD records already carry the exact live operand0 word.  An
    # empty image table makes both replay and M2NDP derive one packed sparse
    # map from those first-use operands instead of creating one tiny file for
    # every random CG source index.
    initial_memory = {}
    fixed_initial_memory = {}
    source_sha256 = _sha256_file(bundle.root / "trace.v2.json")
    common = {
        "schema": 1,
        "workload": "npb_cg",
        "input_sha256": bundle.meta["input_sha256"],
        "source_sha256": bundle.meta["source_sha256"],
        "binary_sha256": bundle.meta["binary_sha256"],
        "config_sha256": bundle.meta["config_sha256"],
        "output_boundaries": {},
        "source_schema": 2,
        "source_trace_sha256": source_sha256,
        "source_phase_work_items": invocation.work_items,
        "window_index": 0,
        "warmup_start": row_first,
        "measure_start": measure_start,
        "measure_stop": row_stop,
        "measure_start_item": measure_start - row_first,
        "fixed_event_records": fixed_record[1],
        "outputs": {},
        "sparse_capture": {
            "plan_path": str(plan_path),
            "plan_sha256": capture.plan_sha256,
            "capture_path": str(capture_path),
            "capture_sha256": capture.capture_sha256,
            "request_count": capture.request_count,
            "ordinal": ordinal,
        },
    }
    dynamic_meta = {
        **common,
        "phases": [{
            "id": invocation.phase, "name": "cg_spmv",
            "work_items": row_stop - row_first,
        }],
        "trace_path": "trace.bin",
        "trace_sha256": dynamic_record[0],
        "trace_record_bytes": canonical.TRACE_STRUCT.size,
        "trace_records": dynamic_record[1],
        "initial_memory": initial_memory,
    }
    fixed_meta = {
        **common,
        "fixed_component": True,
        "phases": [{
            "id": invocation.phase, "name": "cg_spmv.fixed",
            "work_items": invocation.work_items,
        }],
        "measure_start_item": 0,
        "trace_path": "trace.bin",
        "trace_sha256": fixed_record[0],
        "trace_record_bytes": canonical.TRACE_STRUCT.size,
        "trace_records": fixed_record[1],
        "initial_memory": fixed_initial_memory,
    }
    evidence_contract.atomic_write_json(outdir / "trace.meta.json", dynamic_meta)
    evidence_contract.atomic_write_json(fixed_root / "trace.meta.json", fixed_meta)
    canonical.read_bundle(outdir)
    canonical.read_bundle(fixed_root)
    retained = sum(
        path.stat().st_size for path in (
            plan_path, capture_path, outdir / "trace.bin",
            fixed_root / "trace.bin",
        )
    )
    try:
        contract.require_storage_budget(
            retained_bytes=retained, temporary_bytes=0
        )
    except contract.IndexedWindowError as error:
        raise IndexError(str(error)) from error
    materialized = {
        "schema": 2,
        "status": "materialized",
        "workload": "npb_cg",
        "kernel": invocation.kernel,
        "ordinal": ordinal,
        "row_first": row_first,
        "measure_start": measure_start,
        "row_stop": row_stop,
        "dynamic_records": dynamic_record[1],
        "fixed_records": fixed_record[1],
        "retained_bytes": retained,
        "source_trace_sha256": source_sha256,
        "plan_sha256": capture.plan_sha256,
        "capture_sha256": capture.capture_sha256,
        "dynamic_trace_sha256": dynamic_record[0],
        "fixed_trace_sha256": fixed_record[0],
    }
    evidence_contract.atomic_write_json(
        outdir / "materialized-window.v2.json", materialized
    )
    return materialized


def _sha256_file(path):
    path = Path(path)
    if not path.is_file():
        raise IndexError(f"NPB index identity file is missing: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise IndexError(f"cannot hash NPB index identity: {error}") from error
    return digest.hexdigest()


def _identity_hash(bundle, name):
    value = bundle.meta.get(name)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IndexError(f"NPB index {name} is invalid")
    return value


def _generator_sha256():
    components = {
        "index": _sha256_file(Path(__file__)),
        "cardinality": _sha256_file(Path(npb.__file__)),
    }
    return hashlib.sha256(
        evidence_contract.canonical_json(components)
    ).hexdigest()


def build_index(bundle):
    """Build and validate an exact invocation prefix index."""

    if not isinstance(bundle, lazy.LazyBundle):
        raise IndexError("NPB lazy bundle type differs")
    descriptor = bundle.root / "trace.v2.json"
    cursor = 0
    segments = []
    for invocation in bundle.invocations:
        try:
            count = npb.primitive_count(invocation)
        except lazy.LazyTraceError as error:
            raise IndexError(
                f"NPB invocation {invocation.ordinal} count is invalid: {error}"
            ) from error
        if count <= 0 or cursor > _UINT64_MAX - count:
            raise IndexError("NPB primitive prefix count overflows uint64")
        end = cursor + count
        segments.append(contract.IndexSegment(
            primitive_begin=cursor,
            primitive_end=end,
            ordinal=invocation.ordinal,
            phase=invocation.phase,
            iteration=invocation.iteration,
            kernel=invocation.kernel,
            work_items=invocation.work_items,
        ))
        cursor = end
    declared = bundle.dynamic_work.get("primitive_records")
    if (
        not isinstance(declared, int)
        or isinstance(declared, bool)
        or declared < 0
        or declared > _UINT64_MAX
    ):
        raise IndexError("NPB dynamic primitive count is invalid")
    if cursor != declared:
        raise IndexError(
            "NPB terminal primitive count differs from dynamic work: "
            f"indexed={cursor}, declared={declared}"
        )
    index = contract.LazyIndex(
        schema=1,
        workload=bundle.meta.get("workload"),
        descriptor_sha256=_sha256_file(descriptor),
        input_sha256=_identity_hash(bundle, "input_sha256"),
        source_sha256=_identity_hash(bundle, "source_sha256"),
        binary_sha256=_identity_hash(bundle, "binary_sha256"),
        config_sha256=_identity_hash(bundle, "config_sha256"),
        generator_sha256=_generator_sha256(),
        primitive_records=cursor,
        segments=tuple(segments),
    )
    try:
        return contract.validate_lazy_index(index)
    except contract.IndexedWindowError as error:
        raise IndexError(f"NPB lazy index is invalid: {error}") from error


def locate(index, primitive_offset):
    """Locate one global primitive by binary search over invocation starts."""

    try:
        contract.validate_lazy_index(index)
    except contract.IndexedWindowError as error:
        raise IndexError(f"NPB lazy index is invalid: {error}") from error
    if (
        not isinstance(primitive_offset, int)
        or isinstance(primitive_offset, bool)
        or primitive_offset < 0
        or primitive_offset >= index.primitive_records
    ):
        raise IndexError("NPB primitive offset is outside the lazy index")
    starts = tuple(segment.primitive_begin for segment in index.segments)
    segment_index = bisect_right(starts, primitive_offset) - 1
    if segment_index < 0:
        raise IndexError("NPB primitive offset is outside the lazy index")
    segment = index.segments[segment_index]
    return LocatedPrimitive(
        segment=segment,
        local_offset=primitive_offset - segment.primitive_begin,
    )


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Materialize one native-state NPB CG SpMV window"
    )
    parser.add_argument("--trace-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--ordinal", type=int, default=3)
    parser.add_argument("--row-first", type=int, required=True)
    parser.add_argument("--measure-start", type=int, required=True)
    parser.add_argument("--row-stop", type=int, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = _parse_args(argv)
    try:
        # `python -m` initially loads this file as __main__.  Bind the package
        # name before importing the builder so its strict dataclass checks see
        # this same module rather than a second copy of every plan type.
        sys.modules.setdefault("scripts.npb_indexed_windows", sys.modules[__name__])
        from scripts import build_matched_breadth_workloads as builder
        bundle = lazy.read_bundle(options.trace_root)
        plan = read_sparse_capture_plan(options.plan)
        capture = builder.parse_npb_sparse_capture(
            options.capture, plan, plan_path=options.plan
        )
        result = materialize_cg_spmv_window(
            bundle, plan, capture, ordinal=options.ordinal,
            row_first=options.row_first,
            measure_start=options.measure_start,
            row_stop=options.row_stop,
            array_ids={"rowstr": 1, "colidx": 2, "a": 3, "p": 4, "q": 5},
            outdir=options.outdir, plan_path=options.plan,
            capture_path=options.capture,
        )
    except (IndexError, lazy.LazyTraceError, OSError) as error:
        print(f"NPB_CG_WINDOW_MATERIALIZATION_FAILED error={error}")
        return 1
    print(
        "NPB_CG_WINDOW_MATERIALIZATION_PASS "
        f"records={result['dynamic_records']} retained={result['retained_bytes']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
