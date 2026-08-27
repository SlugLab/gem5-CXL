#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate reproducible formal inputs from official Spatter traces."""

import dataclasses
import hashlib
import json
from pathlib import Path


U64_MAX = (1 << 64) - 1
SUPPORTED_KERNELS = ("Gather", "Scatter")


class GenerationError(RuntimeError):
    """A source trace or generated artifact violates the formal contract."""


@dataclasses.dataclass(frozen=True)
class TraceRecord:
    kernel: str
    count: int
    delta: int
    pattern: tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class RecordLayout:
    record: TraceRecord
    base: int
    span: int


@dataclasses.dataclass(frozen=True)
class TraceLayout:
    records: tuple[RecordLayout, ...]
    index_count: int
    index_span: int


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _integer(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise GenerationError(f"{label} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise GenerationError(f"{label} must be {qualifier}")
    return value


def _parse_record(value, position):
    label = f"record {position}"
    if not isinstance(value, dict):
        raise GenerationError(f"{label} must be an object")
    kernel = value.get("kernel")
    if not isinstance(kernel, str) or kernel not in SUPPORTED_KERNELS:
        raise GenerationError(f"{label} kernel is unsupported")
    count = _integer(value.get("count"), f"{label} count", positive=True)
    delta = _integer(value.get("delta"), f"{label} delta")
    raw_pattern = value.get("pattern")
    if not isinstance(raw_pattern, list) or not raw_pattern:
        raise GenerationError(f"{label} pattern must be a nonempty list")
    pattern = tuple(
        _integer(item, f"{label} pattern entry") for item in raw_pattern
    )
    maximum = (count - 1) * delta + max(pattern)
    if maximum > U64_MAX:
        raise GenerationError(f"{label} index exceeds unsigned 64-bit range")
    return TraceRecord(kernel, count, delta, pattern)


def load_records(path, expected_sha256, selected_kernel):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise GenerationError("source trace must be a resolved regular file")
    if _sha256_file(path) != expected_sha256:
        raise GenerationError("source trace SHA-256 differs")
    if selected_kernel not in SUPPORTED_KERNELS:
        raise GenerationError("selected kernel is unsupported")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"source trace JSON is invalid: {error}") from error
    if not isinstance(value, list):
        raise GenerationError("source trace must be a JSON list")
    parsed = tuple(_parse_record(row, index) for index, row in enumerate(value))
    selected = tuple(row for row in parsed if row.kernel == selected_kernel)
    if not selected:
        raise GenerationError("source trace selection is empty")
    return selected


def layout(records):
    records = tuple(records)
    if not records or any(not isinstance(row, TraceRecord) for row in records):
        raise GenerationError("trace layout records are invalid")
    positioned = []
    base = 0
    index_count = 0
    for row in records:
        span = (row.count - 1) * row.delta + max(row.pattern) + 1
        if base + span - 1 > U64_MAX:
            raise GenerationError("trace layout exceeds unsigned 64-bit range")
        positioned.append(RecordLayout(row, base, span))
        base += span
        index_count += row.count * len(row.pattern)
    return TraceLayout(tuple(positioned), index_count, base)


def indices(trace_layout, *, epochs):
    if not isinstance(trace_layout, TraceLayout):
        raise GenerationError("trace layout is invalid")
    epochs = _integer(epochs, "epoch count", positive=True)
    if trace_layout.index_span * epochs - 1 > U64_MAX:
        raise GenerationError("epoch layout exceeds unsigned 64-bit range")
    for epoch in range(epochs):
        epoch_base = epoch * trace_layout.index_span
        for positioned in trace_layout.records:
            record = positioned.record
            record_base = epoch_base + positioned.base
            for iteration in range(record.count):
                iteration_base = record_base + iteration * record.delta
                for offset in record.pattern:
                    yield iteration_base + offset


def resident_bytes(trace_layout, epochs, mode):
    if not isinstance(trace_layout, TraceLayout):
        raise GenerationError("trace layout is invalid")
    epochs = _integer(epochs, "epoch count", positive=True)
    count = trace_layout.index_count * epochs
    span = trace_layout.index_span * epochs
    if mode == "gather":
        return 4 * span + 8 * count + 4 * count
    if mode == "scatter":
        return 4 * count + 8 * count + 4 * span
    raise GenerationError("mode must be gather or scatter")


def required_epochs(trace_layout, mode, minimum_bytes):
    minimum_bytes = _integer(minimum_bytes, "minimum bytes", positive=True)
    one_epoch = resident_bytes(trace_layout, 1, mode)
    epochs = (minimum_bytes + one_epoch - 1) // one_epoch
    if resident_bytes(trace_layout, epochs, mode) < minimum_bytes:
        raise GenerationError("computed epoch count is below minimum bytes")
    return epochs


def value_bits(position):
    position = _integer(position, "value position")
    return 0x3F000000 | ((position * 0x9E3779B1) & 0x007FFFFF)
