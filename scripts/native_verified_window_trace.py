#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Bounded native-verified trace adapters for formal breadth workloads."""

import hashlib
import mmap
import re
import struct
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import run_matched_breadth_gem5 as replay
except ImportError:
    import canonical_work_trace as canonical
    import run_matched_breadth_gem5 as replay


SOURCE = Path(__file__).resolve()
INDEX_BASE = 0x100000000
VALUE_BASE = 0x200000000
DESTINATION_BASE = 0x300000000
_SHA256 = re.compile(r"[0-9a-f]{64}")


class WindowTraceError(RuntimeError):
    """A selected native-verified window violates its frozen input contract."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _operation(phase, opcode, work_item, sequence, *, address=0,
               operand0=0, operand1=0, result=0):
    return canonical.Operation(
        phase=phase,
        opcode=opcode,
        work_item=work_item,
        sequence=sequence,
        address=address,
        operand0=operand0,
        operand1=operand1,
        result=result,
    )


def _meta(workload, phase, work_items, input_sha256,
          source_trace_sha256):
    source_sha256 = _sha256_file(SOURCE)
    return {
        "schema": 1,
        "workload": workload,
        "input_sha256": input_sha256,
        "source_sha256": source_sha256,
        "binary_sha256": source_sha256,
        "config_sha256": source_trace_sha256,
        "phases": [{
            "id": phase,
            "name": workload,
            "work_items": work_items,
        }],
        "output_boundaries": {},
    }


def _validate_coordinate(count, warmup_start, measure_start, measure_stop):
    values = (count, warmup_start, measure_start, measure_stop)
    if any(not isinstance(value, int) or isinstance(value, bool)
           for value in values) or not (
        count > 0
        and 0 <= warmup_start <= measure_start < measure_stop <= count
    ):
        raise WindowTraceError("spatter timing coordinate is invalid")


def materialize_spatter_window(
    *, kind, values_path, index_path, source_trace_sha256, input_sha256,
    warmup_start, measure_start, measure_stop, outdir, window_index=0,
):
    """Materialize one exact Spatter window without a full-workload trace."""

    if kind not in {"gather", "scatter"}:
        raise WindowTraceError("spatter kind is invalid")
    for label, digest in (
        ("source trace", source_trace_sha256), ("input", input_sha256)
    ):
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise WindowTraceError(f"spatter {label} SHA-256 is invalid")
    values_path = Path(values_path).resolve()
    index_path = Path(index_path).resolve()
    if not values_path.is_file() or values_path.stat().st_size % 4:
        raise WindowTraceError("spatter values file is invalid")
    if not index_path.is_file() or index_path.stat().st_size % 8:
        raise WindowTraceError("spatter index file is invalid")
    value_count = values_path.stat().st_size // 4
    count = index_path.stat().st_size // 8
    if kind == "scatter" and value_count != count:
        raise WindowTraceError("scatter values/index length differs")
    _validate_coordinate(
        count, warmup_start, measure_start, measure_stop
    )
    if (
        not isinstance(window_index, int)
        or isinstance(window_index, bool)
        or window_index < 0
    ):
        raise WindowTraceError("spatter window index is invalid")
    outdir = Path(outdir).resolve()
    fixed_root = outdir.with_name(outdir.name + ".fixed")
    if outdir.exists() or fixed_root.exists():
        raise WindowTraceError("fresh spatter window roots are required")
    phase = 3 if kind == "gather" else 4
    workload = "amg_gather" if kind == "gather" else "lulesh_scatter"

    operations = []
    with (
        values_path.open("rb") as values_stream,
        index_path.open("rb") as index_stream,
        mmap.mmap(values_stream.fileno(), 0, access=mmap.ACCESS_READ) as values,
        mmap.mmap(index_stream.fileno(), 0, access=mmap.ACCESS_READ) as indices,
    ):
        for local, item in enumerate(range(warmup_start, measure_stop)):
            target = struct.unpack_from("<Q", indices, item * 8)[0]
            value_index = target if kind == "gather" else item
            if value_index >= value_count:
                raise WindowTraceError("spatter index is out of bounds")
            bits = struct.unpack_from("<I", values, value_index * 4)[0]
            sequence = len(operations)
            operations.append(_operation(
                phase, canonical.Opcode.LOAD_U64, local, sequence,
                address=INDEX_BASE + item * 8,
                operand0=target,
                result=target,
            ))
            operations.append(_operation(
                phase, canonical.Opcode.LOAD_F32, local, sequence + 1,
                address=VALUE_BASE + value_index * 4,
                operand0=bits,
                operand1=sequence + 1 if kind == "gather" else 0,
                result=bits,
            ))
            destination_index = item if kind == "gather" else target
            operations.append(_operation(
                phase, canonical.Opcode.STORE_F32, local, sequence + 2,
                address=DESTINATION_BASE + destination_index * 4,
                operand0=bits,
                result=bits,
            ))

    fixed_operations = (
        _operation(
            phase, canonical.Opcode.BARRIER, 0, 0,
            operand0=count, result=0,
        ),
        _operation(phase, canonical.Opcode.COMMIT, 0, 1),
    )
    canonical.write_bundle(
        fixed_root,
        {
            **_meta(
                workload, phase, count, input_sha256,
                source_trace_sha256,
            ),
            "fixed_component": True,
            "source_trace_sha256": source_trace_sha256,
        },
        fixed_operations,
        {},
        initial_memory={},
    )
    fixed_sha256 = canonical.read_bundle(
        fixed_root
    ).meta["trace_sha256"]
    warmup_items = measure_start - warmup_start
    measured_items = measure_stop - measure_start
    canonical.write_bundle(
        outdir,
        {
            **_meta(
                workload, phase, warmup_items + measured_items,
                input_sha256, source_trace_sha256,
            ),
            "prepared_window": {
                "source_schema": 3,
                "source_trace_sha256": source_trace_sha256,
                "phase": phase,
                "phase_name": workload,
                "warmup_items": warmup_items,
                "measured_items": measured_items,
                "measure_start_item": warmup_items,
                "fixed_event_records": len(fixed_operations),
                "fixed_trace_sha256": fixed_sha256,
                "window_index": window_index,
                "warmup_start": warmup_start,
                "measure_start": measure_start,
                "measure_stop": measure_stop,
            },
        },
        operations,
        {},
        initial_memory={},
    )
    return replay.load_prepared_window_trace(outdir, fixed_root)
