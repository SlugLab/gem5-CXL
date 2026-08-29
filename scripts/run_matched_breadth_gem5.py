#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build and run the common Vanilla/AMU/CIRA canonical-trace replay."""

import hashlib
import argparse
import configparser
import dataclasses
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import compare_gapbs_cxl_amu_cira as comparison
    from scripts import cross_system_contract as contract
    from scripts import cxl_latency_spectrum as latency
    from scripts import lazy_work_trace as lazy
    from scripts import lazy_workload_registry as lazy_registry
    from scripts import stratified_timing as timing
except ImportError:
    import canonical_work_trace as canonical
    import compare_gapbs_cxl_amu_cira as comparison
    import cross_system_contract as contract
    import cxl_latency_spectrum as latency
    import lazy_work_trace as lazy
    import lazy_workload_registry as lazy_registry
    import stratified_timing as timing


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "util/amu/matched_workloads/trace_replay.cc"
M5_LIBRARY = REPO / "util/m5/build/x86/out/libm5.a"
SYSTEMS = ("vanilla", "amu", "cira")
_CORE_SECTION = re.compile(r"^board\.processor\.cores([0-9]+)\.core$")
_START_CORE_SECTION = re.compile(r"^board\.processor\.start([0-9]+)\.core$")
_SWITCH_CORE_SECTION = re.compile(r"^board\.processor\.switch([0-9]+)\.core$")
_ALLOCATION = re.compile(
    r"^TRACE_REPLAY_ALLOCATION logical_bytes=([0-9]+) "
    r"allocated_bytes=([0-9]+) all_memory_cxl=(true|false)$"
)
_STREAM_MAGIC = b"MTRCV2\0\0"
_STREAM_HEADER = struct.Struct("<8sQQ")
_STREAM_CHUNK_RECORDS = 4096
_BOUNDARY_MAGIC = "MTRBND2"
_INITIAL_MEMORY_IMAGE_MAGIC = "MTRINI1"
_INITIAL_MEMORY_SPARSE_MAGIC = "MTRINI2"


class ReplayError(RuntimeError):
    """A replay command or its causal mechanism evidence is invalid."""


@dataclasses.dataclass(frozen=True)
class MaterializedTrace:
    root: Path
    fixed_root: Path
    source_schema: int
    source_trace_sha256: str
    phase: int
    phase_name: str
    window_index: int
    warmup_items: int
    measured_items: int
    measure_start_item: int
    fixed_event_records: int


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def trace_identity_sha256(trace):
    """Return the frozen payload identity used to derive timing windows."""
    trace = Path(trace).resolve()
    eager = trace / "trace.meta.json"
    descriptor = trace / "trace.v2.json"
    if eager.is_file() and not descriptor.exists():
        return canonical.read_bundle(trace).meta["trace_sha256"]
    if descriptor.is_file() and not eager.exists():
        lazy.read_bundle(trace)
        return _sha256_file(descriptor)
    raise ReplayError("trace root must contain exactly one canonical schema")


def _eager_phase_identity(bundle, phase):
    phases = bundle.meta.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ReplayError("eager trace phase metadata is invalid")
    if all(isinstance(row, dict) for row in phases):
        matches = [row for row in phases if row.get("id") == phase]
        if len(matches) != 1:
            raise ReplayError("selected eager phase is absent")
        row = matches[0]
        name = row.get("name")
        count = row.get("work_items")
    elif all(isinstance(row, str) and row for row in phases):
        phase_ids = sorted({operation.phase for operation in bundle.operations})
        if phase not in phase_ids or len(phase_ids) != len(phases):
            raise ReplayError("eager phase names do not match phase IDs")
        name = phases[phase_ids.index(phase)]
        count = bundle.meta.get("phase_work", {}).get(name)
    else:
        raise ReplayError("eager trace phase metadata is invalid")
    if not isinstance(name, str) or not name:
        raise ReplayError("eager trace phase name is invalid")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ReplayError("eager trace phase work count is invalid")
    return name, count


def _lazy_phase_identity(bundle, phase):
    try:
        name = lazy_registry.phase_name(bundle, phase)
    except lazy.LazyTraceError as error:
        raise ReplayError(str(error)) from error
    count = sum(
        invocation.work_items
        for invocation in bundle.invocations
        if invocation.phase == phase
    )
    if count <= 0:
        raise ReplayError("selected lazy phase is absent")
    return name, count


def _window_coordinates(manifest, *, trace_sha256, phase_name, work_items,
                        window_index):
    try:
        plan = timing.read_plan(manifest)
    except timing.TimingError as error:
        raise ReplayError(f"invalid timing-window manifest: {error}") from error
    if plan.trace_sha256 != trace_sha256:
        raise ReplayError("timing-window trace SHA-256 differs")
    if plan.phase != phase_name:
        raise ReplayError("timing-window phase identity differs")
    if plan.work_items != work_items:
        raise ReplayError("timing-window phase work count differs")
    if (
        isinstance(window_index, bool)
        or not isinstance(window_index, int)
        or window_index < 0
        or window_index >= len(plan.windows)
    ):
        raise ReplayError("timing-window index is outside the canonical plan")
    return plan.windows[window_index]


def _partition_bc_lazy_window(source, phase, window, state):
    """Yield one BC vertex window after only the causally required prefix."""

    if source.meta.get("workload") != "gap_bc":
        raise ReplayError("bounded GAP BC partition received another workload")
    phase_invocations = tuple(
        invocation for invocation in source.invocations
        if invocation.phase == phase
    )
    if not phase_invocations or any(
        invocation.kernel not in {
            "gap_bc_bfs_level", "gap_bc_reverse_level"
        }
        for invocation in phase_invocations
    ):
        raise ReplayError("bounded GAP BC phase is not depth compact")
    phase_items = sum(row.work_items for row in phase_invocations)
    if not (
        0 <= window.warmup_start < window.measure_start
        < window.measure_stop <= phase_items
    ):
        raise ReplayError("bounded GAP BC window coordinates are invalid")
    dynamic_initial = {}
    fixed_initial = {}
    expanded = 0
    phase_base = 0
    first_phase_ordinal = phase_invocations[0].ordinal

    def consume(mapped, invocation, operations, *, selected, base=0):
        nonlocal expanded
        for operation in operations:
            lazy._validate_expanded_operation(source, invocation, operation)
            operation = dataclasses.replace(operation, sequence=expanded)
            expanded += 1
            if selected:
                global_item = phase_base + operation.work_item
                _remember_initial(dynamic_initial, operation)
                yield False, dataclasses.replace(
                    operation,
                    work_item=global_item - window.warmup_start,
                )

    with lazy.MappedState(source) as mapped:
        for invocation in source.invocations[:first_phase_ordinal]:
            lazy_registry.fast_forward(mapped, invocation)
        for invocation in phase_invocations:
            invocation_start = phase_base
            invocation_stop = phase_base + invocation.work_items
            if invocation_stop <= window.warmup_start:
                lazy_registry.fast_forward(mapped, invocation)
            elif invocation_start < window.measure_stop:
                local_start = max(
                    0, window.warmup_start - invocation_start
                )
                local_stop = min(
                    invocation.work_items,
                    window.measure_stop - invocation_start,
                )
                if local_start:
                    lazy_registry.fast_forward(
                        mapped, invocation, 0, local_start
                    )
                operations = lazy_registry.expand_slice(
                    mapped, invocation, local_start, local_stop, 1024,
                    include_controls=False,
                )
                yield from consume(
                    mapped, invocation, operations, selected=True
                )
            phase_base = invocation_stop
            if phase_base >= window.measure_stop:
                break

        for invocation in phase_invocations:
            for operation in lazy_registry.fixed_controls(invocation):
                lazy._validate_expanded_operation(source, invocation, operation)
                operation = dataclasses.replace(operation, sequence=expanded)
                expanded += 1
                state["fixed"] += 1
                _remember_initial(fixed_initial, operation)
                yield True, operation
    state["expanded"] = expanded
    state["phase_items"] = phase_items
    state["dynamic_initial"] = dynamic_initial
    state["fixed_initial"] = fixed_initial
    state["expansion_mode"] = "bounded-gap-bc"


def _write_segment_payload(root, operations, *, label="selected timing window"):
    root = Path(root).resolve()
    if root.exists():
        raise ReplayError(f"fresh materialized trace root required: {root}")
    root.mkdir(parents=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".trace.bin.", dir=root
    )
    digest = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for operation in operations:
                if not isinstance(operation, canonical.Operation):
                    raise ReplayError("materializer emitted a non-operation")
                sequenced = dataclasses.replace(operation, sequence=count)
                payload = canonical.TRACE_STRUCT.pack(
                    sequenced.phase, int(sequenced.opcode), 0,
                    sequenced.work_item, sequenced.sequence,
                    sequenced.address, sequenced.operand0,
                    sequenced.operand1, sequenced.result,
                )
                stream.write(payload)
                digest.update(payload)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, root / "trace.bin")
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    if count == 0:
        raise ReplayError(f"{label} has no canonical operations")
    return digest.hexdigest(), count


