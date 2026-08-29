#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Select formal MCF call windows in one authenticated EVENTS scan."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import os
import shutil
import tempfile
from bisect import bisect_right
from pathlib import Path

from scripts import cross_system_contract as evidence_contract
from scripts import indexed_window_contract as window_contract
from scripts import mcfreg2
from scripts import stratified_timing


_PLAN_TO_EVENT_PHASE = {
    "pricing_kernel": "pricing",
    "price_out_impl": "price_out",
}


class SelectionError(RuntimeError):
    """An MCF selection or its immutable publication is invalid."""


@dataclasses.dataclass(frozen=True)
class SelectedPackage:
    root: Path
    source_event_count: int
    retained_event_count: int
    package_sha256: str
    index_sha256: str


@dataclasses.dataclass(frozen=True)
class MaterializedSelection:
    phase: str
    window_index: int
    stratum: int
    warmup_start: int
    measure_start: int
    measure_stop: int
    event_phase: str
    call_begin: int
    call_end: int


def _exact_nonnegative(value, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise SelectionError(f"selected MCF {label} is invalid")
    return value


def _plan_record(plans):
    return {
        phase: dataclasses.asdict(plans[phase])
        for phase in sorted(plans)
    }


def _validate_plans(plans):
    if not isinstance(plans, dict) or not plans:
        raise SelectionError("selected MCF plans are missing")
    unknown = set(plans) - set(_PLAN_TO_EVENT_PHASE)
    if unknown:
        raise SelectionError(
            f"selected MCF plan phase is invalid: {sorted(unknown)[0]}"
        )

    coordinates = []
    intervals = {phase: [] for phase in _PLAN_TO_EVENT_PHASE.values()}
    seen = set()
    for phase in sorted(plans):
        plan = plans[phase]
        if (
            not isinstance(plan, stratified_timing.SamplingPlan)
            or plan.phase != phase
        ):
            raise SelectionError(
                f"selected MCF plan phase differs for {phase}"
            )
        work_items = _exact_nonnegative(
            plan.work_items, f"{phase} work_items"
        )
        if work_items == 0 or not plan.windows:
            raise SelectionError(
                f"selected MCF phase {phase} has no work"
            )
        event_phase = _PLAN_TO_EVENT_PHASE[phase]
        for window_index, window in enumerate(plan.windows):
            if not isinstance(window, stratified_timing.TimingWindow):
                raise SelectionError("selected MCF window type differs")
            stratum = _exact_nonnegative(window.stratum, "stratum")
            warmup_start = _exact_nonnegative(
                window.warmup_start, "warmup_start"
            )
            measure_start = _exact_nonnegative(
                window.measure_start, "measure_start"
            )
            measure_stop = _exact_nonnegative(
                window.measure_stop, "measure_stop"
            )
            if not (
                warmup_start <= measure_start < measure_stop <= work_items
            ):
                raise SelectionError(
                    "selected MCF window call boundaries are invalid"
                )
            if plan.full_phase:
                if (
                    stratum != 0
                    or warmup_start != 0
                    or measure_start != 0
                    or measure_stop != work_items
                ):
                    raise SelectionError(
                        "selected MCF full-phase coordinate differs"
                    )
            else:
                if stratum >= 64:
                    raise SelectionError(
                        "selected MCF window stratum is invalid"
                    )
                stratum_begin = stratum * work_items // 64
                stratum_end = (stratum + 1) * work_items // 64
                if (
                    warmup_start < stratum_begin
                    or measure_stop > stratum_end
                ):
                    raise SelectionError(
                        "selected MCF window crosses its stratum"
                    )
            identity = (
                phase,
                stratum,
                warmup_start,
                measure_start,
                measure_stop,
            )
            if identity in seen:
                raise SelectionError(
                    "selected MCF coordinate is duplicated"
                )
            seen.add(identity)
            coordinates.append({
                "phase": phase,
                "window_index": window_index,
                "stratum": stratum,
                "warmup_start": warmup_start,
                "measure_start": measure_start,
                "measure_stop": measure_stop,
                "event_phase": event_phase,
                "call_begin": warmup_start,
                "call_end": measure_stop,
            })
            intervals[event_phase].append((warmup_start, measure_stop))

    merged = {}
    for phase, values in intervals.items():
        result = []
        for begin, end in sorted(values):
            if result and begin <= result[-1][1]:
                result[-1] = (result[-1][0], max(result[-1][1], end))
            else:
                result.append((begin, end))
        merged[phase] = tuple(result)
    return tuple(coordinates), merged


def _contains(intervals, starts, call):
    if not intervals:
        return False
    index = bisect_right(starts, call) - 1
    return index >= 0 and call < intervals[index][1]


def _canonical_line(row):
    return evidence_contract.canonical_json(row) + b"\n"


def _write_gzip_member(stream, payload):
    begin = stream.tell()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=stream,
        mtime=0,
    ) as member:
        member.write(payload)
    return begin, stream.tell()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_budget(retained_bytes, storage_limit_bytes):
    limit = _exact_nonnegative(storage_limit_bytes, "storage budget")
    if limit > window_contract.STORAGE_LIMIT_BYTES:
        raise SelectionError(
            "selected MCF storage budget exceeds the 512 MiB contract"
        )
    try:
        window_contract.require_storage_budget(
            retained_bytes=retained_bytes, temporary_bytes=0
        )
    except window_contract.IndexedWindowError as error:
        raise SelectionError(
            f"selected MCF storage budget exceeded: {error}"
        ) from error
    if retained_bytes > limit:
        raise SelectionError(
            "selected MCF storage budget exceeded"
        )


