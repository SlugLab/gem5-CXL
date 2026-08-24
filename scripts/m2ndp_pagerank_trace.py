#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate native scalar M2NDP traces for fixed-20 pull PageRank."""

import dataclasses
import json
import math
import os
import struct
from pathlib import Path

try:
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import m2ndp_artifacts as artifacts


IN_OFFSETS_ADDR = 0x8000_0000_0000
IN_NEIGHBORS_ADDR = 0x8100_0000_0000
OUT_DEGREE_ADDR = 0x8200_0000_0000
SCORES_A_ADDR = 0x8300_0000_0000
SCORES_B_ADDR = 0x8500_0000_0000
# Compatibility alias for diagnostic traces and existing strict dump tooling.
SCORES_ADDR = SCORES_A_ADDR
CONTRIB_ADDR = 0x8400_0000_0000
SCRATCHPAD_ADDR = 0x1000_0000_0000_0000
PACKET_SIZE = 32

UNIQUE_KERNELS = (
    "K0_INIT",
    "K1_META",
    "K2_CONTRIB",
    "K3_PULL_DAMP",
)


@dataclasses.dataclass(frozen=True)
class TraceResult:
    root: Path
    unique_kernels: tuple[str, ...]
    funcsim_launches: int
    ndpsim_launches: int
    measure_marker: str
    meta_path: Path


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def f32_sub(left, right):
    return f32(f32(left) - f32(right))


def f32_div(left, right):
    return f32(f32(left) / f32(right))


def float32_bits(value):
    return struct.unpack("<I", struct.pack("<f", f32(value)))[0]


def bits_float32(word):
    return struct.unpack("<f", struct.pack("<I", word))[0]


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _format_float_word(word):
    value = bits_float32(word)
    if not math.isfinite(value):
        raise artifacts.EvidenceError(
            f"non-finite float32 word 0x{word:08x} cannot be serialized"
        )
    text = format(value, ".9g")
    if struct.pack("<f", float(text)) != struct.pack("<I", word):
        raise artifacts.EvidenceError(
            f"float32 decimal round-trip changed 0x{word:08x}"
        )
    return text


def _write_array(stream, address, type_name, values, fmt, per_packet):
    stream.write("_META_\n")
    stream.write(type_name + "\n")
    stream.write("_DATA_\n")
    row = []
    row_index = 0
    for value in values:
        if fmt == "float32":
            encoded = _format_float_word(value)
        else:
            encoded = str(value)
        row.append(encoded)
        if len(row) == per_packet:
            stream.write(
                f"0x{address + row_index * PACKET_SIZE:x} "
                + " ".join(row)
                + "\n"
            )
            row.clear()
            row_index += 1
    if row:
        row.extend("0" for _ in range(per_packet - len(row)))
        stream.write(
            f"0x{address + row_index * PACKET_SIZE:x} "
            + " ".join(row)
            + "\n"
        )