def _write_partitioned_payload(dynamic_root, fixed_root, operations):
    """Write dynamic and fixed records in one bounded pass over a source."""
    dynamic_root = Path(dynamic_root).resolve()
    fixed_root = Path(fixed_root).resolve()
    if dynamic_root.exists() or fixed_root.exists():
        raise ReplayError("fresh materialized dynamic and fixed roots required")
    dynamic_root.mkdir(parents=True)
    fixed_root.mkdir(parents=True)
    roots = (dynamic_root, fixed_root)
    descriptors = []
    temporary_paths = []
    streams = []
    digests = [hashlib.sha256(), hashlib.sha256()]
    counts = [0, 0]
    source_to_partition_sequence = ({}, {})
    try:
        for root in roots:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".trace.bin.", dir=root
            )
            descriptors.append(descriptor)
            temporary_paths.append(Path(temporary_name))
            streams.append(os.fdopen(descriptor, "wb"))
        descriptors.clear()
        for fixed, operation in operations:
            index = 1 if fixed else 0
            operand1 = operation.operand1
            if operation.opcode in {
                canonical.Opcode.LOAD_U32, canonical.Opcode.LOAD_U64,
                canonical.Opcode.LOAD_F32, canonical.Opcode.LOAD_F64,
            } and operand1:
                if operand1 & canonical.LOAD_DEPENDENCY_RELATIVE_FLAG:
                    distance = (
                        operand1 &
                        ~canonical.LOAD_DEPENDENCY_RELATIVE_FLAG
                    )
                    if distance == 0 or distance > operation.sequence:
                        raise ReplayError(
                            "canonical relative load dependency is invalid"
                        )
                    source_dependency = operation.sequence - distance
                else:
                    source_dependency = operand1 - 1
                    if source_dependency >= operation.sequence:
                        raise ReplayError(
                            "canonical absolute load dependency is invalid"
                        )
                mapped = source_to_partition_sequence[index].get(
                    source_dependency
                )
                operand1 = 0 if mapped is None else mapped + 1
            sequenced = dataclasses.replace(
                operation, sequence=counts[index], operand1=operand1,
            )
            payload = canonical.TRACE_STRUCT.pack(
                sequenced.phase, int(sequenced.opcode), 0,
                sequenced.work_item, sequenced.sequence, sequenced.address,
                sequenced.operand0, sequenced.operand1, sequenced.result,
            )
            streams[index].write(payload)
            digests[index].update(payload)
            source_to_partition_sequence[index][operation.sequence] = (
                counts[index]
            )
            counts[index] += 1
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        streams.clear()
        for temporary, root in zip(temporary_paths, roots):
            os.replace(temporary, root / "trace.bin")
    except BaseException:
        for stream in streams:
            stream.close()
        for descriptor in descriptors:
            os.close(descriptor)
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
        raise
    if counts[0] == 0:
        raise ReplayError("selected timing window has no canonical operations")
    if counts[1] == 0:
        raise ReplayError("selected timing window has no fixed events")
    return tuple(
        (digest.hexdigest(), count)
        for digest, count in zip(digests, counts)
    )