def _publish_directory(temporary, output):
    directory = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    os.replace(temporary, output)
    parent = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _select_stream(package_path, intervals, package_output, limit):
    interval_starts = {
        phase: tuple(begin for begin, _ in values)
        for phase, values in intervals.items()
    }
    frames = []
    expected_call = {"pricing": 0, "price_out": 0}
    expected_order = 0
    active = None
    active_payload = None
    source_event_count = 0
    retained_event_count = 0

    with package_output.open("wb") as output:
        for streamed in mcfreg2.stream_events(package_path):
            source_event_count = streamed.ordinal + 1
            row = streamed.row
            kind = row.get("kind")
            if kind == "CALL_BEGIN":
                if active is not None:
                    raise SelectionError(
                        "selected MCF call begins inside an active call"
                    )
                phase = row.get("phase")
                if phase not in expected_call:
                    raise SelectionError("selected MCF call phase is invalid")
                call = row.get("call")
                ordinal = row.get("ordinal")
                order = row.get("order")
                for value, label in (
                    (call, "call"),
                    (ordinal, "ordinal"),
                    (order, "order"),
                ):
                    _exact_nonnegative(value, label)
                if (
                    call != ordinal
                    or call != expected_call[phase]
                    or order != expected_order
                ):
                    raise SelectionError(
                        "selected MCF call/order sequence differs"
                    )
                active = {
                    "phase": phase,
                    "call": call,
                    "order": order,
                    "source_event_begin": streamed.ordinal,
                    "selected": _contains(
                        intervals[phase], interval_starts[phase], call
                    ),
                }
                active_payload = (
                    bytearray(_canonical_line(row))
                    if active["selected"] else None
                )
                continue
            if active is None:
                raise SelectionError(
                    "selected MCF event occurs outside a call"
                )
            if row.get("call") != active["call"]:
                raise SelectionError(
                    "selected MCF event crosses a call boundary"
                )
            if active_payload is not None:
                active_payload.extend(_canonical_line(row))
            if kind != "CALL_END":
                continue
            if (
                row.get("phase") != active["phase"]
                or row.get("ordinal") != active["call"]
                or row.get("order") != active["order"]
            ):
                raise SelectionError(
                    "selected MCF call end phase/call differs"
                )
            if active_payload is not None:
                compressed_begin, compressed_end = _write_gzip_member(
                    output, active_payload
                )
                event_count = streamed.ordinal + 1 - active[
                    "source_event_begin"
                ]
                frames.append({
                    "phase": active["phase"],
                    "call": active["call"],
                    "source_event_begin": active["source_event_begin"],
                    "source_event_end": streamed.ordinal + 1,
                    "retained_event_begin": retained_event_count,
                    "retained_event_end": (
                        retained_event_count + event_count
                    ),
                    "compressed_begin": compressed_begin,
                    "compressed_end": compressed_end,
                })
                retained_event_count += event_count
                output.flush()
                _require_budget(output.tell(), limit)
            expected_call[active["phase"]] += 1
            expected_order += 1
            active = None
            active_payload = None
        if active is not None:
            raise SelectionError("selected MCF call is truncated")
        output.flush()
        os.fsync(output.fileno())
    return (
        tuple(frames),
        expected_call,
        source_event_count,
        retained_event_count,
    )