def _write_memory_map(path, arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for address, type_name, values in arrays:
                if type_name == "int64":
                    _write_array(
                        stream, address, type_name, values, "int64", 4
                    )
                elif type_name == "int32":
                    _write_array(
                        stream, address, type_name, values, "int32", 8
                    )
                elif type_name == "float32":
                    _write_array(
                        stream, address, type_name, values, "float32", 8
                    )
                else:
                    raise artifacts.EvidenceError(
                        f"unsupported map type {type_name}"
                    )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_float32_map(path, base, count):
    words = {}
    mode = None
    with Path(path).open(encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if line == "_META_":
                mode = "type"
            elif mode == "type":
                mode = "float_data" if line == "float32" else "other"
            elif line == "_DATA_":
                if mode == "float_data":
                    mode = "float_rows"
                elif mode == "other":
                    mode = "other_rows"
            elif mode == "float_rows" and line:
                fields = line.split()
                address = int(fields[0], 16)
                for index, value in enumerate(fields[1:]):
                    words[address + index * 4] = float32_bits(float(value))
    result = []
    for index in range(count):
        address = base + index * 4
        if address not in words:
            raise artifacts.EvidenceError(
                f"float32 map missing address 0x{address:x}"
            )
        result.append(words[address])
    return result


def flip_float32_bit_for_test(path, base, *, index, bit):
    if index < 0 or bit < 0 or bit >= 32:
        raise ValueError("float32 test bit location is out of range")
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    target = base + index * 4
    mode = None
    matches = []
    for line_index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line == "_META_":
            mode = "type"
        elif mode == "type":
            mode = "float_data" if line == "float32" else "other"
        elif line == "_DATA_":
            mode = "float_rows" if mode == "float_data" else "other_rows"
        elif mode == "float_rows" and line:
            fields = line.split()
            address = int(fields[0], 16)
            word_index = (target - address) // 4
            if (
                target >= address
                and target < address + (len(fields) - 1) * 4
                and word_index >= 0
            ):
                before = float32_bits(float(fields[word_index + 1]))
                after = before ^ (1 << bit)
                fields[word_index + 1] = _format_float_word(after)
                lines[line_index] = " ".join(fields)
                matches.append((before, after))
    if len(matches) != 1:
        raise artifacts.EvidenceError(
            f"float32 test target match count is {len(matches)}, expected 1"
        )
    _atomic_write_text(path, "\n".join(lines) + "\n")
    return matches[0]


def _kernel(name, kernel_id, body):
    return (
        f"-kernel name = {name}\n"
        f"-kernel id = {kernel_id}\n\n"
        "KERNELBODY:\n"
        + "\n".join(body)
        + "\n"
    )


def _kernels():
    spad = SCRATCHPAD_ADDR
    return {
        "K0_INIT": _kernel(
            "K0_INIT",
            0,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "flw f0, 32(x31)",
                "srli x3, x2, 5",
                "bge x3, x11, .SKIP0",
                "slli x4, x3, 2",
                "add x4, x10, x4",
                "fsw f0, 0(x4)",
                ".SKIP0",
            ],
        ),
        "K1_META": _kernel(
            "K1_META",
            1,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "slli x3, x11, 3",
                "add x3, x10, x3",
                "ld x4, 0(x3)",
                "bne x4, x12, .SKIP0",
                "li x5, 1",
                ".SKIP0",
            ],
        ),
        "K2_CONTRIB": _kernel(
            "K2_CONTRIB",
            2,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "ld x13, 24(x31)",
                "srli x3, x2, 5",
                "bge x3, x13, .SKIP0",
                "slli x4, x3, 2",
                "add x5, x10, x4",
                "flw f0, 0(x5)",
                "add x5, x11, x4",
                "lw x6, 0(x5)",
                "fmv.w.x f1, x6",
                "fdiv f0, f0, f1",
                "add x5, x12, x4",
                "fsw f0, 0(x5)",
                ".SKIP0",
            ],
        ),
        "K3_PULL_DAMP": _kernel(
            "K3_PULL_DAMP",
            3,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "ld x13, 24(x31)",
                "ld x14, 32(x31)",
                "flw f2, 64(x31)",
                "flw f3, 68(x31)",
                "srli x3, x2, 5",
                "bge x3, x14, .SKIP0",
                "slli x4, x3, 3",
                "add x4, x10, x4",
                "ld x5, 0(x4)",
                "ld x6, 8(x4)",
                "li x7, 0",
                "fmv.w.x f0, x7",
                ".LOOP0",
                "bge x5, x6, .SKIP1",
                "slli x8, x5, 2",
                "add x8, x11, x8",
                "lw x9, 0(x8)",
                "slli x9, x9, 2",
                "add x9, x12, x9",
                "flw f1, 0(x9)",
                "fadd f0, f0, f1",
                "addi x5, x5, 1",
                "j .LOOP0",
                ".SKIP1",
                "fmul f0, f2, f0",
                "fadd f0, f3, f0",
                "slli x4, x3, 2",
                "add x4, x13, x4",
                "fsw f0, 0(x4)",
                ".SKIP0",
            ],
        ),
    }


