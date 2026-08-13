#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Lossless bounded-memory canonical work-trace descriptors."""

import dataclasses
import hashlib
import json
import mmap
import os
import pathlib
import re
import struct
import tempfile

try:
    from scripts import canonical_work_trace as canonical
except ImportError:
    import canonical_work_trace as canonical


class LazyTraceError(RuntimeError):
    """A schema-2 descriptor or expansion violates canonical semantics."""


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ELEMENTS = {
    "u32": (4, "I"),
    "u64": (8, "Q"),
    "f32": (4, "f"),
    "f64": (8, "d"),
}
_MEMORY_OPCODE_TYPES = {
    canonical.Opcode.LOAD_U32: "u32",
    canonical.Opcode.LOAD_U64: "u64",
    canonical.Opcode.LOAD_F32: "f32",
    canonical.Opcode.LOAD_F64: "f64",
    canonical.Opcode.STORE_U32: "u32",
    canonical.Opcode.STORE_U64: "u64",
    canonical.Opcode.STORE_F32: "f32",
    canonical.Opcode.STORE_F64: "f64",
}
_SCALAR_BASE = 0x7000000000000000


def _default_scalar_addresses(scalars):
    return {
        name: _SCALAR_BASE + 8 * index
        for index, name in enumerate(sorted(scalars))
    }


def _scalar_names(meta, invocations):
    names = set(meta.get("initial_scalars", {}))
    for invocation in invocations:
        parameters = invocation.parameters
        for key in (
            "result", "result_xz", "result_zz", "norm3", "zeta",
            "snapshot", "rnm2", "rnmu",
        ):
            value = parameters.get(key)
            if isinstance(value, str):
                names.add(value)
        for key in ("zero", "results"):
            names.update(
                value for value in parameters.get(key, [])
                if isinstance(value, str)
            )
    return names


def _uint(value, bits, label):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value >= 1 << bits
    ):
        raise LazyTraceError(f"{label} is outside uint{bits}")
    return value


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.-]*", value
    ):
        raise LazyTraceError(f"{label} is invalid")
    return value


def _digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise LazyTraceError(f"{label} SHA-256 is invalid")
    return value


