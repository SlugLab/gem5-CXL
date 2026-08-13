#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Backend-neutral canonical work trace and raw-result bundle."""

import dataclasses
import enum
import hashlib
import os
import re
import struct
import tempfile
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
except ImportError:
    import cross_system_contract as contract


TRACE_STRUCT = struct.Struct("<H H I Q Q Q Q Q Q")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class TraceError(RuntimeError):
    """A trace or raw-result bundle violates canonical semantics."""


class Opcode(enum.IntEnum):
    LOAD_U32 = 1
    LOAD_U64 = 2
    LOAD_F32 = 3
    LOAD_F64 = 4
    STORE_U32 = 5
    STORE_U64 = 6
    STORE_F32 = 7
    STORE_F64 = 8
    F32_ADD = 9
    F32_MUL = 10
    F32_DIV = 11
    F64_ADD = 12
    I64_ADD = 13
    I64_MIN = 14
    BARRIER = 15
    COMMIT = 16
    F64_MAX = 17
    F64_MUL = 18
    F64_SUB = 19
    F64_DIV = 20
    F64_SQRT = 21
    F64_MOV = 22
    F64_ABS = 23


def _unsigned(value, bits, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= 1 << bits
    ):
        raise TraceError(f"{label} is outside uint{bits}")
    return value


@dataclasses.dataclass(frozen=True)
class Operation:
    phase: int
    opcode: Opcode
    work_item: int
    sequence: int
    address: int
    operand0: int
    operand1: int
    result: int

    def __post_init__(self):
        _unsigned(self.phase, 16, "phase")
        if not isinstance(self.opcode, Opcode):
            raise TraceError("opcode is not canonical")
        for field in (
            "work_item",
            "sequence",
            "address",
            "operand0",
            "operand1",
            "result",
        ):
            _unsigned(getattr(self, field), 64, field)


@dataclasses.dataclass(frozen=True)
class TraceBundle:
    meta: dict
    operations: tuple[Operation, ...]
    outputs: dict[str, tuple[int, ...]]


def encode_operations(operations):
    payload = bytearray()
    for expected_sequence, operation in enumerate(operations):
        if not isinstance(operation, Operation):
            raise TraceError("trace contains a non-operation value")
        if operation.sequence != expected_sequence:
            raise TraceError(
                f"operation sequence {operation.sequence} != {expected_sequence}"
            )
        payload.extend(
            TRACE_STRUCT.pack(
                operation.phase,
                int(operation.opcode),
                0,
                operation.work_item,
                operation.sequence,
                operation.address,
                operation.operand0,
                operation.operand1,
                operation.result,
            )
        )
    return bytes(payload)


def operations_sha256(operations):
    """Hash an ordered operation stream without materializing its encoding."""
    digest = hashlib.sha256()
    for expected_sequence, operation in enumerate(operations):
        if not isinstance(operation, Operation):
            raise TraceError("trace contains a non-operation value")
        if operation.sequence != expected_sequence:
            raise TraceError(
                f"operation sequence {operation.sequence} != "
                f"{expected_sequence}"
            )
        digest.update(TRACE_STRUCT.pack(
            operation.phase,
            int(operation.opcode),
            0,
            operation.work_item,
            operation.sequence,
            operation.address,
            operation.operand0,
            operation.operand1,
            operation.result,
        ))
    return digest.hexdigest()


def decode_operations(payload):
    if len(payload) % TRACE_STRUCT.size:
        raise TraceError(
            f"trace size must be a multiple of {TRACE_STRUCT.size} bytes"
        )
    operations = []
    for index, fields in enumerate(TRACE_STRUCT.iter_unpack(payload)):
        phase, opcode_value, reserved, work_item, sequence, address, operand0, operand1, result = fields
        if reserved != 0:
            raise TraceError(f"record {index} reserved field is nonzero")
        try:
            opcode = Opcode(opcode_value)
        except ValueError as error:
            raise TraceError(
                f"record {index} has unknown opcode {opcode_value}"
            ) from error
        operation = Operation(
            phase,
            opcode,
            work_item,
            sequence,
            address,
            operand0,
            operand1,
            result,
        )
        if sequence != index:
            raise TraceError(f"record {index} sequence is {sequence}")
        operations.append(operation)
    return tuple(operations)


def validate_translation(reference, actual):
    reference = tuple(reference)
    actual = tuple(actual)
    if len(reference) != len(actual):
        raise TraceError(
            f"translation length {len(actual)} != reference {len(reference)}"
        )
    for index, (expected, observed) in enumerate(zip(reference, actual)):
        if expected.sequence != observed.sequence or observed.sequence != index:
            raise TraceError(f"translation sequence differs at record {index}")
        if expected != observed:
            raise TraceError(f"translation operation differs at sequence {index}")
    return actual


def compare_words(expected, actual, label, *, word_bits):
    if word_bits not in (32, 64):
        raise TraceError("word width must be 32 or 64")
    expected = tuple(expected)
    actual = tuple(actual)
    if len(expected) != len(actual):
        raise TraceError(
            f"{label} length {len(actual)} != expected {len(expected)}"
        )
    width = word_bits // 4
    for index, (want, got) in enumerate(zip(expected, actual)):
        _unsigned(want, word_bits, f"{label}[{index}] expected")
        _unsigned(got, word_bits, f"{label}[{index}] actual")
        if want != got:
            raise TraceError(
                f"{label}[{index}] expected 0x{want:0{width}x} "
                f"actual 0x{got:0{width}x}"
            )
    return True


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path, payload):
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