def select_windows(
    package_path,
    plans,
    outdir,
    *,
    storage_limit_bytes=window_contract.STORAGE_LIMIT_BYTES,
):
    """Create an immutable selected-call package from one EVENTS inflate."""

    package_path = Path(package_path).resolve()
    outdir = Path(outdir).resolve()
    if not package_path.is_file():
        raise SelectionError(f"selected MCF source is missing: {package_path}")
    if outdir.exists():
        raise SelectionError(
            f"selected MCF output already exists: {outdir}"
        )
    coordinates, intervals = _validate_plans(plans)
    package = mcfreg2.read_package(
        package_path, lazy_section_names=("EVENTS",)
    )
    expected_counts = {
        "pricing": package.header.pricing_calls,
        "price_out": package.header.price_out_calls,
    }
    for phase, plan in plans.items():
        event_phase = _PLAN_TO_EVENT_PHASE[phase]
        if plan.work_items != expected_counts[event_phase]:
            raise SelectionError(
                f"selected MCF {phase} call count differs from source"
            )

    outdir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{outdir.name}.", dir=outdir.parent
    ))
    try:
        selected_path = temporary / "windows.jsonl.gz"
        (
            frames,
            observed_counts,
            source_event_count,
            retained_event_count,
        ) = _select_stream(
            package_path, intervals, selected_path, storage_limit_bytes
        )
        if source_event_count != package.header.event_count:
            raise SelectionError(
                "selected MCF source event count differs"
            )
        for phase in plans:
            event_phase = _PLAN_TO_EVENT_PHASE[phase]
            if observed_counts[event_phase] != expected_counts[event_phase]:
                raise SelectionError(
                    f"selected MCF {phase} terminal call count differs"
                )
        package_sha256 = _sha256_file(selected_path)
        source_sha256 = _sha256_file(package_path)
        plan_value = _plan_record(plans)
        plan_sha256 = hashlib.sha256(
            evidence_contract.canonical_json(plan_value)
        ).hexdigest()
        generator_sha256 = _sha256_file(Path(__file__))
        index_value = {
            "schema": 1,
            "source_path": str(package_path),
            "source_sha256": source_sha256,
            "source_event_count": source_event_count,
            "retained_event_count": retained_event_count,
            "package_file": selected_path.name,
            "package_sha256": package_sha256,
            "plan_sha256": plan_sha256,
            "generator_sha256": generator_sha256,
            "coordinates": list(coordinates),
            "frames": list(frames),
        }
        index_path = evidence_contract.atomic_write_json(
            temporary / "index.json", index_value
        )
        index_sha256 = _sha256_file(index_path)
        retained_bytes = selected_path.stat().st_size + index_path.stat().st_size
        _require_budget(retained_bytes, storage_limit_bytes)
        _publish_directory(temporary, outdir)
        return SelectedPackage(
            outdir,
            source_event_count,
            retained_event_count,
            package_sha256,
            index_sha256,
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def read_coordinate(root, phase, window_index):
    """Read exactly one selected coordinate from a published index."""

    try:
        value = evidence_contract.load_json(Path(root) / "index.json")
    except evidence_contract.ContractError as error:
        raise SelectionError(str(error)) from error
    coordinates = value.get("coordinates") if isinstance(value, dict) else None
    if not isinstance(coordinates, list):
        raise SelectionError("selected MCF coordinate index is invalid")
    matches = [
        row for row in coordinates
        if isinstance(row, dict)
        and row.get("phase") == phase
        and row.get("window_index") == window_index
    ]
    if len(matches) != 1:
        raise SelectionError(
            "selected MCF coordinate is absent or duplicate"
        )
    fields = {field.name for field in dataclasses.fields(MaterializedSelection)}
    if set(matches[0]) != fields:
        raise SelectionError("selected MCF coordinate fields differ")
    try:
        return MaterializedSelection(**matches[0])
    except TypeError as error:
        raise SelectionError(
            f"selected MCF coordinate is invalid: {error}"
        ) from error
