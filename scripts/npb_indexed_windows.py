#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Exact prefix indexes for formal NPB lazy invocation streams."""

from __future__ import annotations

import dataclasses
import hashlib
from bisect import bisect_right
from pathlib import Path

from scripts import cross_system_contract as evidence_contract
from scripts import indexed_window_contract as contract
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb


_UINT64_MAX = (1 << 64) - 1


class IndexError(RuntimeError):
    """An NPB prefix index or seek coordinate is invalid."""


@dataclasses.dataclass(frozen=True)
class LocatedPrimitive:
    segment: contract.IndexSegment
    local_offset: int


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