def _formal_kernels():
    """Four-way kernels; row bounds change addressing, never FP order."""
    spad = SCRATCHPAD_ADDR
    return {
        "K0_INIT": _kernel(
            "K0_INIT", 0,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "flw f0, 32(x31)",
                "srli x3, x2, 5",
                "bge x3, x12, .SKIP0",
                "add x3, x3, x11",
                "slli x4, x3, 2",
                "add x4, x10, x4",
                "fsw f0, 0(x4)",
                ".SKIP0",
            ],
        ),
        "K1_META": _kernels()["K1_META"],
        "K2_CONTRIB": _kernel(
            "K2_CONTRIB", 2,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "ld x13, 24(x31)",
                "ld x14, 32(x31)",
                "srli x3, x2, 5",
                "bge x3, x14, .SKIP0",
                "add x3, x3, x13",
                "slli x4, x3, 2",
                "add x5, x10, x4",
                "flw f0, 0(x5)",
                "add x5, x11, x4",
                "lw x6, 0(x5)",
                "fmv.w.x f1, x6",
                "fdiv f0, f0, f1",
                "add x5, x12, x4",
                "fsw f0, 0(x5)",
                ".SKIP0",
            ],
        ),
        "K3_PULL_DAMP": _kernel(
            "K3_PULL_DAMP", 3,
            [
                f"li x31, {spad}",
                "ld x10, 0(x31)",
                "ld x11, 8(x31)",
                "ld x12, 16(x31)",
                "ld x13, 24(x31)",
                "ld x14, 32(x31)",
                "ld x15, 40(x31)",
                "flw f2, 64(x31)",
                "flw f3, 68(x31)",
                "srli x3, x2, 5",
                "bge x3, x15, .SKIP0",
                "add x3, x3, x14",
                "slli x4, x3, 3",
                "add x4, x10, x4",
                "ld x5, 0(x4)",
                "ld x6, 8(x4)",
                "li x7, 0",
                "fmv.w.x f0, x7",
                ".LOOP0",
                "bge x5, x6, .SKIP1",
                "slli x8, x5, 2",
                "add x8, x11, x8",
                "lw x9, 0(x8)",
                "slli x9, x9, 2",
                "add x9, x12, x9",
                "flw f1, 0(x9)",
                "fadd f0, f0, f1",
                "addi x5, x5, 1",
                "j .LOOP0",
                ".SKIP1",
                "fmul f0, f2, f0",
                "fadd f0, f3, f0",
                "slli x4, x3, 2",
                "add x4, x13, x4",
                "fsw f0, 0(x4)",
                ".SKIP0",
            ],
        ),
    }


def pr_static_partitions(rows, workers=4):
    if rows < 0 or workers <= 0:
        raise artifacts.EvidenceError("invalid static partition shape")
    quotient, remainder = divmod(rows, workers)
    bounds = []
    for worker in range(workers):
        begin = worker * quotient + min(worker, remainder)
        end = begin + quotient + (1 if worker < remainder else 0)
        bounds.append((begin, end))
    if bounds[0][0] != 0 or bounds[-1][1] != rows:
        raise artifacts.EvidenceError("static partitions do not cover rows")
    if any(bounds[i][1] != bounds[i + 1][0] for i in range(workers - 1)):
        raise artifacts.EvidenceError("static partitions are not contiguous")
    return tuple(bounds)


def _launch(kernel_id, base, size, int_args, float_args=()):
    total_args = len(int_args) + len(float_args)
    smem_size = 96
    fields = [
        "1",
        str(kernel_id),
        f"0x{base:x}",
        f"0x{size:x}",
        f"0x{smem_size:x}",
        f"0x{total_args * 8:x}",
    ]
    fields.extend(f"0x{argument:x}" for argument in int_args)
    if float_args:
        fields.append("FP32")
        fields.extend(format(f32(value), ".9g") for value in float_args)
    return " ".join(fields) + "\n"


def _trial_names(iterations, *, measured):
    first = "K0_INIT_TRIAL1" if measured else "K0_INIT"
    names = [first, "K1_META"]
    for _ in range(iterations):
        names.extend(("K2_CONTRIB", "K3_PULL_DAMP"))
    return names


def _formal_trial_records(
    *, trial, iterations, bounds, node_count, edge_count, init_score,
    damping, base_score,
):
    records = []
    for partition, (begin, end) in enumerate(bounds):
        count = end - begin
        name = f"K0_INIT_TRIAL{trial}_PART{partition}"
        records.append((
            name,
            "K0_INIT",
            _launch(
                0,
                SCORES_A_ADDR + begin * PACKET_SIZE,
                count * PACKET_SIZE,
                (SCORES_A_ADDR, begin, count),
                (init_score,),
            ),
        ))
    records.append((
        f"K1_META_TRIAL{trial}",
        "K1_META",
        _launch(
            1, IN_OFFSETS_ADDR, PACKET_SIZE,
            (IN_OFFSETS_ADDR, node_count, edge_count),
        ),
    ))
    for iteration in range(iterations):
        read_scores = SCORES_A_ADDR if iteration % 2 == 0 else SCORES_B_ADDR
        write_scores = SCORES_B_ADDR if iteration % 2 == 0 else SCORES_A_ADDR
        iteration_tag = "" if iteration == 0 else f"_ITER{iteration}"
        for partition, (begin, end) in enumerate(bounds):
            count = end - begin
            name = (
                f"K2_CONTRIB_TRIAL{trial}{iteration_tag}_PART{partition}"
            )
            records.append((
                name,
                "K2_CONTRIB",
                _launch(
                    2,
                    CONTRIB_ADDR + begin * PACKET_SIZE,
                    count * PACKET_SIZE,
                    (
                        read_scores, OUT_DEGREE_ADDR, CONTRIB_ADDR,
                        begin, count,
                    ),
                ),
            ))
        for partition, (begin, end) in enumerate(bounds):
            count = end - begin
            name = (
                f"K3_PULL_DAMP_TRIAL{trial}_ITER{iteration}_PART{partition}"
            )
            records.append((
                name,
                "K3_PULL_DAMP",
                _launch(
                    3,
                    write_scores + begin * PACKET_SIZE,
                    count * PACKET_SIZE,
                    (
                        IN_OFFSETS_ADDR, IN_NEIGHBORS_ADDR, CONTRIB_ADDR,
                        write_scores, begin, count,
                    ),
                    (damping, base_score),
                ),
            ))
    return records