def _write_sparse_initial(root, words):
    """Bind only first-use words required by one materialized partition."""
    root = Path(root)
    records = {}
    rows = sorted((address, bits, value) for address, (bits, value) in words.items())
    segments = []
    for address, bits, value in rows:
        width = bits // 8
        if segments and segments[-1][1] == bits and address == segments[-1][0] + width * len(segments[-1][2]):
            segments[-1][2].append(value)
        else:
            segments.append([address, bits, [value]])
    for index, (base, bits, values) in enumerate(segments):
        suffix = "u32" if bits == 32 else "u64"
        relative = Path("initial") / f"segment-{index}.{suffix}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        code = "I" if bits == 32 else "Q"
        target.write_bytes(struct.pack(f"<{len(values)}{code}", *values))
        records[f"segment-{index}"] = {
            "path": relative.as_posix(), "sha256": _sha256_file(target),
            "logical_base": base, "word_bits": bits,
            "count": len(values), "byte_count": len(values) * (bits // 8),
        }
    return records


def _remember_initial(words, operation):
    widths = {
        canonical.Opcode.LOAD_U32: 32, canonical.Opcode.LOAD_F32: 32,
        canonical.Opcode.STORE_U32: 32, canonical.Opcode.STORE_F32: 32,
        canonical.Opcode.LOAD_U64: 64, canonical.Opcode.LOAD_F64: 64,
        canonical.Opcode.STORE_U64: 64, canonical.Opcode.STORE_F64: 64,
    }
    bits = widths.get(operation.opcode)
    if bits is None or operation.address in words:
        return
    initial = operation.operand0 if operation.opcode.name.startswith("LOAD") else 0
    words[operation.address] = (bits, initial)


def _eager_initial_word(bundle, root, address, bits):
    width = bits // 8
    for record in bundle.meta.get("initial_memory", {}).values():
        if record["word_bits"] != bits:
            continue
        base = record["logical_base"]
        limit = base + record["byte_count"]
        if base <= address and address + width <= limit:
            with (Path(root) / record["path"]).open("rb") as stream:
                stream.seek(address - base)
                payload = stream.read(width)
            if len(payload) != width:
                raise ReplayError("eager initial image is truncated")
            return int.from_bytes(payload, "little")
    raise ReplayError("eager window address lacks initial authority")


def materialize_window_trace(trace, *, manifest, phase, window_index, outdir):
    """Stream one canonical warmup+measure window into a bounded schema-1 trace."""
    trace = Path(trace).resolve()
    source_sha256 = trace_identity_sha256(trace)
    state = {"fixed": 0, "expanded": 0, "phase_items": 0}

    if (trace / "trace.meta.json").is_file():
        source_schema = 1
        source = canonical.read_bundle(trace)
        phase_name, phase_items = _eager_phase_identity(source, phase)
        window = _window_coordinates(
            manifest, trace_sha256=source_sha256, phase_name=phase_name,
            work_items=phase_items, window_index=window_index,
        )

        def partitioned_operations():
            dynamic_initial = {}
            fixed_initial = {}
            overlay = {}
            for operation in source.operations:
                if operation.phase != phase:
                    if operation.opcode.name.startswith("STORE_"):
                        bits = 32 if operation.opcode in {
                            canonical.Opcode.STORE_U32,
                            canonical.Opcode.STORE_F32,
                        } else 64
                        overlay[operation.address] = (bits, operation.operand0)
                    continue
                if operation.opcode in {
                    canonical.Opcode.BARRIER, canonical.Opcode.COMMIT,
                }:
                    state["fixed"] += 1
                    _remember_initial(fixed_initial, operation)
                    yield True, operation
                    continue
                if operation.work_item >= phase_items:
                    state["fixed"] += 1
                    _remember_initial(fixed_initial, operation)
                    yield True, operation
                    continue
                if window.warmup_start <= operation.work_item < window.measure_stop:
                    if operation.opcode.name.startswith(
                        ("LOAD_", "STORE_")
                    ) and operation.address not in dynamic_initial:
                        bits = 32 if operation.opcode in {
                            canonical.Opcode.LOAD_U32,
                            canonical.Opcode.LOAD_F32,
                            canonical.Opcode.STORE_U32,
                            canonical.Opcode.STORE_F32,
                        } else 64
                        value = overlay.get(operation.address)
                        if value is not None and value[0] != bits:
                            raise ReplayError(
                                "eager window memory width changes at one address"
                            )
                        if value is None:
                            value = (bits, _eager_initial_word(
                                source, trace, operation.address, bits
                            ))
                        dynamic_initial[operation.address] = value
                    yield False, dataclasses.replace(
                        operation,
                        work_item=operation.work_item - window.warmup_start,
                    )
                if operation.opcode.name.startswith("STORE_"):
                    bits = 32 if operation.opcode in {
                        canonical.Opcode.STORE_U32,
                        canonical.Opcode.STORE_F32,
                    } else 64
                    overlay[operation.address] = (bits, operation.operand0)
            state["dynamic_initial"] = dynamic_initial
            state["fixed_initial"] = fixed_initial

        source_meta = source.meta
        operations = partitioned_operations()
    else:
        source_schema = 2
        source = lazy.read_bundle(trace)
        phase_name, phase_items = _lazy_phase_identity(source, phase)
        window = _window_coordinates(
            manifest, trace_sha256=source_sha256, phase_name=phase_name,
            work_items=phase_items, window_index=window_index,
        )

        if source.meta.get("workload") == "gap_bc":
            def partitioned_operations():
                yield from _partition_bc_lazy_window(
                    source, phase, window, state
                )
        else:
            def partitioned_operations():
                phase_base = 0
                expanded = 0
                dynamic_initial = {}
                fixed_initial = {}
                with lazy.MappedState(source) as mapped:
                    for invocation in source.invocations:
                        try:
                            expander = lazy_registry.expander(invocation)
                        except lazy.LazyTraceError as error:
                            raise ReplayError(
                                f"unknown lazy replay kernel {invocation.kernel}"
                            ) from error
                        for operation in expander(mapped, invocation, 1024):
                            lazy._validate_expanded_operation(
                                source, invocation, operation
                            )
                            expanded += 1
                            operation = dataclasses.replace(
                                operation, sequence=expanded - 1
                            )
                            if invocation.phase != phase:
                                continue
                            if operation.opcode in {
                                canonical.Opcode.BARRIER,
                                canonical.Opcode.COMMIT,
                            }:
                                state["fixed"] += 1
                                _remember_initial(fixed_initial, operation)
                                yield True, operation
                                continue
                            if operation.work_item >= invocation.work_items:
                                state["fixed"] += 1
                                _remember_initial(fixed_initial, operation)
                                yield True, dataclasses.replace(
                                    operation,
                                    work_item=phase_base + operation.work_item,
                                )
                                continue
                            global_item = phase_base + operation.work_item
                            if (
                                window.warmup_start <= global_item
                                < window.measure_stop
                            ):
                                _remember_initial(dynamic_initial, operation)
                                yield False, dataclasses.replace(
                                    operation,
                                    work_item=(
                                        global_item - window.warmup_start
                                    ),
                                )
                        if invocation.phase == phase:
                            phase_base += invocation.work_items
                state["expanded"] = expanded
                state["phase_items"] = phase_base
                if expanded != source.dynamic_work["primitive_records"]:
                    raise ReplayError("lazy dynamic primitive count differs")
                if phase_base != phase_items:
                    raise ReplayError(
                        "lazy phase work count changed during expansion"
                    )
                state["dynamic_initial"] = dynamic_initial
                state["fixed_initial"] = fixed_initial

        source_meta = source.meta
        operations = partitioned_operations()

    fixed_outdir = Path(outdir).with_name(Path(outdir).name + ".fixed")
    dynamic_record, fixed_record = _write_partitioned_payload(
        outdir, fixed_outdir, operations
    )
    if source_schema in (1, 2):
        state["initial_memory"] = _write_sparse_initial(
            outdir, state["dynamic_initial"]
        )
        state["fixed_initial_memory"] = _write_sparse_initial(
            fixed_outdir, state["fixed_initial"]
        )
    trace_sha256, trace_records = dynamic_record
    fixed_trace_sha256, fixed_trace_records = fixed_record
    warmup_items = window.measure_start - window.warmup_start
    measured_items = window.measure_stop - window.measure_start
    meta = {
        "schema": 1,
        "workload": source_meta["workload"],
        "input_sha256": source_meta.get("input_sha256", source_sha256),
        "source_sha256": source_meta["source_sha256"],
        "binary_sha256": source_meta["binary_sha256"],
        "config_sha256": source_meta["config_sha256"],
        "phases": [{"id": phase, "name": phase_name,
                    "work_items": warmup_items + measured_items}],
        "output_boundaries": {},
        "source_schema": source_schema,
        "source_trace_sha256": source_sha256,
        "source_phase_work_items": phase_items,
        "window_index": window_index,
        "warmup_start": window.warmup_start,
        "measure_start": window.measure_start,
        "measure_stop": window.measure_stop,
        "measure_start_item": warmup_items,
        "fixed_event_records": state["fixed"],
        "trace_path": "trace.bin",
        "trace_sha256": trace_sha256,
        "trace_record_bytes": canonical.TRACE_STRUCT.size,
        "trace_records": trace_records,
        "outputs": {},
        "initial_memory": state.get("initial_memory", {}),
    }
    contract.atomic_write_json(Path(outdir) / "trace.meta.json", meta)
    fixed_meta = {
        **meta,
        "phases": [{"id": phase, "name": f"{phase_name}.fixed",
                    "work_items": phase_items}],
        "measure_start_item": 0,
        "trace_sha256": fixed_trace_sha256,
        "trace_records": fixed_trace_records,
        "fixed_component": True,
        "initial_memory": state.get(
            "fixed_initial_memory", meta["initial_memory"]
        ),
    }
    contract.atomic_write_json(fixed_outdir / "trace.meta.json", fixed_meta)
    canonical.read_bundle(outdir)
    canonical.read_bundle(fixed_outdir)
    return MaterializedTrace(
        Path(outdir).resolve(), fixed_outdir.resolve(), source_schema,
        source_sha256, phase, phase_name, window_index,
        warmup_items, measured_items, warmup_items, state["fixed"],
    )


def load_prepared_window_trace(dynamic_root, fixed_root):
    """Validate a bounded category adapter's dynamic/fixed trace pair."""

    dynamic_root = Path(dynamic_root).resolve()
    fixed_root = Path(fixed_root).resolve()
    dynamic = canonical.read_bundle(dynamic_root)
    fixed = canonical.read_bundle(fixed_root)
    record = dynamic.meta.get("prepared_window")
    required = {
        "source_schema", "source_trace_sha256", "phase", "phase_name",
        "warmup_items", "measured_items", "measure_start_item",
        "fixed_event_records", "fixed_trace_sha256",
        "window_index", "warmup_start", "measure_start", "measure_stop",
    }
    if record is None:
        phases = dynamic.meta.get("phases")
        flat_required = {
            "source_schema", "source_trace_sha256", "window_index",
            "warmup_start", "measure_start", "measure_stop",
            "measure_start_item", "fixed_event_records",
        }
        if (
            flat_required.issubset(dynamic.meta)
            and isinstance(phases, list)
            and len(phases) == 1
            and isinstance(phases[0], dict)
        ):
            record = {
                "source_schema": dynamic.meta["source_schema"],
                "source_trace_sha256": dynamic.meta["source_trace_sha256"],
                "phase": phases[0].get("id"),
                "phase_name": phases[0].get("name"),
                "warmup_items": (
                    dynamic.meta["measure_start"]
                    - dynamic.meta["warmup_start"]
                ),
                "measured_items": (
                    dynamic.meta["measure_stop"]
                    - dynamic.meta["measure_start"]
                ),
                "measure_start_item": dynamic.meta["measure_start_item"],
                "fixed_event_records": dynamic.meta["fixed_event_records"],
                "fixed_trace_sha256": fixed.meta.get("trace_sha256"),
                "window_index": dynamic.meta["window_index"],
                "warmup_start": dynamic.meta["warmup_start"],
                "measure_start": dynamic.meta["measure_start"],
                "measure_stop": dynamic.meta["measure_stop"],
            }
    if not isinstance(record, dict) or set(record) != required:
        raise ReplayError("prepared window metadata fields differ")
    source_sha256 = record["source_trace_sha256"]
    if (
        not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        raise ReplayError("prepared window source SHA-256 is invalid")
    if (
        fixed.meta.get("fixed_component") is not True
        or fixed.meta.get("source_trace_sha256") != source_sha256
        or fixed.meta.get("trace_sha256") != record["fixed_trace_sha256"]
    ):
        raise ReplayError("prepared fixed trace identity differs")
    integer_fields = (
        "source_schema", "phase", "warmup_items", "measured_items",
        "measure_start_item", "fixed_event_records",
        "window_index", "warmup_start", "measure_start", "measure_stop",
    )
    for name in integer_fields:
        value = record[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReplayError(f"prepared window {name} is invalid")
    if (
        record["source_schema"] == 0
        or record["measured_items"] == 0
        or record["fixed_event_records"] == 0
        or record["measure_start_item"] != record["warmup_items"]
        or not (
            record["warmup_start"] <= record["measure_start"]
            < record["measure_stop"]
        )
        or not isinstance(record["phase_name"], str)
        or not record["phase_name"]
    ):
        raise ReplayError("prepared window shape is invalid")
    selected_items = record["warmup_items"] + record["measured_items"]
    phases = dynamic.meta.get("phases")
    expected_phase = {
        "id": record["phase"],
        "name": record["phase_name"],
        "work_items": selected_items,
    }
    if (
        record["warmup_items"]
        != record["measure_start"] - record["warmup_start"]
        or record["measured_items"]
        != record["measure_stop"] - record["measure_start"]
        or phases != [expected_phase]
        or not dynamic.operations
        or any(operation.phase != record["phase"]
               or operation.work_item >= selected_items
               for operation in dynamic.operations)
        or len(fixed.operations) != record["fixed_event_records"]
        or any(operation.phase != record["phase"]
               for operation in fixed.operations)
    ):
        raise ReplayError("prepared window shape is invalid")
    return MaterializedTrace(
        dynamic_root,
        fixed_root,
        record["source_schema"],
        source_sha256,
        record["phase"],
        record["phase_name"],
        record["window_index"],
        record["warmup_items"],
        record["measured_items"],
        record["measure_start_item"],
        record["fixed_event_records"],
    )


def materialized_trace_record(materialized):
    if not isinstance(materialized, MaterializedTrace):
        raise ReplayError("materialized trace evidence has the wrong type")
    return {
        **dataclasses.asdict(materialized),
        "root": str(materialized.root),
        "fixed_root": str(materialized.fixed_root),
    }


def bind_materialized_window_selection(options, materialized):
    """Bind the replay ROI markers to a validated materialized window."""

    if not isinstance(materialized, MaterializedTrace):
        raise ReplayError("materialized trace selection has the wrong type")
    manifest = materialized.root / "trace.meta.json"
    if not manifest.is_file():
        raise ReplayError("materialized trace metadata is missing")
    options.window_manifest = manifest.resolve()
    options.phase = materialized.phase
    options.window_index = materialized.window_index
    options.measure_start_item = materialized.measure_start_item


def _emit_lazy_replay_stream(trace, stream):
    bundle = lazy.read_bundle(trace)
    operation_digest = hashlib.sha256()
    stream_digest = hashlib.sha256()
    commits = []
    chunk = bytearray()
    chunk_records = 0
    total_records = 0

    def write(payload):
        stream.write(payload)
        stream_digest.update(payload)

    def flush_chunk():
        nonlocal chunk, chunk_records
        if chunk_records == 0:
            return
        write(_STREAM_HEADER.pack(_STREAM_MAGIC, chunk_records, 0))
        write(chunk)
        chunk = bytearray()
        chunk_records = 0

    with lazy.MappedState(bundle) as state:
        for invocation in bundle.invocations:
            for operation in lazy_registry.expander(invocation)(
                state, invocation, 1024
            ):
                lazy._validate_expanded_operation(bundle, invocation, operation)
                operation = dataclasses.replace(operation, sequence=total_records)
                payload = canonical.TRACE_STRUCT.pack(
                    operation.phase, int(operation.opcode), 0,
                    operation.work_item, operation.sequence, operation.address,
                    operation.operand0, operation.operand1, operation.result,
                )
                operation_digest.update(payload)
                chunk.extend(payload)
                chunk_records += 1
                total_records += 1
                if operation.opcode == canonical.Opcode.COMMIT:
                    commits.append((operation.sequence, operation.result))
                if chunk_records == _STREAM_CHUNK_RECORDS:
                    flush_chunk()
            flush_chunk()
            write(_STREAM_HEADER.pack(_STREAM_MAGIC, 0, 2))
    if total_records != bundle.dynamic_work["primitive_records"]:
        raise ReplayError("lazy dynamic primitive count differs")
    flush_chunk()
    write(_STREAM_HEADER.pack(_STREAM_MAGIC, 0, 1))
    return {
        "schema": 1,
        "source_schema": 2,
        "source_trace_sha256": trace_identity_sha256(trace),
        "trace_records": total_records,
        "operations_sha256": operation_digest.hexdigest(),
        "stream_sha256": stream_digest.hexdigest(),
        "commit_order": [sequence for sequence, _ in commits],
        "raw_outputs": [result for _, result in commits],
        "boundary_commitments": bundle.meta.get("boundary_commitments", {}),
    }


def write_lazy_replay_stream(trace, path):
    """Write a framed schema-2 stream without materializing an eager trace."""
    bundle = lazy.read_bundle(trace)
    if bundle.dynamic_work["primitive_records"] > 1_000_000:
        raise ReplayError(
            "diagnostic lazy stream materialization exceeds record ceiling; "
            "use bounded pipe replay"
        )
    path = Path(path).resolve()
    if path.exists():
        raise ReplayError(f"lazy replay stream already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            evidence = _emit_lazy_replay_stream(trace, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ReplayError(f"lazy replay stream already exists: {path}") from error
        return evidence
    finally:
        temporary.unlink(missing_ok=True)


def build_replay_binary(outdir, *, native=False, cxx="g++"):
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"replay build root already exists: {outdir}")
    if shutil.which(cxx) is None:
        raise ReplayError(f"C++ compiler is unavailable: {cxx}")
    outdir.mkdir(parents=True)
    binary = outdir / "trace_replay"
    command = [
        cxx,
        "-std=c++17",
        "-O3",
        "-fopenmp",
        "-ffp-contract=off",
        "-fno-fast-math",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(REPO),
        "-I",
        str(REPO / "include"),
    ]
    if native:
        command.append("-DTRACE_REPLAY_NATIVE=1")
    else:
        command.extend(("-static", "-no-pie"))
    command.append(str(SOURCE))
    if not native:
        if not M5_LIBRARY.is_file():
            raise ReplayError(f"checked-in m5 ABI library is missing: {M5_LIBRARY}")
        command.append(str(M5_LIBRARY))
    command.extend(("-o", str(binary)))
    try:
        subprocess.run(
            command,
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ReplayError(
            f"trace replay build failed: {error.stderr.strip()}"
        ) from error
    manifest = {
        "schema": 1,
        "native": bool(native),
        "source": str(SOURCE),
        "source_sha256": _sha256_file(SOURCE),
        "binary": str(binary),
        "binary_sha256": _sha256_file(binary),
        "command": command,
    }
    if not native:
        manifest["m5_library"] = str(M5_LIBRARY)
        manifest["m5_library_sha256"] = _sha256_file(M5_LIBRARY)
    (outdir / "build.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return binary


def _boundary_contract(bundle):
    specifications = bundle.meta.get("output_boundaries")
    if not isinstance(specifications, dict):
        raise ReplayError("canonical output boundary metadata is invalid")
    if set(specifications) != set(bundle.outputs):
        raise ReplayError("canonical output boundary set differs")
    rows = []
    for name in sorted(specifications):
        specification = specifications[name]
        if not isinstance(specification, dict):
            raise ReplayError(f"output boundary {name} metadata is invalid")
        word_bits = specification.get("word_bits")
        count = specification.get("count")
        probes = specification.get("probes")
        if word_bits not in (32, 64):
            raise ReplayError(f"output boundary {name} word width is invalid")
        if (
            isinstance(count, bool) or not isinstance(count, int)
            or count < 0 or len(bundle.outputs[name]) != count
        ):
            raise ReplayError(f"output boundary {name} count is invalid")
        if not isinstance(probes, list) or len(probes) != count:
            raise ReplayError(
                f"output boundary {name} address mapping is missing"
            )
        checked = []
        for index, probe in enumerate(probes):
            if not isinstance(probe, dict):
                raise ReplayError(
                    f"output boundary {name} probe {index} is invalid"
                )
            address = probe.get("address")
            after_sequence = probe.get("after_sequence")
            if (
                isinstance(address, bool) or not isinstance(address, int)
                or address < 0 or address >= 1 << 64
            ):
                raise ReplayError(
                    f"output boundary {name} address mapping {index} "
                    "is invalid"
                )
            if (
                isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
                or after_sequence >= len(bundle.operations)
            ):
                raise ReplayError(
                    f"output boundary {name} after-sequence {index} is invalid"
                )
            checked.append((address, after_sequence))
        rows.append((name, word_bits, tuple(checked)))
    return tuple(rows)


def _validate_lazy_functional_boundaries(bundle):
    commitments = bundle.meta.get("boundary_commitments")
    if not isinstance(commitments, dict) or not commitments:
        raise ReplayError("lazy trace raw output boundary mapping is missing")
    return commitments


def _write_boundary_map(bundle, path):
    """Bind each named raw word to one canonical observed operation."""
    path = Path(path).resolve()
    if path.exists():
        raise ReplayError(f"fresh boundary map required: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _boundary_contract(bundle)
    lines = [_BOUNDARY_MAGIC, str(len(rows))]
    for name, word_bits, probes in rows:
        encoded_name = name.encode("utf-8").hex()
        lines.append(" ".join((
            encoded_name, str(word_bits), str(len(probes)),
            *(str(value) for probe in probes for value in probe),
        )))
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _write_initial_memory_map(bundle, root, path):
    path = Path(path).resolve()
    if path.exists():
        raise ReplayError(f"fresh initial memory map required: {path}")
    records = bundle.meta.get("initial_memory")
    if not isinstance(records, dict):
        raise ReplayError("canonical initial memory images are missing")
    widths = {
        canonical.Opcode.LOAD_U32: 32,
        canonical.Opcode.LOAD_F32: 32,
        canonical.Opcode.STORE_U32: 32,
        canonical.Opcode.STORE_F32: 32,
        canonical.Opcode.LOAD_U64: 64,
        canonical.Opcode.LOAD_F64: 64,
        canonical.Opcode.STORE_U64: 64,
        canonical.Opcode.STORE_F64: 64,
    }
    occupied = []
    for record in records.values():
        start = record["logical_base"]
        occupied.append((start, start + record["byte_count"]))
    sparse = {}
    for operation in bundle.operations:
        bits = widths.get(operation.opcode)
        if bits is None:
            continue
        address = operation.address
        width = bits // 8
        if any(start <= address and address + width <= stop
               for start, stop in occupied):
            continue
        overlaps = [
            prior
            for prior in range(max(0, address - 7), address + width)
            if prior in sparse
            and address < prior + sparse[prior][0] // 8
            and prior < address + width
        ]
        if overlaps and address not in sparse:
            raise ReplayError("sparse initial memory words overlap")
        if address not in sparse:
            initial = (
                operation.operand0
                if operation.opcode.name.startswith("LOAD_") else 0
            )
            sparse[address] = (bits, initial)
        elif sparse[address][0] != bits:
            raise ReplayError("sparse initial memory width changes")
    lines = [
        _INITIAL_MEMORY_SPARSE_MAGIC,
        f"{len(records)} {len(sparse)}",
    ]
    for name in sorted(records):
        record = records[name]
        image_path = (Path(root) / record["path"]).resolve()
        lines.append(" ".join((
            str(record["logical_base"]), str(record["word_bits"]),
            str(record["count"]), image_path.as_posix(),
        )))
    lines.extend(
        f"{address} {bits} {value}"
        for address, (bits, value) in sorted(sparse.items())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _write_lazy_initial_memory_map(bundle, path):
    path = Path(path).resolve()
    scalar_root = path.with_suffix(".scalars")
    scalar_root.mkdir(parents=True, exist_ok=False)
    widths = {"u32": 32, "u64": 64, "f32": 32, "f64": 64}
    rows = [
        (array.logical_base, widths[array.element_type], array.count,
         (bundle.root / array.path).resolve())
        for array in bundle.arrays
    ]
    for name in sorted(bundle.meta["initial_scalars"]):
        image = scalar_root / f"{name}.u64"
        image.write_bytes(struct.pack("<Q", bundle.meta["initial_scalars"][name]))
        rows.append((bundle.meta["scalar_addresses"][name], 64, 1, image))
    lines = [_INITIAL_MEMORY_IMAGE_MAGIC, str(len(rows))]
    lines.extend(" ".join(map(str, row)) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _write_lazy_boundary_map(bundle, path):
    commitments = bundle.meta.get("boundary_commitments")
    if not isinstance(commitments, dict) or not commitments:
        raise ReplayError("lazy trace raw output boundary mapping is missing")
    rows = []
    selected = set()
    sequence = 0
    with lazy.MappedState(bundle) as state:
        for invocation in bundle.invocations:
            commit = None
            for operation in lazy_registry.expander(invocation)(
                state, invocation, 1024
            ):
                lazy._validate_expanded_operation(bundle, invocation, operation)
                if operation.opcode == canonical.Opcode.COMMIT:
                    commit = sequence
                sequence += 1
            if commit is None:
                raise ReplayError("lazy invocation has no COMMIT")
            for name, bits, count, base in lazy_registry.boundary_specs(
                bundle, invocation
            ):
                if name not in commitments:
                    continue
                if name in selected:
                    raise ReplayError(f"duplicate lazy boundary mapping: {name}")
                step = bits // 8
                rows.append((name, bits, tuple(
                    (base + index * step, commit) for index in range(count)
                )))
                selected.add(name)
    if set(commitments) != selected:
        raise ReplayError("lazy trace raw output boundary mapping is missing")
    lines = [_BOUNDARY_MAGIC, str(len(rows))]
    for name, bits, probes in rows:
        lines.append(" ".join((name.encode().hex(), str(bits), str(len(probes)),
            *(str(value) for probe in probes for value in probe))))
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return Path(path)


def validate_output_boundaries(bundle, observed):
    """Require an exact named/shape/order/raw-word replay boundary image."""
    _boundary_contract(bundle)
    if not isinstance(observed, dict):
        raise ReplayError("replay output boundaries are missing")
    if set(observed) != set(bundle.outputs):
        raise ReplayError("replay output boundary set differs from canonical")
    for name in sorted(bundle.outputs):
        record = observed[name]
        specification = bundle.meta["output_boundaries"][name]
        if not isinstance(record, dict):
            raise ReplayError(f"replay output boundary {name} is invalid")
        if record.get("word_bits") != specification["word_bits"]:
            raise ReplayError(f"replay output boundary {name} width differs")
        if record.get("count") != specification["count"]:
            raise ReplayError(f"replay output boundary {name} count differs")
        words = record.get("raw_words")
        if not isinstance(words, list):
            raise ReplayError(f"replay output boundary {name} words are missing")
        try:
            canonical.compare_words(
                bundle.outputs[name], words, name,
                word_bits=specification["word_bits"],
            )
        except canonical.TraceError as error:
            raise ReplayError(str(error)) from error
    return observed


def run_native_replay(binary, *, system, trace, outdir):
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    binary = Path(binary).resolve()
    trace = Path(trace).resolve()
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"replay output root already exists: {outdir}")
    bundle = canonical.read_bundle(trace)
    outdir.mkdir(parents=True)
    result = outdir / "result.json"
    boundary_map = _write_boundary_map(bundle, outdir / "boundary-map.txt")
    initial_map = _write_initial_memory_map(
        bundle, trace, outdir / "initial-memory-map.txt"
    )
    command = [
        str(binary),
        "--system",
        system,
        "--trace",
        str(trace / "trace.bin"),
        "--result",
        str(result),
        "--boundary-map",
        str(boundary_map),
        "--initial-memory-map",
        str(initial_map),
    ]
    try:
        subprocess.run(
            command,
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"OMP_NUM_THREADS": "4"},
        )
    except subprocess.CalledProcessError as error:
        raise ReplayError(
            f"{system} native replay failed: {error.stderr.strip()}"
        ) from error
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"invalid native replay result: {error}") from error
    if value.get("trace_records") != len(bundle.operations):
        raise ReplayError("native replay record count differs from trace")
    if value.get("verification") != "pass":
        raise ReplayError("native replay bit-exact verification failed")
    validate_output_boundaries(bundle, value.get("output_boundaries"))
    value.update(_exact_correctness_fields(value))
    return value


def _exact_correctness_fields(result):
    boundaries = result.get("output_boundaries", {})
    compared_words = sum(
        len(record.get("raw_words", ()))
        for record in boundaries.values()
        if isinstance(record, dict)
    )
    if compared_words == 0:
        compared_words = len(result.get("raw_outputs", ()))
    return {
        "numeric_verification": "pass",
        "bit_exact": True,
        "compared_words": compared_words,
        "mismatched_words": 0,
        "nonfinite_words": 0,
    }


def run_native_lazy_replay(binary, *, system, trace, outdir):
    trace = Path(trace).resolve()
    outdir = Path(outdir).resolve()
    bundle = lazy.read_bundle(trace)
    _validate_lazy_functional_boundaries(bundle)
    outdir.mkdir(parents=True)
    boundary_map = _write_lazy_boundary_map(bundle, outdir / "boundary-map.txt")
    initial_map = _write_lazy_initial_memory_map(
        bundle, outdir / "initial-memory-map.txt"
    )
    result_path = outdir / "result.json"
    pipe_read, pipe_write = os.pipe()
    stream_path = Path(f"/proc/{os.getpid()}/fd/{pipe_read}")
    command = [
        str(Path(binary).resolve()), "--system", system,
        "--trace", str(stream_path), "--result", str(result_path),
        "--mode", "functional", "--stream", "1",
        "--boundary-map", str(boundary_map),
        "--initial-memory-map", str(initial_map),
    ]
    evidence = {}
    errors = []
    def produce():
        try:
            with os.fdopen(pipe_write, "wb", buffering=0) as stream:
                evidence.update(_emit_lazy_replay_stream(trace, stream))
        except BaseException as error:
            errors.append(error)
    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    try:
        subprocess.run(command, cwd=REPO, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True,
                       env={"OMP_NUM_THREADS": "4"})
    except subprocess.CalledProcessError as error:
        raise ReplayError(
            f"{system} native lazy replay failed: {error.stderr.strip()}"
        ) from error
    finally:
        os.close(pipe_read)
        producer.join(timeout=60)
    if producer.is_alive() or errors:
        raise ReplayError(f"native lazy producer failed: {errors[:1]}")
    expected = evidence
    value = _load_json(result_path, "native lazy replay result")
    if value.get("trace_records") != expected["trace_records"]:
        raise ReplayError("native lazy replay record count differs")
    observed = value.get("output_boundaries", {})
    if set(observed) != set(expected["boundary_commitments"]):
        raise ReplayError("native lazy replay boundary set differs")
    for name, digest in expected["boundary_commitments"].items():
        row = observed[name]
        code = "I" if row["word_bits"] == 32 else "Q"
        payload = struct.pack(f"<{len(row['raw_words'])}{code}", *row["raw_words"])
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ReplayError(f"native lazy replay boundary {name} differs")
    value.update(_exact_correctness_fields(value))
    return value


def _integer(row, field):
    value = row.get(field)
    if isinstance(value, bool):
        raise ReplayError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ReplayError(f"{field} must be an integer") from error
    if result != value or result < 0:
        raise ReplayError(f"{field} must be a nonnegative integer")
    return result


def validate_mechanism(
    system, row, *, require_activity=True, cxl_link_delay="1us"
):
    """Fail closed on topology, correctness, or mechanism-counter drift."""
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    if row.get("verification") != "pass":
        raise ReplayError(f"{system} bit-exact verification did not pass")
    if _integer(row, "threads") != 4:
        raise ReplayError("matched replay requires four threads")
    if row.get("all_memory_cxl") is not True:
        raise ReplayError("matched replay requires all-CXL memory")
    if row.get("allocated_on_cxl") is not True:
        raise ReplayError("matched replay allocation is not on CXL")
    if _integer(row, "cxl_link_delay_ticks") != latency.ticks(
        cxl_link_delay
    ):
        raise ReplayError("gem5 CXL latency differs from the campaign identity")
    if _integer(row, "queue_errors"):
        raise ReplayError(f"{system} queue errors are nonzero")
    if _integer(row, "descriptor_errors"):
        raise ReplayError(f"{system} descriptor errors are nonzero")

    if system == "amu":
        issued = _integer(row, "issued_loads")
        completed = _integer(row, "completed_loads")
        if issued != completed or (require_activity and issued == 0):
            raise ReplayError("AMU issued/completed loads differ")
        drains = _integer(row, "drains")
        phases = _integer(row, "phases")
        if drains > phases * _integer(row, "threads"):
            raise ReplayError("AMU per-request drain is forbidden")
    elif system == "cira":
        issued = _integer(row, "issued_prefetches")
        completed = _integer(row, "completed_prefetches")
        if issued != completed or (require_activity and issued == 0):
            raise ReplayError("CIRA issued/completed prefetches differ")
        issued_per_core = row.get("issued_per_core")
        completed_per_core = row.get("completed_per_core")
        if (
            not isinstance(issued_per_core, list)
            or len(issued_per_core) != 4
            or (require_activity and any(
                _integer({"value": value}, "value") == 0
                for value in issued_per_core
            ))
        ):
            raise ReplayError("CIRA requires four active cores")
        if issued_per_core != completed_per_core:
            raise ReplayError("CIRA per-core issued/completed work differs")
    return row


def validate_config_ini(path, *, cxl_link_delay="1us"):
    """Prove the generated gem5 topology is four-core, timing, and all CXL."""
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeDecodeError, configparser.Error) as error:
        raise ReplayError(f"cannot parse gem5 config.ini: {error}") from error
    if not parser.has_section("board"):
        raise ReplayError("gem5 config has no board section")
    mem_mode = parser.get("board", "mem_mode", fallback="")
    core_sets = {"cores": [], "start": [], "switch": []}
    patterns = {
        "cores": _CORE_SECTION,
        "start": _START_CORE_SECTION,
        "switch": _SWITCH_CORE_SECTION,
    }
    for section in parser.sections():
        for name, pattern in patterns.items():
            match = pattern.fullmatch(section)
            if match is not None:
                core_sets[name].append(int(match.group(1)))
    normal = sorted(core_sets["cores"]) == [0, 1, 2, 3]
    switched = (
        sorted(core_sets["start"]) == [0, 1, 2, 3]
        and sorted(core_sets["switch"]) == [0, 1, 2, 3]
        and not core_sets["cores"]
    )
    if not ((normal and mem_mode == "timing") or
            (switched and mem_mode == "atomic")):
        raise ReplayError(
            "matched replay requires timing or switched timing memory mode"
        )
    measured_prefix = "switch" if switched else "cores"
    for core in range(4):
        cpu_type = parser.get(
            f"board.processor.{measured_prefix}{core}.core",
            "type", fallback="",
        )
        if "Timing" not in cpu_type and "O3" not in cpu_type:
            raise ReplayError("gem5 measured core is not a timing CPU")
    if switched:
        for core in range(4):
            section = f"board.processor.start{core}.core"
            if (
                "Atomic" not in parser.get(section, "type", fallback="")
                or parser.get(section, "switched_out", fallback="") != "false"
                or parser.get(
                    f"board.processor.switch{core}.core",
                    "switched_out", fallback="",
                ) != "true"
            ):
                raise ReplayError("gem5 fast-forward core contract differs")

    board_ranges = parser.get("board", "mem_ranges", fallback="").split()
    links = []
    for section in parser.sections():
        if parser.get(section, "type", fallback="") != "SerialLink":
            continue
        ranges = parser.get(section, "ranges", fallback="").split()
        if ranges and set(ranges).intersection(board_ranges):
            links.append(section)
    if not links:
        raise ReplayError("gem5 config has no CXL SerialLink memory route")
    delays = {
        int(parser.get(section, "delay", fallback="-1")) for section in links
    }
    expected_ticks = latency.ticks(cxl_link_delay)
    if delays != {expected_ticks}:
        raise ReplayError("gem5 CXL latency differs from the campaign identity")
    covered = set()
    link_ports = set()

    def reaches_memory(port, visited):
        if ".memory." in port and "mem_ctrl" in port:
            return True
        section = port.split(".cpu_side", 1)[0].split(".mem_side", 1)[0]
        if section in visited or not parser.has_section(section):
            return False
        visited.add(section)
        if parser.get(section, "type", fallback="") not in {
            "NoncoherentXBar", "CoherentXBar",
        }:
            return False
        return any(
            reaches_memory(destination, visited)
            for destination in parser.get(
                section, "mem_side_ports", fallback=""
            ).split()
        )

    for section in links:
        covered.update(parser.get(section, "ranges", fallback="").split())
        link_ports.add(f"{section}.cpu_side_port")
        destination = parser.get(section, "mem_side_port", fallback="")
        if not reaches_memory(destination, set()):
            raise ReplayError("CXL link does not terminate at memory")
    if set(board_ranges) - covered:
        raise ReplayError("CXL links do not cover every board memory range")

    membus = "board.cache_hierarchy.membus"
    if not parser.has_section(membus):
        raise ReplayError("gem5 config has no coherent memory bus")
    destinations = parser.get(membus, "mem_side_ports", fallback="").split()
    if not link_ports.issubset(destinations):
        raise ReplayError("CXL memory links are not attached to the memory bus")
    if any(".memory." in port or "mem_ctrl" in port for port in destinations):
        raise ReplayError("a memory-controller path bypasses CXL")
    return {
        "threads": 4,
        "all_memory_cxl": True,
        "fast_forward_setup": switched,
        "cxl_link_delay_ticks": expected_ticks,
        "cxl_links": links,
        "memory_ranges": board_ranges,
    }


def parse_allocation_log(path, *, required_bytes):
    if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
        raise ReplayError("required allocation bytes must be an integer")
    if required_bytes <= 0:
        raise ReplayError("required allocation bytes must be positive")
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="strict"
        ).splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReplayError(f"cannot read replay allocation log: {error}") from error
    matches = [match for line in lines if (match := _ALLOCATION.fullmatch(line))]
    if len(matches) != 1:
        raise ReplayError("replay allocation log must contain one marker")
    logical_bytes = int(matches[0].group(1))
    allocated_bytes = int(matches[0].group(2))
    on_cxl = matches[0].group(3) == "true"
    if logical_bytes < required_bytes or allocated_bytes < logical_bytes:
        raise ReplayError("replay allocation does not cover canonical state")
    if not on_cxl:
        raise ReplayError("replay allocation is not marked all-CXL")
    return {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "allocated_on_cxl": True,
    }


def command_for(options):
    if options.system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {options.system}")
    if options.mode not in {"functional", "window"}:
        raise ReplayError(f"unsupported replay mode: {options.mode}")
    trace = Path(options.trace).resolve()
    trace_file = Path(getattr(options, "replay_trace", trace / "trace.bin"))
    binary_args = [
        "--system", options.system,
        "--trace", str(trace_file),
        "--result", str((Path(options.outdir).resolve() / "result.json")),
        "--mode", options.mode,
    ]
    if getattr(options, "stream_mode", False):
        binary_args.extend(("--stream", "1"))
    boundary_map = getattr(options, "boundary_map", None)
    if boundary_map is not None:
        binary_args.extend(("--boundary-map", str(Path(boundary_map).resolve())))
    initial_memory_map = getattr(options, "initial_memory_map", None)
    if initial_memory_map is not None:
        binary_args.extend((
            "--initial-memory-map", str(Path(initial_memory_map).resolve())
        ))
    if options.mode == "functional":
        if any(
            value is not None
            for value in (
                options.window_manifest, options.phase, options.window_index
            )
        ):
            raise ReplayError("functional replay may not select a timing window")
    else:
        if (
            options.window_manifest is None
            or options.phase is None
            or options.window_index is None
        ):
            raise ReplayError("window replay requires manifest, phase, and index")
        binary_args.extend((
            "--window-manifest", str(Path(options.window_manifest).resolve()),
            "--phase", str(options.phase),
            "--window-index", str(options.window_index),
            "--measure-start-item",
            str(getattr(options, "measure_start_item", 0)),
        ))

    command = [
        str(Path(options.gem5).resolve()),
        "--redirect-stdout",
        "--redirect-stderr",
        "--stdout-file=simout",
        "--stderr-file=simerr",
        "-d", str(Path(options.outdir).resolve()),
        str(Path(options.config).resolve()),
        "--binary", str(Path(options.binary).resolve()),
        "--arguments", shlex.join(binary_args),
        "--cores", "4",
        "--cpu", "timing",
        "--cxl-memory",
        "--cxl-link-delay", getattr(options, "cxl_link_delay", "1us"),
        "--require-m5-verification-exit",
    ]
    if options.system == "amu":
        command.extend((
            "--asmc-profile", "paper-calibrated",
            "--asmc-calibration-manifest",
            str(Path(options.calibration).resolve()),
            "--asmc-spm-size", "64KiB",
        ))
    else:
        command.append("--no-asmc")
    if options.system == "cira":
        command.extend(("--cira", "--cira-to-l2"))
    if options.mode == "window":
        command.extend((
            "--roi-work-events", "--continue-after-roi",
            "--fast-forward-cpu", "atomic",
            "--fast-forward-replay-window",
            "--iterations", "2", "--measure-trial", "1",
        ))
    return command


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReplayError(f"{label} must be a JSON object")
    return value


def _stat_integer(stats, name):
    if name not in stats:
        raise ReplayError(f"missing required gem5 statistic: {name}")
    value = stats[name]
    integer = int(value)
    if value != integer or integer < 0:
        raise ReplayError(f"gem5 statistic is not a nonnegative integer: {name}")
    return integer


def _expected_commits(bundle):
    commits = [
        operation for operation in bundle.operations
        if operation.opcode == canonical.Opcode.COMMIT
    ]
    return (
        [operation.sequence for operation in commits],
        [operation.result for operation in commits],
    )


def _required_shadow_bytes(bundle):
    memory_opcodes = {
        canonical.Opcode.LOAD_U32, canonical.Opcode.LOAD_U64,
        canonical.Opcode.LOAD_F32, canonical.Opcode.LOAD_F64,
        canonical.Opcode.STORE_U32, canonical.Opcode.STORE_U64,
        canonical.Opcode.STORE_F32, canonical.Opcode.STORE_F64,
    }
    return 64 * len({
        operation.address & ~63 for operation in bundle.operations
        if operation.opcode in memory_opcodes
    })


def collect_run_evidence(run_dir, *, system, trace, config,
                         expected=None, required_bytes=None,
                         require_activity=True, cxl_link_delay="1us"):
    """Join bit-exact program output with gem5-owned causal statistics."""
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    run_dir = Path(run_dir).resolve()
    bundle = None
    if expected is None:
        bundle = canonical.read_bundle(Path(trace).resolve())
        expected_order, expected_raw = _expected_commits(bundle)
        required_bytes = _required_shadow_bytes(bundle)
    else:
        expected_order = expected["commit_order"]
        expected_raw = expected["raw_outputs"]
        if required_bytes is None:
            raise ReplayError("stream replay required allocation is missing")
    result = _load_json(run_dir / "result.json", "replay result")
    if result.get("commit_order") != expected_order:
        raise ReplayError("replay commit order differs from canonical trace")
    if result.get("raw_outputs") != expected_raw:
        raise ReplayError("replay raw output differs from canonical trace")
    if expected is not None and result.get("trace_records") != expected[
        "trace_records"
    ]:
        raise ReplayError("stream replay record count differs")
    if result.get("verification") != "pass":
        raise ReplayError("replay program verification did not pass")
    if bundle is not None:
        validate_output_boundaries(bundle, result.get("output_boundaries"))
    else:
        observed = result.get("output_boundaries")
        commitments = expected.get("boundary_commitments", {})
        if not isinstance(observed, dict) or set(observed) != set(commitments):
            raise ReplayError("stream replay output boundary set differs")
        for name, digest in commitments.items():
            record = observed[name]
            bits = record.get("word_bits")
            words = record.get("raw_words")
            if bits not in (32, 64) or not isinstance(words, list):
                raise ReplayError(f"stream replay boundary {name} is invalid")
            code = "I" if bits == 32 else "Q"
            payload = struct.pack(f"<{len(words)}{code}", *words)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ReplayError(f"stream replay boundary {name} differs")

    topology = validate_config_ini(
        config, cxl_link_delay=cxl_link_delay
    )
    expected_ticks = latency.ticks(cxl_link_delay)
    if topology["cxl_link_delay_ticks"] != expected_ticks:
        raise ReplayError("gem5 CXL latency differs from the campaign identity")
    allocation = (
        parse_allocation_log(run_dir / "simout", required_bytes=required_bytes)
        if required_bytes
        else {"logical_bytes": 0, "allocated_bytes": 0,
              "allocated_on_cxl": True}
    )
    try:
        stats = comparison.parse_stats(run_dir / "stats.txt")
    except comparison.StatsError as error:
        raise ReplayError(str(error)) from error
    sim_ticks = _stat_integer(stats, "simTicks")
    row = {
        "verification": "pass",
        **_exact_correctness_fields(result),
        "threads": _integer(result, "threads"),
        "phases": _integer(result, "phases"),
        "all_memory_cxl": topology["all_memory_cxl"],
        "cxl_link_delay": cxl_link_delay,
        "cxl_link_delay_ticks": topology["cxl_link_delay_ticks"],
        "allocated_on_cxl": allocation["allocated_on_cxl"],
        "allocated_bytes": allocation["allocated_bytes"],
        "logical_bytes": allocation["logical_bytes"],
        "raw_outputs": result["raw_outputs"],
        "commit_order": result["commit_order"],
        "sim_ticks": sim_ticks,
        "queue_errors": 0,
        "descriptor_errors": 0,
    }
    if system == "amu":
        row.update({
            "issued_loads": _stat_integer(
                stats, "board.asmc.issuedLoads"
            ),
            "completed_loads": _stat_integer(
                stats, "board.asmc.completedLoads"
            ),
            "drains": _integer(result, "drains"),
        })
        row["queue_errors"] = sum(
            _stat_integer(stats, name)
            for name in (
                "board.asmc.rejectedQueueFull",
                "board.asmc.rejectedSpmFull",
                "board.asmc.translationFaults",
                "board.asmc.pendingQueueFull",
                "board.asmc.farSpmFlagPackets",
                "board.asmc.spmMissingFlagPackets",
            )
        )
    elif system == "cira":
        row.update({
            "issued_prefetches": _stat_integer(
                stats, "board.cira.issuedPrefetches"
            ),
            "completed_prefetches": _stat_integer(
                stats, "board.cira.completedPrefetches"
            ),
            "issued_per_core": [
                _stat_integer(stats, f"board.cira.issuedPrefetchesPerCore::{core}")
                for core in range(4)
            ],
            "completed_per_core": [
                _stat_integer(
                    stats, f"board.cira.completedPrefetchesPerCore::{core}"
                )
                for core in range(4)
            ],
        })
        row["queue_errors"] = _stat_integer(
            stats, "board.cira.rejectedQueueFull"
        ) + _stat_integer(stats, "board.cira.rejectedCsrIndexQueueFull")
        row["descriptor_errors"] = _stat_integer(
            stats, "board.cira.droppedCsrDescriptors"
        )
    validate_mechanism(
        system, row, require_activity=require_activity,
        cxl_link_delay=cxl_link_delay,
    )
    return row


def combine_window_evidence(dynamic, fixed, *, fixed_trace):
    """Keep measured-window and one-time fixed ROI timing disjoint."""
    dynamic_ticks = _integer(dynamic, "sim_ticks")
    fixed_ticks = _integer(fixed, "sim_ticks")
    if dynamic.get("verification") != "pass":
        raise ReplayError("dynamic window verification did not pass")
    if fixed.get("verification") != "pass":
        raise ReplayError("fixed replay verification did not pass")
    if fixed_ticks == 0:
        raise ReplayError("fixed replay simTicks must be positive")
    fixed_trace = Path(fixed_trace).resolve()
    if not fixed_trace.is_file():
        raise ReplayError("fixed replay trace artifact is missing")
    return {
        **dynamic,
        "sim_ticks": dynamic_ticks,
        "fixed_sim_ticks": fixed_ticks,
        "fixed_trace_sha256": _sha256_file(fixed_trace),
    }


def _launch_gem5(command, *, outdir, timeout, label):
    outdir = Path(outdir).resolve()
    outdir.parent.mkdir(parents=True, exist_ok=True)
    driver_log = outdir.with_suffix(f".{label}.driver.log")
    try:
        with driver_log.open("x", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if timeout == 0 else timeout,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReplayError(
            f"gem5 {label} replay launch failed: {error}"
        ) from error
    if completed.returncode != 0:
        raise ReplayError(
            f"gem5 {label} replay exited {completed.returncode}; "
            f"see {driver_log}"
        )
    return driver_log


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one matched Vanilla/AMU/CIRA canonical replay."
    )
    parser.add_argument("--mode", choices=("functional", "window"), required=True)
    parser.add_argument("--system", choices=SYSTEMS, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--fixed-trace", type=Path)
    parser.add_argument("--window-manifest", type=Path)
    parser.add_argument("--phase", type=int)
    parser.add_argument("--window-index", type=int)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument(
        "--cxl-link-delay", choices=latency.LABELS, default="1us"
    )
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    options = parser.parse_args(argv)
    canonical_selection = (
        options.window_manifest, options.phase, options.window_index
    )
    if options.mode == "functional" and (
        options.fixed_trace is not None
        or any(value is not None for value in canonical_selection)
    ):
        parser.error("functional replay may not select a timing window")
    if options.mode == "window":
        canonical_complete = all(
            value is not None for value in canonical_selection
        )
        canonical_absent = all(
            value is None for value in canonical_selection
        )
        prepared_complete = options.fixed_trace is not None
        if not (
            (canonical_complete and not prepared_complete)
            or (canonical_absent and prepared_complete)
        ):
            parser.error(
                "window replay requires either manifest/phase/index or "
                "a prepared fixed trace"
            )
    if options.phase is not None and options.phase < 0:
        parser.error("--phase must be nonnegative")
    if options.window_index is not None and options.window_index < 0:
        parser.error("--window-index must be nonnegative")
    if options.timeout < 0:
        parser.error("--timeout must be nonnegative")
    return options


def run(options):
    outdir = Path(options.outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"fresh replay output root required: {outdir}")
    for label in ("binary", "gem5", "config"):
        path = Path(getattr(options, label)).resolve()
        if not path.is_file():
            raise ReplayError(f"replay {label} is missing: {path}")
    trace = Path(options.trace).resolve()
    materialized = None
    lazy_bundle = None
    producer_thread = None
    producer_evidence = {}
    producer_errors = []
    pipe_read = None
    boundary_map = None
    initial_memory_map = None
    replay_trace = trace
    if options.mode == "window":
        if getattr(options, "fixed_trace", None) is not None:
            materialized = load_prepared_window_trace(
                trace, options.fixed_trace
            )
        else:
            materialized = materialize_window_trace(
                trace,
                manifest=Path(options.window_manifest).resolve(),
                phase=options.phase,
                window_index=options.window_index,
                outdir=outdir.with_name(outdir.name + ".input"),
            )
        replay_trace = materialized.root
        materialized_bundle = canonical.read_bundle(replay_trace)
        initial_memory_map = _write_initial_memory_map(
            materialized_bundle, replay_trace,
            outdir.with_name(outdir.name + ".initial-memory-map.txt"),
        )
    elif (trace / "trace.v2.json").is_file():
        lazy_bundle = lazy.read_bundle(trace)
        _validate_lazy_functional_boundaries(lazy_bundle)
        boundary_map = _write_lazy_boundary_map(
            lazy_bundle,
            outdir.with_name(outdir.name + ".boundary-map.txt"),
        )
        initial_memory_map = _write_lazy_initial_memory_map(
            lazy_bundle,
            outdir.with_name(outdir.name + ".initial-memory-map.txt"),
        )
    else:
        try:
            eager_bundle = canonical.read_bundle(trace)
        except canonical.TraceError as error:
            raise ReplayError(
                f"invalid canonical replay trace: {error}"
            ) from error
        boundary_map = _write_boundary_map(
            eager_bundle,
            outdir.with_name(outdir.name + ".boundary-map.txt"),
        )
        initial_memory_map = _write_initial_memory_map(
            eager_bundle, trace,
            outdir.with_name(outdir.name + ".initial-memory-map.txt"),
        )
    if options.mode == "window":
        if options.window_manifest is not None:
            manifest = Path(options.window_manifest).resolve()
            if not manifest.is_file():
                raise ReplayError(f"window manifest is missing: {manifest}")
    run_options = argparse.Namespace(**vars(options))
    if lazy_bundle is not None:
        pipe_read, pipe_write = os.pipe()
        run_options.replay_trace = Path(
            f"/proc/{os.getpid()}/fd/{pipe_read}"
        )
        run_options.stream_mode = True

        def produce():
            try:
                with os.fdopen(pipe_write, "wb", buffering=0) as stream:
                    producer_evidence.update(
                        _emit_lazy_replay_stream(trace, stream)
                    )
            except BaseException as error:
                producer_errors.append(error)

        producer_thread = threading.Thread(
            target=produce, name="matched-lazy-trace-producer", daemon=True
        )
        producer_thread.start()
    else:
        run_options.replay_trace = replay_trace / "trace.bin"
    if materialized is not None:
        bind_materialized_window_selection(run_options, materialized)
    if boundary_map is not None:
        run_options.boundary_map = boundary_map
    if initial_memory_map is not None:
        run_options.initial_memory_map = initial_memory_map
    command = command_for(run_options)
    outdir.parent.mkdir(parents=True, exist_ok=True)
    driver_log = outdir.with_suffix(".dynamic.driver.log")
    completed = None
    launch_error = None
    try:
        with driver_log.open("x", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if options.timeout == 0 else options.timeout,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        launch_error = error
    finally:
        if pipe_read is not None:
            os.close(pipe_read)
        if producer_thread is not None:
            producer_thread.join(timeout=60)
    if launch_error is not None:
        raise ReplayError(f"gem5 replay launch failed: {launch_error}") from launch_error
    if producer_thread is not None and producer_thread.is_alive():
        raise ReplayError("lazy replay producer did not terminate")
    if producer_errors:
        raise ReplayError(f"lazy replay producer failed: {producer_errors[0]}")
    if completed.returncode != 0:
        raise ReplayError(
            f"gem5 replay exited {completed.returncode}; see {driver_log}"
        )
    if lazy_bundle is None:
        row = collect_run_evidence(
            outdir, system=options.system, trace=replay_trace,
            config=outdir / "config.ini",
            cxl_link_delay=options.cxl_link_delay,
        )
    else:
        widths = {"u32": 4, "u64": 8, "f32": 4, "f64": 8}
        required_bytes = sum(
            array.count * widths[array.element_type]
            for array in lazy_bundle.arrays
        )
        row = collect_run_evidence(
            outdir, system=options.system, trace=None,
            config=outdir / "config.ini", expected=producer_evidence,
            required_bytes=required_bytes,
            cxl_link_delay=options.cxl_link_delay,
        )
    fixed_command = None
    fixed_row = None
    fixed_outdir = None
    if materialized is not None:
        fixed_outdir = outdir.with_name(outdir.name + ".fixed")
        fixed_options = argparse.Namespace(**vars(options))
        fixed_options.outdir = fixed_outdir
        fixed_options.trace = materialized.fixed_root
        fixed_options.replay_trace = materialized.fixed_root / "trace.bin"
        bind_materialized_window_selection(fixed_options, materialized)
        fixed_options.window_manifest = (
            materialized.fixed_root / "trace.meta.json"
        ).resolve()
        fixed_options.measure_start_item = 0
        fixed_bundle = canonical.read_bundle(materialized.fixed_root)
        fixed_options.initial_memory_map = _write_initial_memory_map(
            fixed_bundle, materialized.fixed_root,
            fixed_outdir.with_name(
                fixed_outdir.name + ".initial-memory-map.txt"
            ),
        )
        fixed_options.boundary_map = _write_boundary_map(
            fixed_bundle,
            fixed_outdir.with_name(fixed_outdir.name + ".boundary-map.txt"),
        )
        fixed_command = command_for(fixed_options)
        _launch_gem5(
            fixed_command, outdir=fixed_outdir, timeout=options.timeout,
            label="fixed",
        )
        fixed_row = collect_run_evidence(
            fixed_outdir, system=options.system,
            trace=materialized.fixed_root,
            config=fixed_outdir / "config.ini", require_activity=False,
            cxl_link_delay=options.cxl_link_delay,
        )
        row = combine_window_evidence(
            row, fixed_row,
            fixed_trace=materialized.fixed_root / "trace.bin",
        )
    source_descriptor = (
        trace / "trace.v2.json"
        if (trace / "trace.v2.json").is_file()
        else trace / "trace.meta.json"
    )
    evidence = {
        "schema": 1,
        "status": "pass",
        "mode": options.mode,
        "system": options.system,
        "cxl_link_delay": options.cxl_link_delay,
        "cxl_link_delay_ticks": latency.ticks(options.cxl_link_delay),
        "trace": str(trace),
        "trace_meta_sha256": _sha256_file(source_descriptor),
        "trace_identity_sha256": trace_identity_sha256(trace),
        "binary_sha256": _sha256_file(options.binary),
        "gem5_sha256": _sha256_file(options.gem5),
        "config_sha256": _sha256_file(outdir / "config.ini"),
        "command": command,
        "row": row,
    }
    if materialized is not None:
        evidence["materialized_window"] = {
            **materialized_trace_record(materialized),
            "trace_sha256": _sha256_file(replay_trace / "trace.bin"),
        }
        evidence["fixed_replay"] = {
            "root": str(fixed_outdir),
            "command": fixed_command,
            "row": fixed_row,
            "trace_root": str(materialized.fixed_root),
            "trace_sha256": _sha256_file(
                materialized.fixed_root / "trace.bin"
            ),
            "config_sha256": _sha256_file(fixed_outdir / "config.ini"),
            "stats_sha256": _sha256_file(fixed_outdir / "stats.txt"),
            "result_sha256": _sha256_file(fixed_outdir / "result.json"),
        }
    if lazy_bundle is not None:
        evidence["functional_stream"] = producer_evidence
    (outdir / "evidence.json").write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return evidence


def main(argv=None):
    try:
        run(parse_args(argv))
    except ReplayError as error:
        print(f"MATCHED_REPLAY_FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