def _validate_parameter(value, label):
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise LazyTraceError(
            f"{label} must use names or raw integer values"
        )
    if isinstance(value, int):
        if value < -(1 << 63) or value >= 1 << 64:
            raise LazyTraceError(f"{label} integer is outside canonical range")
        return
    if isinstance(value, str):
        _name(value, label)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_parameter(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _name(key, f"{label} key")
            _validate_parameter(item, f"{label}.{key}")
        return
    raise LazyTraceError(f"{label} must use names or raw integer values")


@dataclasses.dataclass(frozen=True)
class ArrayImage:
    name: str
    role: str
    element_type: str
    count: int
    logical_base: int
    path: str
    sha256: str

    def __post_init__(self):
        _name(self.name, "array name")
        if self.role not in {"input", "state"}:
            raise LazyTraceError("array role is invalid")
        if self.element_type not in _ELEMENTS:
            raise LazyTraceError("array element type is invalid")
        _uint(self.count, 64, "array count")
        if self.count == 0:
            raise LazyTraceError("array count is zero")
        _uint(self.logical_base, 64, "array logical base")
        if self.logical_base == 0:
            raise LazyTraceError("array logical base is zero")
        if not isinstance(self.path, str) or not self.path:
            raise LazyTraceError("array path is invalid")
        _digest(self.sha256, "array")
        size = _ELEMENTS[self.element_type][0]
        if self.logical_base + self.count * size > 1 << 64:
            raise LazyTraceError("array logical range overflows uint64")


@dataclasses.dataclass(frozen=True)
class Invocation:
    ordinal: int
    phase: int
    kernel: str
    iteration: int
    work_items: int
    parameters: dict

    def __post_init__(self):
        _uint(self.ordinal, 64, "invocation ordinal")
        _uint(self.phase, 16, "invocation phase")
        _name(self.kernel, "invocation kernel")
        _uint(self.iteration, 64, "invocation iteration")
        _uint(self.work_items, 64, "invocation work items")
        if not isinstance(self.parameters, dict):
            raise LazyTraceError("invocation parameters are invalid")
        _validate_parameter(self.parameters, "invocation parameters")


@dataclasses.dataclass(frozen=True)
class LazyBundle:
    root: pathlib.Path
    meta: dict
    arrays: tuple
    invocations: tuple
    dynamic_work: dict


def _canonical_payload(meta, arrays, invocations, dynamic_work):
    return {
        "meta": meta,
        "arrays": [dataclasses.asdict(value) for value in arrays],
        "invocations": [dataclasses.asdict(value) for value in invocations],
        "dynamic_work": dynamic_work,
    }


def _atomic_json(path, value):
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_meta(meta):
    if not isinstance(meta, dict) or meta.get("schema") != 2:
        raise LazyTraceError("lazy trace metadata schema must be 2")
    _name(meta.get("workload"), "workload")
    for field in ("source_sha256", "binary_sha256", "config_sha256"):
        _digest(meta.get(field), field.removesuffix("_sha256"))
    scalars = meta.get("initial_scalars", {})
    addresses = meta.get("scalar_addresses", {})
    if not isinstance(scalars, dict) or not isinstance(addresses, dict):
        raise LazyTraceError("lazy scalar memory contract is invalid")
    if set(scalars) != set(addresses):
        raise LazyTraceError("lazy scalar address set differs from initial state")
    used = set()
    for name, value in scalars.items():
        _name(name, "scalar name")
        _uint(value, 64, f"initial scalar {name}")
        address = addresses[name]
        _uint(address, 64, f"scalar address {name}")
        if address % 8 or address in used:
            raise LazyTraceError("lazy scalar addresses overlap or are unaligned")
        used.add(address)


def _validate_dynamic_work(value):
    if not isinstance(value, dict):
        raise LazyTraceError("dynamic work is invalid")
    _uint(value.get("primitive_records"), 64, "dynamic primitive records")


def _array_path(root, relative):
    path = pathlib.Path(relative)
    if path.is_absolute():
        raise LazyTraceError("array path escapes bundle root")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise LazyTraceError("array path escapes bundle root") from error
    return resolved


def _validate_arrays(root, arrays):
    names = set()
    ranges = []
    for array in arrays:
        if array.name in names:
            raise LazyTraceError(f"duplicate array name {array.name}")
        names.add(array.name)
        size = _ELEMENTS[array.element_type][0]
        ranges.append((array.logical_base, array.logical_base + array.count * size,
                       array.name))
        path = _array_path(root, array.path)
        try:
            payload_size = path.stat().st_size
        except OSError as error:
            raise LazyTraceError(f"cannot inspect array {array.name}: {error}") from error
        if payload_size != array.count * size:
            raise LazyTraceError(f"array {array.name} byte count differs")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != array.sha256:
            raise LazyTraceError(f"array {array.name} SHA-256 differs")
    ranges.sort()
    for left, right in zip(ranges, ranges[1:]):
        if left[1] > right[0]:
            raise LazyTraceError(
                f"logical ranges overlap: {left[2]} and {right[2]}"
            )


def _validate_scalar_ranges(meta, arrays):
    ranges = [
        (array.logical_base,
         array.logical_base + array.count * _ELEMENTS[array.element_type][0],
         array.name)
        for array in arrays
    ]
    for name, start in meta.get("scalar_addresses", {}).items():
        if start > (1 << 64) - 8:
            raise LazyTraceError(f"scalar {name} address range wraps")
        ranges.append((start, start + 8, f"scalar.{name}"))
    ranges.sort()
    for left, right in zip(ranges, ranges[1:]):
        if left[1] > right[0]:
            raise LazyTraceError(
                f"logical ranges overlap: {left[2]} and {right[2]}"
            )


def _validate_invocations(invocations):
    for expected, invocation in enumerate(invocations):
        if invocation.ordinal != expected:
            raise LazyTraceError("invocation ordinals are not contiguous")


def write_bundle(root, meta, arrays, invocations, dynamic_work):
    root = pathlib.Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "trace.v2.json"
    if path.exists():
        raise LazyTraceError("lazy trace bundle already exists")
    meta = dict(meta)
    invocations = tuple(invocations)
    initial_scalars = dict(meta.get("initial_scalars", {}))
    for name in _scalar_names(meta, invocations):
        initial_scalars.setdefault(name, 0)
    meta["initial_scalars"] = initial_scalars
    meta.setdefault(
        "scalar_addresses",
        _default_scalar_addresses(meta.get("initial_scalars", {})),
    )
    arrays = tuple(arrays)
    _validate_meta(meta)
    _validate_dynamic_work(dynamic_work)
    _validate_arrays(root, arrays)
    _validate_scalar_ranges(meta, arrays)
    _validate_invocations(invocations)
    _atomic_json(path, _canonical_payload(
        dict(meta), arrays, invocations, dict(dynamic_work)
    ))
    return path


def read_bundle(root):
    root = pathlib.Path(root).resolve()
    try:
        value = json.loads((root / "trace.v2.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LazyTraceError(f"cannot read lazy trace descriptor: {error}") from error
    try:
        meta = value["meta"]
        arrays = tuple(ArrayImage(**row) for row in value["arrays"])
        invocations = tuple(Invocation(**row) for row in value["invocations"])
        dynamic_work = value["dynamic_work"]
    except (KeyError, TypeError) as error:
        raise LazyTraceError("lazy trace descriptor fields are incomplete") from error
    _validate_meta(meta)
    _validate_dynamic_work(dynamic_work)
    _validate_arrays(root, arrays)
    _validate_scalar_ranges(meta, arrays)
    _validate_invocations(invocations)
    return LazyBundle(root, meta, arrays, invocations, dynamic_work)


class MappedState:
    def __init__(self, bundle):
        self._bundle = bundle
        self._files = {}
        self._maps = {}
        self._arrays = {array.name: array for array in bundle.arrays}
        initial_scalars = bundle.meta.get("initial_scalars", {})
        if not isinstance(initial_scalars, dict):
            raise LazyTraceError("initial scalar map is invalid")
        _validate_parameter(initial_scalars, "initial scalars")
        self._scalars = dict(initial_scalars)
        for name in _scalar_names(bundle.meta, bundle.invocations):
            self._scalars.setdefault(name, 0)
        self._scalar_addresses = self._bundle.meta.get(
            "scalar_addresses", _default_scalar_addresses(self._scalars)
        )

    def __enter__(self):
        try:
            for array in self._bundle.arrays:
                stream = _array_path(self._bundle.root, array.path).open("rb")
                access = mmap.ACCESS_COPY if array.role == "state" else mmap.ACCESS_READ
                self._files[array.name] = stream
                self._maps[array.name] = mmap.mmap(
                    stream.fileno(), 0, access=access
                )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _type, _value, _traceback):
        for mapping in self._maps.values():
            mapping.close()
        for stream in self._files.values():
            stream.close()
        self._maps.clear()
        self._files.clear()

    def _element(self, name, index):
        try:
            array = self._arrays[name]
        except KeyError as error:
            raise LazyTraceError(f"unknown array {name}") from error
        _uint(index, 64, "array index")
        if index >= array.count:
            raise LazyTraceError(f"array {name} index is outside image")
        width, code = _ELEMENTS[array.element_type]
        return array, width, code, index * width

    def load_float(self, name, index):
        array, width, code, offset = self._element(name, index)
        if code not in {"f", "d"}:
            raise LazyTraceError(f"array {name} is not floating point")
        value = struct.unpack_from(f"<{code}", self._maps[name], offset)[0]
        return array.logical_base + offset, value

    def load_raw(self, name, index):
        array, width, _code, offset = self._element(name, index)
        raw_code = "I" if width == 4 else "Q"
        raw = struct.unpack_from(f"<{raw_code}", self._maps[name], offset)[0]
        return array.logical_base + offset, raw

    def store_float(self, name, index, value):
        array, _width, code, offset = self._element(name, index)
        if array.role != "state":
            raise LazyTraceError(f"array {name} is immutable")
        if code not in {"f", "d"}:
            raise LazyTraceError(f"array {name} is not floating point")
        struct.pack_into(f"<{code}", self._maps[name], offset, value)

    def store_raw(self, name, index, value):
        array, width, _code, offset = self._element(name, index)
        if array.role != "state":
            raise LazyTraceError(f"array {name} is immutable")
        _uint(value, width * 8, "raw array value")
        raw_code = "I" if width == 4 else "Q"
        struct.pack_into(f"<{raw_code}", self._maps[name], offset, value)

    def boundary_sha256(self, name, count=None):
        if name not in self._arrays:
            raise LazyTraceError(f"unknown array {name}")
        array = self._arrays[name]
        if count is None:
            count = array.count
        _uint(count, 64, "boundary count")
        if count == 0 or count > array.count:
            raise LazyTraceError(f"array {name} boundary count is invalid")
        width = _ELEMENTS[array.element_type][0]
        byte_count = count * width
        digest = hashlib.sha256()
        mapping = self._maps[name]
        for offset in range(0, byte_count, 1024 * 1024):
            digest.update(mapping[offset:min(offset + 1024 * 1024, byte_count)])
        return digest.hexdigest()

    def load_scalar(self, name):
        _name(name, "scalar name")
        try:
            raw = self._scalars[name]
        except KeyError as error:
            raise LazyTraceError(f"unknown scalar {name}") from error
        _uint(raw, 64, "scalar raw value")
        return raw

    def store_scalar(self, name, raw):
        _name(name, "scalar name")
        _uint(raw, 64, "scalar raw value")
        self._scalars[name] = raw

    def scalar_address(self, name):
        _name(name, "scalar name")
        try:
            return self._scalar_addresses[name]
        except KeyError as error:
            raise LazyTraceError(f"unknown scalar {name}") from error

    def scalar_sha256(self, name):
        return hashlib.sha256(struct.pack("<Q", self.load_scalar(name))).hexdigest()


def _validate_memory_address(bundle, operation):
    element_type = _MEMORY_OPCODE_TYPES.get(operation.opcode)
    if element_type is None:
        return
    width = _ELEMENTS[element_type][0]
    for array in bundle.arrays:
        array_width = _ELEMENTS[array.element_type][0]
        start = array.logical_base
        end = start + array.count * array_width
        if start <= operation.address and operation.address + width <= end:
            if array.element_type != element_type:
                raise LazyTraceError(
                    "memory operation type differs from declared image"
                )
            if (operation.address - start) % width:
                raise LazyTraceError("memory operation address is misaligned")
            return
    addresses = bundle.meta.get(
        "scalar_addresses",
        _default_scalar_addresses(bundle.meta.get("initial_scalars", {})),
    )
    for name, address in addresses.items():
        if operation.address == address and element_type == "f64":
            return
    raise LazyTraceError("memory operation address is outside declared image")


def _validate_expanded_operation(bundle, invocation, operation):
    if not isinstance(operation, canonical.Operation):
        raise LazyTraceError("expander emitted a non-operation")
    if operation.sequence != 0:
        raise LazyTraceError("expander assigned a sequence")
    if operation.phase != invocation.phase:
        raise LazyTraceError("expander changed invocation phase")
    _validate_memory_address(bundle, operation)


def _validate_batch_work_items(batch_work_items):
    _uint(batch_work_items, 64, "batch work items")
    if batch_work_items == 0:
        raise LazyTraceError("batch work items is zero")


def iter_operations(bundle, expanders, *, batch_work_items=1):
    _validate_batch_work_items(batch_work_items)
    sequence = 0
    with MappedState(bundle) as state:
        for invocation in bundle.invocations:
            expander = expanders.get(invocation.kernel)
            if expander is None:
                raise LazyTraceError(f"unknown kernel {invocation.kernel}")
            for operation in expander(state, invocation, batch_work_items):
                _validate_expanded_operation(bundle, invocation, operation)
                yield dataclasses.replace(operation, sequence=sequence)
                sequence += 1
    if sequence != bundle.dynamic_work["primitive_records"]:
        raise LazyTraceError(
            f"dynamic primitive count {sequence} != "
            f"{bundle.dynamic_work['primitive_records']}"
        )


def expanded_fingerprint(bundle, expanders, *, batch_work_items=1):
    count = 0

    def counted():
        nonlocal count
        for operation in iter_operations(
            bundle, expanders, batch_work_items=batch_work_items
        ):
            count += 1
            yield operation

    digest = canonical.operations_sha256(counted())
    return digest, count