def _formal_timing_records(records):
    grouped = []
    cursor = 0
    while cursor < len(records):
        name, kernel, launch = records[cursor]
        if kernel == "K1_META":
            grouped.append((name, kernel, launch, 1))
            cursor += 1
            continue
        prefix = name.rsplit("_PART", 1)[0]
        phase = records[cursor:cursor + 4]
        if (
            len(phase) != 4
            or any(row[1] != kernel for row in phase)
            or [row[0] for row in phase]
            != [f"{prefix}_PART{index}" for index in range(4)]
        ):
            raise artifacts.EvidenceError(
                "formal timing phase is not four contiguous partitions: "
                f"{prefix}"
            )
        grouped.append((
            f"{prefix}_GROUP",
            kernel,
            "".join(row[2] for row in phase),
            4,
        ))
        cursor += 4
    return grouped


def read_trace_meta(path):
    return json.loads(Path(path).read_text())


def validate_trace_binding(
    meta, *, profile, profile_manifest_sha256, cxl_link_delay,
    vanilla_raw_sha256, directed_edges
):
    expected = {
        "profile": profile.name,
        "profile_manifest_sha256": profile_manifest_sha256,
        "cxl_link_delay": cxl_link_delay,
        "vanilla_raw_sha256": vanilla_raw_sha256,
        "graph_sha256": profile.graph_sha256,
        "num_nodes": profile.num_nodes,
        "num_directed_edges": directed_edges,
        "trials": profile.trials,
        "measured_trial": profile.measured_trial,
        "iterations": profile.page_rank_iterations,
        "stage_sequence": list(UNIQUE_KERNELS),
        "measure_marker": (
            "K2_CONTRIB_TRIAL1_GROUP"
            if profile.name == "pr-offload-4thread-1us"
            else "K0_INIT_TRIAL1"
        ),
    }
    if profile.name == "pr-offload-4thread-1us":
        expected.update(
            logical_partitions=4,
            partition_bounds=[
                list(bound)
                for bound in pr_static_partitions(profile.num_nodes, 4)
            ],
            double_buffered=True,
        )
    for field, value in expected.items():
        if meta.get(field) != value:
            raise artifacts.EvidenceError(
                f"trace {field}={meta.get(field)!r}, expected {value!r}"
            )
    kernels = meta.get("kernel_sha256")
    if not isinstance(kernels, dict) or set(kernels) != set(UNIQUE_KERNELS):
        raise artifacts.EvidenceError("trace kernel hash set is incomplete")
    for name, digest in kernels.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise artifacts.EvidenceError(
                f"trace kernel hash is invalid for {name}"
            )
    return meta