def _require_digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TraceError(f"{label} SHA-256 is invalid")


def _validate_meta(meta):
    required = {
        "schema",
        "workload",
        "input_sha256",
        "source_sha256",
        "binary_sha256",
        "config_sha256",
        "phases",
        "output_boundaries",
    }
    if not isinstance(meta, dict) or not required.issubset(meta):
        raise TraceError("trace metadata fields are incomplete")
    if meta["schema"] != 1:
        raise TraceError("trace metadata schema must be 1")
    if not isinstance(meta["workload"], str) or not meta["workload"]:
        raise TraceError("trace workload is invalid")
    for field in (
        "input_sha256",
        "source_sha256",
        "binary_sha256",
        "config_sha256",
    ):
        _require_digest(meta[field], field.removesuffix("_sha256"))
    if not isinstance(meta["phases"], list) or not meta["phases"]:
        raise TraceError("trace phases are invalid")
    if not isinstance(meta["output_boundaries"], dict):
        raise TraceError("output boundaries are invalid")


def _safe_name(name):
    result = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if not result or result in {".", ".."}:
        raise TraceError(f"output boundary name is invalid: {name!r}")
    return result


def _encode_words(values, word_bits, label):
    values = tuple(values)
    for index, value in enumerate(values):
        _unsigned(value, word_bits, f"{label}[{index}]")
    code = "I" if word_bits == 32 else "Q"
    return struct.pack(f"<{len(values)}{code}", *values)


def _decode_words(payload, word_bits, label):
    if word_bits not in (32, 64):
        raise TraceError(f"{label} word width must be 32 or 64")
    bytes_per_word = word_bits // 8
    if len(payload) % bytes_per_word:
        raise TraceError(f"{label} byte length is not uint{word_bits} aligned")
    code = "I" if word_bits == 32 else "Q"
    count = len(payload) // bytes_per_word
    return tuple(struct.unpack(f"<{count}{code}", payload))


def write_bundle(root, meta, operations, outputs):
    root = Path(root)
    if (root / "trace.meta.json").exists():
        raise TraceError("trace bundle already exists")
    _validate_meta(meta)
    boundaries = meta["output_boundaries"]
    if set(outputs) != set(boundaries):
        raise TraceError("output boundary set differs from metadata")
    trace_payload = encode_operations(tuple(operations))
    _atomic_write(root / "trace.bin", trace_payload)
    output_records = {}
    used_paths = set()
    for name, specification in boundaries.items():
        if not isinstance(specification, dict):
            raise TraceError(f"output boundary {name} is invalid")
        word_bits = specification.get("word_bits")
        count = specification.get("count")
        if word_bits not in (32, 64) or count != len(outputs[name]):
            raise TraceError(f"output boundary {name} shape differs")
        suffix = "u32" if word_bits == 32 else "u64"
        relative = Path("results") / f"{_safe_name(name)}.{suffix}"
        if relative in used_paths:
            raise TraceError("output boundary filenames collide")
        used_paths.add(relative)
        payload = _encode_words(outputs[name], word_bits, name)
        _atomic_write(root / relative, payload)
        output_records[name] = {
            "path": relative.as_posix(),
            "sha256": _sha256_bytes(payload),
            "word_bits": word_bits,
            "count": count,
        }
    final_meta = {
        **meta,
        "trace_path": "trace.bin",
        "trace_sha256": _sha256_bytes(trace_payload),
        "trace_record_bytes": TRACE_STRUCT.size,
        "trace_records": len(trace_payload) // TRACE_STRUCT.size,
        "outputs": output_records,
    }
    contract.atomic_write_json(root / "trace.meta.json", final_meta)
    return root / "trace.meta.json"


def read_bundle(root):
    root = Path(root)
    meta = contract.load_json(root / "trace.meta.json")
    _validate_meta(meta)
    if meta.get("trace_record_bytes") != TRACE_STRUCT.size:
        raise TraceError("trace record size differs")
    trace_path = root / meta.get("trace_path", "")
    try:
        trace_payload = trace_path.read_bytes()
    except OSError as error:
        raise TraceError(f"cannot read trace payload: {error}") from error
    if _sha256_bytes(trace_payload) != meta.get("trace_sha256"):
        raise TraceError("trace SHA-256 differs")
    operations = decode_operations(trace_payload)
    if len(operations) != meta.get("trace_records"):
        raise TraceError("trace record count differs")
    output_records = meta.get("outputs")
    if not isinstance(output_records, dict):
        raise TraceError("trace output records are missing")
    outputs = {}
    for name, record in output_records.items():
        path = root / record["path"]
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise TraceError(f"cannot read output {name}: {error}") from error
        if _sha256_bytes(payload) != record.get("sha256"):
            raise TraceError(f"output {name} SHA-256 differs")
        values = _decode_words(payload, record.get("word_bits"), name)
        if len(values) != record.get("count"):
            raise TraceError(f"output {name} element count differs")
        outputs[name] = values
    return TraceBundle(meta, operations, outputs)
