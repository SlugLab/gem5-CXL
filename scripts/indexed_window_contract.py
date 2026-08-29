#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Strict immutable contracts for indexed lazy-window artifacts.

The helpers in this module deliberately reject coercion.  In particular,
JSON booleans are not accepted as integers and readers require an exact field
set so a producer cannot silently extend or weaken an evidence identity.
"""

import dataclasses
import hashlib
import re
from pathlib import Path

from scripts import cross_system_contract


STORAGE_LIMIT_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")


class IndexedWindowError(RuntimeError):
    """An indexed-window artifact violates its immutable contract."""


@dataclasses.dataclass(frozen=True)
class IndexSegment:
    primitive_begin: int
    primitive_end: int
    ordinal: int
    phase: int
    iteration: int
    kernel: str
    work_items: int


@dataclasses.dataclass(frozen=True)
class LazyIndex:
    schema: int
    workload: str
    descriptor_sha256: str
    input_sha256: str
    source_sha256: str
    binary_sha256: str
    config_sha256: str
    generator_sha256: str
    primitive_records: int
    segments: tuple[IndexSegment, ...]


@dataclasses.dataclass(frozen=True)
class RealizedWindow:
    schema: int
    workload: str
    phase: int
    level: int
    stratum: int
    window_index: int
    requested_warmup_start: int
    requested_measure_start: int
    requested_measure_stop: int
    realized_warmup_start: int
    realized_measure_start: int
    realized_measure_stop: int
    lazy_index_sha256: str
    segment_ordinals: tuple[int, ...]
    safe_cut_reasons: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class SparseStateRecord:
    array_name: str
    logical_index: int
    logical_address: int
    element_bits: int
    raw_word: int


@dataclasses.dataclass(frozen=True)
class RetainedPackage:
    schema: int
    workload: str
    path: str
    sha256: str
    retained_bytes: int
    record_count: int


def _require_exact_int(value, label, *, minimum=0):
    if not isinstance(value, int) or isinstance(value, bool):
        raise IndexedWindowError(f"{label} must be an integer")
    if value < minimum:
        raise IndexedWindowError(f"{label} must be at least {minimum}")
    return value


def _require_nonempty_string(value, label):
    if not isinstance(value, str) or not value:
        raise IndexedWindowError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IndexedWindowError(f"{label} SHA-256 is invalid")
    return value


def _require_exact_fields(record, expected, label):
    if not isinstance(record, dict):
        raise IndexedWindowError(f"{label} must be an object")
    actual = set(record)
    expected = set(expected)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise IndexedWindowError(
            f"{label} fields are invalid: missing={missing}, unknown={unknown}"
        )
    return record


def _validate_segment(segment, expected_ordinal):
    if not isinstance(segment, IndexSegment):
        raise IndexedWindowError("index segment has the wrong type")
    begin = _require_exact_int(
        segment.primitive_begin, "segment primitive_begin"
    )
    end = _require_exact_int(segment.primitive_end, "segment primitive_end")
    ordinal = _require_exact_int(segment.ordinal, "segment ordinal")
    _require_exact_int(segment.phase, "segment phase")
    _require_exact_int(segment.iteration, "segment iteration")
    _require_exact_int(segment.work_items, "segment work_items")
    _require_nonempty_string(segment.kernel, "segment kernel")
    if end <= begin:
        raise IndexedWindowError("segment coverage must be non-empty")
    if ordinal != expected_ordinal:
        raise IndexedWindowError(
            "segment ordinal sequence must be contiguous from zero"
        )
    return begin, end


def validate_lazy_index(index):
    """Validate exact identity and contiguous primitive prefix coverage."""

    if not isinstance(index, LazyIndex):
        raise IndexedWindowError("lazy index has the wrong type")
    schema = _require_exact_int(index.schema, "index schema")
    if schema != 1:
        raise IndexedWindowError("lazy index schema must be 1")
    _require_nonempty_string(index.workload, "index workload")
    for field in dataclasses.fields(index):
        if field.name.endswith("_sha256"):
            _require_sha256(
                getattr(index, field.name), field.name.removesuffix("_sha256")
            )
    primitive_records = _require_exact_int(
        index.primitive_records, "primitive_records"
    )
    if not isinstance(index.segments, tuple):
        raise IndexedWindowError("index segments must be a tuple")

    expected_begin = 0
    for expected_ordinal, segment in enumerate(index.segments):
        begin, end = _validate_segment(segment, expected_ordinal)
        if begin != expected_begin:
            raise IndexedWindowError(
                "segment coverage has a gap or overlap"
            )
        expected_begin = end
    if expected_begin != primitive_records:
        raise IndexedWindowError(
            "segment coverage does not match primitive_records"
        )
    return index


def _segment_from_record(record):
    names = tuple(field.name for field in dataclasses.fields(IndexSegment))
    _require_exact_fields(record, names, "index segment")
    return IndexSegment(**record)


def write_lazy_index(path, index):
    """Atomically publish a validated lazy index."""

    validate_lazy_index(index)
    return cross_system_contract.atomic_write_json(
        path, dataclasses.asdict(index)
    )


def read_lazy_index(path):
    """Read and strictly validate a lazy index JSON record."""

    try:
        record = cross_system_contract.load_json(path)
    except cross_system_contract.ContractError as error:
        raise IndexedWindowError(str(error)) from error
    names = tuple(field.name for field in dataclasses.fields(LazyIndex))
    _require_exact_fields(record, names, "lazy index")
    raw_segments = record["segments"]
    if not isinstance(raw_segments, list):
        raise IndexedWindowError("index segments must be an array")
    try:
        index = LazyIndex(
            **{
                **record,
                "segments": tuple(
                    _segment_from_record(segment)
                    for segment in raw_segments
                ),
            }
        )
    except TypeError as error:
        raise IndexedWindowError(f"invalid lazy index: {error}") from error
    return validate_lazy_index(index)


def validate_retained_package(package):
    """Validate a retained package and the file bound by its metadata."""

    if not isinstance(package, RetainedPackage):
        raise IndexedWindowError("retained package has the wrong type")
    if _require_exact_int(package.schema, "retained package schema") != 1:
        raise IndexedWindowError("retained package schema must be 1")
    _require_nonempty_string(package.workload, "retained package workload")
    path = Path(_require_nonempty_string(package.path, "retained package path"))
    _require_sha256(package.sha256, "retained package")
    retained_bytes = _require_exact_int(
        package.retained_bytes, "retained package bytes"
    )
    _require_exact_int(package.record_count, "retained package record_count")
    if not path.is_file():
        raise IndexedWindowError(f"retained package file is missing: {path}")
    if path.stat().st_size != retained_bytes:
        raise IndexedWindowError("retained package size does not match")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise IndexedWindowError(
            f"retained package file cannot be read: {path}: {error}"
        ) from error
    if digest.hexdigest() != package.sha256:
        raise IndexedWindowError("retained package SHA-256 does not match")
    return package


def require_storage_budget(*, retained_bytes, temporary_bytes):
    """Require retained plus peak temporary storage to fit the hard limit."""

    for value, label in (
        (retained_bytes, "retained_bytes"),
        (temporary_bytes, "temporary_bytes"),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise IndexedWindowError(
                f"{label} must be a non-negative integer"
            )
    total = retained_bytes + temporary_bytes
    if total > STORAGE_LIMIT_BYTES:
        raise IndexedWindowError(
            "retained plus temporary storage exceeds 512 MiB"
        )
    return total