def generate_trace(
    *, bundle, reference, outdir, trials=2, iterations=20,
    profile=None, profile_manifest_sha256=None, cxl_link_delay=None,
    vanilla_raw_sha256=None,
):
    if trials != 2 or iterations != 20:
        raise artifacts.EvidenceError(
            "publication trace requires two trials and 20 iterations"
        )
    reference_header, reference_words = reference
    if reference_header["graph_sha256"] != bundle.meta.graph_sha256:
        raise artifacts.EvidenceError("reference graph hash mismatch")
    if reference_header["num_nodes"] != bundle.meta.num_nodes:
        raise artifacts.EvidenceError("reference node count mismatch")
    if reference_header["iterations"] != iterations:
        raise artifacts.EvidenceError("reference iteration count mismatch")
    if profile is not None:
        if (
            profile.graph_sha256 != bundle.meta.graph_sha256
            or profile.num_nodes != bundle.meta.num_nodes
            or profile.trials != trials
            or profile.page_rank_iterations != iterations
            or profile.measured_trial != 1
        ):
            raise artifacts.EvidenceError(
                "trace inputs do not match the experiment profile"
            )
        for value, label in (
            (profile_manifest_sha256, "profile manifest"),
            (vanilla_raw_sha256, "Vanilla raw vector"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise artifacts.EvidenceError(f"{label} SHA-256 is invalid")
        if not isinstance(cxl_link_delay, str) or not cxl_link_delay:
            raise artifacts.EvidenceError("trace CXL latency is missing")

    outdir = Path(outdir)
    trace_dir = outdir / "0"
    trace_dir.mkdir(parents=True, exist_ok=True)
    node_count = bundle.meta.num_nodes
    edge_count = bundle.meta.num_directed_edges
    init_score = f32_div(f32(1.0), f32(node_count))
    damping = f32(0.85)
    base_score = f32_div(
        f32_sub(f32(1.0), damping), f32(node_count)
    )

    formal = (
        profile is not None
        and profile.name == "pr-offload-4thread-1us"
    )
    if formal and getattr(profile, "logical_partitions", None) != 4:
        raise artifacts.EvidenceError(
            "formal trace requires four logical partitions"
        )

    kernels = _formal_kernels() if formal else _kernels()
    launches = {
        "K0_INIT": _launch(
            0,
            SCORES_ADDR,
            node_count * PACKET_SIZE,
            (SCORES_ADDR, node_count),
            (init_score,),
        ),
        "K1_META": _launch(
            1,
            IN_OFFSETS_ADDR,
            PACKET_SIZE,
            (IN_OFFSETS_ADDR, node_count, edge_count),
        ),
        "K2_CONTRIB": _launch(
            2,
            CONTRIB_ADDR,
            node_count * PACKET_SIZE,
            (SCORES_ADDR, OUT_DEGREE_ADDR, CONTRIB_ADDR, node_count),
        ),
        "K3_PULL_DAMP": _launch(
            3,
            SCORES_ADDR,
            node_count * PACKET_SIZE,
            (
                IN_OFFSETS_ADDR,
                IN_NEIGHBORS_ADDR,
                CONTRIB_ADDR,
                SCORES_ADDR,
                node_count,
            ),
            (damping, base_score),
        ),
    }
    for name in UNIQUE_KERNELS:
        _atomic_write_text(trace_dir / f"{name}.traceg", kernels[name])
        if not formal:
            _atomic_write_text(
                trace_dir / f"{name}_launch.txt", launches[name]
            )
    if not formal:
        _atomic_write_text(
            trace_dir / "K0_INIT_TRIAL1.traceg", kernels["K0_INIT"]
        )
        _atomic_write_text(
            trace_dir / "K0_INIT_TRIAL1_launch.txt", launches["K0_INIT"]
        )

    memory_arrays = [
        (IN_OFFSETS_ADDR, "int64", bundle.in_offsets),
        (IN_NEIGHBORS_ADDR, "int32", bundle.in_neighbors),
        (OUT_DEGREE_ADDR, "int32", bundle.out_degree),
        (SCORES_A_ADDR, "float32", (0 for _ in range(node_count))),
    ]
    if formal:
        memory_arrays.append(
            (SCORES_B_ADDR, "float32", (0 for _ in range(node_count)))
        )
    memory_arrays.append(
        (CONTRIB_ADDR, "float32", (0 for _ in range(node_count)))
    )
    _write_memory_map(
        trace_dir / "K0_INIT_input.data",
        memory_arrays,
    )
    _write_memory_map(
        trace_dir / "K3_PULL_DAMP_output.data",
        ((SCORES_A_ADDR, "float32", reference_words),),
    )

    partition_bounds = pr_static_partitions(node_count, 4) if formal else ()
    if formal:
        trial_records = [
            _formal_trial_records(
                trial=trial,
                iterations=iterations,
                bounds=partition_bounds,
                node_count=node_count,
                edge_count=edge_count,
                init_score=init_score,
                damping=damping,
                base_score=base_score,
            )
            for trial in range(trials)
        ]
        for records in trial_records:
            for name, kernel_name, launch in records:
                _atomic_write_text(
                    trace_dir / f"{name}.traceg", kernels[kernel_name]
                )
                _atomic_write_text(
                    trace_dir / f"{name}_launch.txt", launch
                )
        timing_records = [
            _formal_timing_records(records) for records in trial_records
        ]
        for records in timing_records:
            for name, kernel_name, launch, _ in records:
                _atomic_write_text(
                    trace_dir / f"{name}.traceg", kernels[kernel_name]
                )
                _atomic_write_text(
                    trace_dir / f"{name}_launch.txt", launch
                )
        functional_names = [name for name, _, _ in trial_records[0]]
        timing_names = [
            name for records in timing_records for name, _, _, _ in records
        ]
    else:
        functional_names = _trial_names(iterations, measured=False)
        timing_names = functional_names + _trial_names(
            iterations, measured=True
        )
    if formal:
        timing_input = trace_dir / f"{timing_names[0]}_input.data"
        timing_output = trace_dir / f"{timing_names[-1]}_output.data"
        timing_input.unlink(missing_ok=True)
        timing_output.unlink(missing_ok=True)
        os.link(trace_dir / "K0_INIT_input.data", timing_input)
        os.link(trace_dir / "K3_PULL_DAMP_output.data", timing_output)
    sequence_lines = []
    for name in functional_names:
        sequence_lines.append(
            str((trace_dir / f"{name}.traceg").resolve())
            + "\t"
            + (trace_dir / f"{name}_launch.txt").read_text().strip()
        )
    _atomic_write_text(
        outdir / "funcsim.sequence",
        "\n".join(sequence_lines) + "\n",
    )
    _atomic_write_text(
        trace_dir / "kernelslist.g", "\n".join(timing_names) + "\n"
    )
    _atomic_write_text(
        outdir / "functional.config",
        "num_ndp_units = 32\n"
        "num_x_registers = 32\n"
        "num_f_registers = 32\n"
        "num_v_registers = 32\n"
        "spad_size = 102400\n"
        "use_synthetic_memory = 0\n",
    )

    meta = {
        "schema": 1,
        "graph_sha256": bundle.meta.graph_sha256,
        "num_nodes": node_count,
        "num_directed_edges": edge_count,
        "directed": bundle.meta.directed,
        "iterations": iterations,
        "trials": trials,
        "measured_trial": 1,
        "stage_sequence": list(UNIQUE_KERNELS),
        "funcsim_launches": len(functional_names),
        "ndpsim_launches": len(timing_names),
        "measure_marker": (
            "K2_CONTRIB_TRIAL1_GROUP" if formal else "K0_INIT_TRIAL1"
        ),
        "timing_commands_per_trial": (
            len(timing_records[0]) if formal else len(functional_names)
        ),
        "timing_launch_records_per_trial": (
            sum(record[3] for record in timing_records[0])
            if formal else len(functional_names)
        ),
        "logical_partitions": 4 if formal else 1,
        "partition_bounds": (
            [list(bound) for bound in partition_bounds]
            if formal else [[0, node_count]]
        ),
        "double_buffered": formal,
        "max_kernel_launch": 128,
        "packet_size": PACKET_SIZE,
        "init_score_bits": f"0x{float32_bits(init_score):08x}",
        "damping_bits": f"0x{float32_bits(damping):08x}",
        "base_score_bits": f"0x{float32_bits(base_score):08x}",
        "reference_sha256": reference_header["binary_sha256"],
        "profile": profile.name if profile is not None else "",
        "profile_manifest_sha256": profile_manifest_sha256 or "",
        "cxl_link_delay": cxl_link_delay or "",
        "vanilla_raw_sha256": vanilla_raw_sha256 or "",
        "kernel_sha256": {
            name: artifacts.sha256_file(trace_dir / f"{name}.traceg")
            for name in UNIQUE_KERNELS
        },
        "file_sha256": {
            str(path.relative_to(outdir)): artifacts.sha256_file(path)
            for path in sorted(outdir.rglob("*"))
            if path.is_file() and path.name != "trace.meta.json"
        },
    }
    meta_path = outdir / "trace.meta.json"
    artifacts.atomic_write_json(meta_path, meta)
    if profile is not None:
        validate_trace_binding(
            meta,
            profile=profile,
            profile_manifest_sha256=profile_manifest_sha256,
            cxl_link_delay=cxl_link_delay,
            vanilla_raw_sha256=vanilla_raw_sha256,
            directed_edges=edge_count,
        )
    return TraceResult(
        outdir,
        UNIQUE_KERNELS,
        len(functional_names),
        len(timing_names),
        "K2_CONTRIB_TRIAL1_GROUP" if formal else "K0_INIT_TRIAL1",
        meta_path,
    )
