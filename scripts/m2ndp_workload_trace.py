#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Strict scalar lowering from canonical matched traces to M2NDP packages."""

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import contextlib
from pathlib import Path
from types import SimpleNamespace

try:
    from scripts import canonical_work_trace as canonical
    from scripts import cross_system_contract as contract
    from scripts import m2ndp_artifacts as artifact_helpers
    from scripts import lazy_work_trace as lazy
    from scripts import npb_lazy_trace as npb
except ImportError:
    import canonical_work_trace as canonical
    import cross_system_contract as contract
    import m2ndp_artifacts as artifact_helpers
    import lazy_work_trace as lazy
    import npb_lazy_trace as npb


_SHA256 = re.compile(r"[0-9a-f]{64}")


class TraceTranslationError(RuntimeError):
    """A canonical trace cannot be represented by the strict scalar path."""


@dataclasses.dataclass(frozen=True)
class LoweredOperation:
    sequence: int
    phase: int
    work_item: int
    opcode: str
    address: int
    operand0: int
    operand1: int
    result: int
    dependency: int
    instruction: str


@dataclasses.dataclass(frozen=True)
class PackageProvenance:
    trace_sha256: str
    input_sha256: str
    funcsim_path: str
    ndpsim_path: str
    patch_paths: tuple[str, ...]
    config_path: str
    ndpsim_config_path: str | None = None
    funcsim_sha256: str = dataclasses.field(init=False)
    ndpsim_sha256: str = dataclasses.field(init=False)
    patch_sha256: str = dataclasses.field(init=False)
    config_sha256: str = dataclasses.field(init=False)
    ndpsim_config_sha256: str = dataclasses.field(init=False)
    ndpsim_config_tree_sha256: str = dataclasses.field(init=False)

    def __post_init__(self):
        for field in ("trace_sha256", "input_sha256"):
            value = getattr(self, field)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise TraceTranslationError(
                    f"{field.removesuffix('_sha256')} SHA-256 is invalid"
                )
        funcsim = _require_file(self.funcsim_path, "FuncSim")
        ndpsim = _require_file(self.ndpsim_path, "NDPSim")
        config = _require_file(self.config_path, "M2NDP config")
        ndpsim_config = _require_file(
            self.ndpsim_config_path or config, "NDPSim config"
        )
        patches = tuple(
            str(_require_file(path, "M2NDP patch"))
            for path in self.patch_paths
        )
        if not patches:
            raise TraceTranslationError("M2NDP patch set is empty")
        object.__setattr__(self, "funcsim_path", str(funcsim))
        object.__setattr__(self, "ndpsim_path", str(ndpsim))
        object.__setattr__(self, "config_path", str(config))
        object.__setattr__(self, "ndpsim_config_path", str(ndpsim_config))
        object.__setattr__(self, "patch_paths", patches)
        object.__setattr__(self, "funcsim_sha256", _sha256_file(funcsim))
        object.__setattr__(self, "ndpsim_sha256", _sha256_file(ndpsim))
        object.__setattr__(self, "config_sha256", _sha256_file(config))
        object.__setattr__(
            self, "ndpsim_config_sha256", _sha256_file(ndpsim_config)
        )
        object.__setattr__(
            self, "ndpsim_config_tree_sha256",
            _sha256_tree(ndpsim_config.parent),
        )
        object.__setattr__(self, "patch_sha256", _sha256_files(patches))

    def as_dict(self):
        return dataclasses.asdict(self)


def _canonical(name):
    return lambda operation: " ".join((
        f"c_{name}", "x0,",
        f"{operation.address},{operation.operand0},",
        f"{operation.operand1},{operation.result}",
    )).replace(", ", ",")


LOWERING = {
    opcode: _canonical(opcode.name.lower()) for opcode in canonical.Opcode
}


def lower_operations(operations):
    lowered = []
    for sequence, operation in enumerate(operations):
        if operation.sequence != sequence:
            raise TraceTranslationError(
                f"canonical operation sequence {operation.sequence} != {sequence}"
            )
        try:
            formatter = LOWERING[operation.opcode]
        except (KeyError, TypeError) as error:
            raise TraceTranslationError(
                f"canonical operation {sequence} is not lowerable"
            ) from error
        dependency = (
            operation.operand1
            if operation.opcode in {
                canonical.Opcode.LOAD_U32,
                canonical.Opcode.LOAD_U64,
                canonical.Opcode.LOAD_F32,
                canonical.Opcode.LOAD_F64,
            }
            else 0
        )
        lowered.append(LoweredOperation(
            sequence=sequence,
            phase=operation.phase,
            work_item=operation.work_item,
            opcode=operation.opcode.name,
            address=operation.address,
            operand0=operation.operand0,
            operand1=operation.operand1,
            result=operation.result,
            dependency=dependency,
            instruction=formatter(operation),
        ))
    return tuple(lowered)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_file(path, label):
    path = Path(path).resolve()
    if not path.is_file():
        raise TraceTranslationError(f"{label} file is missing: {path}")
    return path


def _sha256_files(paths):
    digest = hashlib.sha256()
    for path in sorted(Path(item).resolve() for item in paths):
        encoded = path.name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _sha256_tree(root):
    root = Path(root).resolve()
    files = tuple(
        path for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    if not files:
        raise TraceTranslationError(f"configuration tree is empty: {root}")
    digest = hashlib.sha256()
    for path in files:
        encoded = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _atomic_text_stream(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    stream = None
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        yield stream
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        os.replace(temporary, path)
    except BaseException:
        if stream is not None:
            stream.close()
        temporary.unlink(missing_ok=True)
        raise


def _launch_events(lowered):
    if not lowered:
        raise TraceTranslationError("canonical trace contains no operations")
    groups = []
    start = 0
    for index in range(1, len(lowered) + 1):
        if index == len(lowered) or lowered[index].phase != lowered[start].phase:
            groups.append((lowered[start].phase, start, index - 1))
            start = index
    events = []
    for launch, (phase, first, last) in enumerate(groups):
        common = {"launch": launch, "phase": phase}
        events.extend((
            {**common, "kind": "fixed_launch", "before_sequence": first},
            {**common, "kind": "dynamic", "first_sequence": first,
             "last_sequence": last, "operation_count": last - first + 1},
            {**common, "kind": "fixed_completion", "after_sequence": last},
        ))
    return groups, events


def _kernel(name, kernel_id, rows):
    return (
        f"-kernel name = {name}\n"
        f"-kernel id = {kernel_id}\n\n"
        "KERNELBODY:\n"
        + "\n".join(rows)
        + "\n"
    )


def _launch(kernel_id):
    return f"1 {kernel_id} 0x1000000000000000 0x20 0x0 0x0\n"


def _boundary_checks(bundle):
    checks = {}
    specifications = bundle.meta.get("output_boundaries", {})
    for name in sorted(specifications):
        specification = specifications[name]
        probes = specification.get("probes")
        bits = specification.get("word_bits")
        words = bundle.outputs.get(name)
        if bits not in (32, 64) or not isinstance(probes, list) or (
            words is None or len(probes) != len(words)
        ):
            raise TraceTranslationError(
                f"canonical output boundary {name} mapping is invalid"
            )
        for probe, word in zip(probes, words):
            sequence = probe.get("after_sequence")
            address = probe.get("address")
            if not isinstance(sequence, int) or not isinstance(address, int):
                raise TraceTranslationError(
                    f"canonical output boundary {name} probe is invalid"
                )
            checks.setdefault(sequence, []).append(
                f"c_check_u{bits} x0,{address},{word},0,0"
            )
    return checks


def _write_memory_map(bundle, trace_root, outdir):
    image_root = outdir / "images"
    image_root.mkdir()
    packets = {}
    image_records = []
    for name, record in sorted(bundle.meta["initial_memory"].items()):
        source = (trace_root / record["path"]).resolve()
        if _sha256_file(source) != record["sha256"]:
            raise TraceTranslationError(
                f"initial memory image {name} SHA-256 differs"
            )
        destination = image_root / Path(record["path"]).name
        shutil.copyfile(source, destination)
        bits = record["word_bits"]
        words = bundle.initial_memory[name]
        width = bits // 8
        for offset, word in enumerate(words):
            address = record["logical_base"] + offset * width
            if address % width:
                raise TraceTranslationError(
                    f"initial memory image {name} word is misaligned"
                )
            packet_base = address - address % 32
            packet = packets.setdefault(
                packet_base,
                {"bits": bits, "words": [0] * (32 // width), "used": set()},
            )
            if packet["bits"] != bits:
                raise TraceTranslationError(
                    "initial memory mixes word widths in one packet"
                )
            index = (address - packet_base) // width
            if index in packet["used"]:
                raise TraceTranslationError("initial memory words overlap")
            packet["words"][index] = word
            packet["used"].add(index)
        image_records.append({
            "name": name,
            "path": destination.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(destination),
            "logical_base": record["logical_base"],
            "word_bits": bits,
            "count": len(words),
        })
    target_packets = {
        address: {
            "bits": packet["bits"], "words": list(packet["words"]),
            "used": set(packet["used"]),
        }
        for address, packet in packets.items()
    }
    store_widths = {
        canonical.Opcode.STORE_U32: 32,
        canonical.Opcode.STORE_F32: 32,
        canonical.Opcode.STORE_U64: 64,
        canonical.Opcode.STORE_F64: 64,
    }
    for operation in bundle.operations:
        bits = store_widths.get(operation.opcode)
        if bits is None:
            continue
        if operation.operand0 >= 1 << bits:
            raise TraceTranslationError(
                f"canonical uint{bits} store value is out of range"
            )
        width = bits // 8
        address = operation.address
        if address % width:
            raise TraceTranslationError("canonical store address is misaligned")
        packet_base = address - address % 32
        packet = target_packets.setdefault(
            packet_base,
            {"bits": bits, "words": [0] * (32 // width), "used": set()},
        )
        if packet["bits"] != bits:
            raise TraceTranslationError(
                "canonical stores mix word widths in one packet"
            )
        index = (address - packet_base) // width
        packet["words"][index] = operation.operand0
        packet["used"].add(index)

    path = outdir / "memory-map.data"
    target_path = outdir / "target-map.data"
    _atomic_write_text(path, _render_packets(packets))
    _atomic_write_text(target_path, _render_packets(target_packets))
    return path, target_path, image_records


def _render_packets(selected_packets):
    sections = []
    for bits in (32, 64):
        rows = [
            (address, packet)
            for address, packet in sorted(selected_packets.items())
            if packet["bits"] == bits
        ]
        if not rows:
            continue
        sections.extend(("_META_", f"int{bits}", "_DATA_"))
        for address, packet in rows:
            signed = [
                value if value < 1 << (bits - 1)
                else value - (1 << bits)
                for value in packet["words"]
            ]
            sections.append(
                f"0x{address:x} " + " ".join(str(value) for value in signed)
            )
    return "\n".join(sections) + "\n"


def _insert_packet_word(packets, address, bits, word, label):
    width = bits // 8
    if address % width:
        raise TraceTranslationError(f"{label} word is misaligned")
    packet_base = address - address % 32
    packet = packets.setdefault(
        packet_base,
        {"bits": bits, "words": [0] * (32 // width), "used": set()},
    )
    if packet["bits"] != bits:
        raise TraceTranslationError(f"{label} mixes word widths in one packet")
    index = (address - packet_base) // width
    packet["words"][index] = word
    packet["used"].add(index)


def _lazy_maps(bundle, state, outdir):
    image_root = outdir / "images"
    image_root.mkdir()
    initial_packets = {}
    target_packets = {}
    image_records = []
    widths = {"u32": 32, "f32": 32, "u64": 64, "f64": 64}
    for array in bundle.arrays:
        source = (bundle.root / array.path).resolve()
        destination = image_root / f"array-{len(image_records)}-{source.name}"
        shutil.copyfile(source, destination)
        bits = widths[array.element_type]
        width = bits // 8
        with source.open("rb") as source_stream:
            for index in range(array.count):
                address = array.logical_base + index * width
                payload = source_stream.read(width)
                if len(payload) != width:
                    raise TraceTranslationError(
                        f"lazy initial image {array.name} is truncated"
                    )
                initial = int.from_bytes(payload, "little")
                _insert_packet_word(
                    initial_packets, address, bits, initial,
                    "lazy initial memory",
                )
                _insert_packet_word(
                    target_packets, address, bits,
                    state.load_raw(array.name, index)[1],
                    "lazy target memory",
                )
        image_records.append({
            "name": array.name,
            "path": destination.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(destination),
            "logical_base": array.logical_base,
            "word_bits": bits,
            "count": array.count,
        })
    for name, initial in sorted(bundle.meta["initial_scalars"].items()):
        address = bundle.meta["scalar_addresses"][name]
        _insert_packet_word(
            initial_packets, address, 64, initial, "lazy scalar memory"
        )
        _insert_packet_word(
            target_packets, address, 64, state.load_scalar(name),
            "lazy scalar target",
        )
    memory_map = outdir / "memory-map.data"
    target_map = outdir / "target-map.data"
    _atomic_write_text(memory_map, _render_packets(initial_packets))
    _atomic_write_text(target_map, _render_packets(target_packets))
    return memory_map, target_map, image_records


def _verify_manifest_file(root, record, label):
    if not isinstance(record, dict):
        raise TraceTranslationError(f"{label} manifest record is invalid")
    relative = record.get("path")
    if not isinstance(relative, str) or not relative:
        raise TraceTranslationError(f"{label} manifest path is invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise TraceTranslationError(f"{label} escapes package root") from error
    if _sha256_file(_require_file(path, label)) != record.get("sha256"):
        raise TraceTranslationError(f"{label} SHA-256 differs")
    return path


def _verify_output_boundaries(manifest):
    boundaries = manifest.get("output_boundaries")
    if not isinstance(boundaries, dict):
        raise TraceTranslationError("M2NDP output boundaries are missing")
    if not boundaries:
        derivation = manifest.get("derived_window")
        if (
            manifest.get("functional_gate") != "operation_results"
            or not isinstance(derivation, dict)
            or _SHA256.fullmatch(
                str(derivation.get("source_trace_sha256", ""))
            ) is None
            or not isinstance(derivation.get("window_index"), int)
            or isinstance(derivation.get("window_index"), bool)
            or derivation["window_index"] < 0
        ):
            raise TraceTranslationError("M2NDP output boundaries are missing")
        return boundaries
    for name, record in sorted(boundaries.items()):
        if not isinstance(name, str) or not name or not isinstance(record, dict):
            raise TraceTranslationError("M2NDP output boundary is invalid")
        bits = record.get("word_bits")
        if record.get("element_type") != f"u{bits}" or bits not in (32, 64):
            raise TraceTranslationError(
                f"M2NDP output boundary {name} type is invalid"
            )
        words = record.get("raw_words")
        if not isinstance(words, list) or not words:
            raise TraceTranslationError(
                f"M2NDP output boundary {name} words are missing"
            )
        payload = bytearray()
        for index, word in enumerate(words):
            if (
                isinstance(word, bool) or not isinstance(word, int)
                or word < 0 or word >= 1 << bits
            ):
                raise TraceTranslationError(
                    f"M2NDP output boundary {name}[{index}] is outside uint{bits}"
                )
            payload.extend(word.to_bytes(bits // 8, "little"))
        if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
            raise TraceTranslationError(
                f"M2NDP output boundary {name} SHA-256 differs"
            )
    return boundaries


def _verify_package_payloads(root, manifest, *, timing):
    operations = manifest.get("operations")
    sequence = _verify_manifest_file(root, operations, "sequence")
    _verify_manifest_file(
        root,
        {
            "path": operations.get("records_path")
            if isinstance(operations, dict) else None,
            "sha256": operations.get("records_sha256")
            if isinstance(operations, dict) else None,
        },
        "operation records",
    )
    _verify_manifest_file(root, manifest.get("memory_map"), "memory map")
    _verify_manifest_file(root, manifest.get("target_map"), "target map")
    for index, image in enumerate(manifest.get("initial_images", ())):
        _verify_manifest_file(root, image, f"initial image {index}")
    for index, kernel in enumerate(manifest.get("kernels", ())):
        field = "timing_path" if timing else "path"
        hash_field = "timing_sha256" if timing else "sha256"
        _verify_manifest_file(
            root,
            {"path": kernel.get(field), "sha256": kernel.get(hash_field)},
            f"{'timing ' if timing else ''}kernel {index}",
        )
        _verify_manifest_file(
            root,
            {"path": kernel.get("launch_path"),
             "sha256": kernel.get("launch_sha256")},
            f"launch {index}",
        )
    return sequence


def run_funcsim_package(manifest_path, *, funcsim=None, evidence_path=None):
    """Run canonical FuncSim and require its in-simulator bit checks."""
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = contract.load_json(manifest_path)
    if manifest.get("schema") != 1:
        raise TraceTranslationError("M2NDP package schema is invalid")
    boundaries = _verify_output_boundaries(manifest)
    provenance = manifest.get("provenance", {})
    configured = Path(provenance.get("funcsim_path", "")).resolve()
    selected = configured if funcsim is None else Path(funcsim).resolve()
    if selected != configured or _sha256_file(
        _require_file(selected, "FuncSim")
    ) != provenance.get("funcsim_sha256"):
        raise TraceTranslationError("FuncSim provenance differs")
    config = Path(provenance.get("config_path", "")).resolve()
    if _sha256_file(_require_file(config, "M2NDP config")) != provenance.get(
        "config_sha256"
    ):
        raise TraceTranslationError("M2NDP config provenance differs")
    patch_paths = provenance.get("patch_paths")
    if not isinstance(patch_paths, list) or _sha256_files(
        tuple(_require_file(path, "M2NDP patch") for path in patch_paths)
    ) != provenance.get("patch_sha256"):
        raise TraceTranslationError("M2NDP patch provenance differs")
    sequence = _verify_package_payloads(root, manifest, timing=False)
    memory_map = _verify_manifest_file(
        root, manifest.get("memory_map"), "memory map"
    )
    command = [
        str(selected), "--memory_map", str(memory_map),
        "--canonical_sequence_file", str(sequence),
        "--config", str(config),
    ]
    completed = subprocess.run(
        command, cwd=root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    stdout_path = root / "funcsim.stdout.log"
    stderr_path = root / "funcsim.stderr.log"
    _atomic_write_text(stdout_path, completed.stdout)
    _atomic_write_text(stderr_path, completed.stderr)
    markers = {}
    for line in completed.stdout.splitlines():
        match = re.fullmatch(r"M2NDP_CANONICAL_([A-Z]+)=(.+)", line.strip())
        if match:
            markers[match.group(1)] = match.group(2)
    expected_launches = manifest.get("dynamic_launches")
    expected_words = sum(
        len(record.get("raw_words", ()))
        for record in boundaries.values()
    )
    try:
        launches = int(markers.get("LAUNCHES", "-1"))
        boundaries = int(markers.get("BOUNDARIES", "-1"))
        operations = int(markers.get("OPERATIONS", "-1"))
    except ValueError as error:
        raise TraceTranslationError("FuncSim canonical markers are invalid") from error
    if (
        completed.returncode != 0 or markers.get("MODE") != "1"
        or markers.get("MATCH") != "PASS"
        or launches != expected_launches or boundaries != expected_words
        or operations != manifest.get("operation_count")
        or (
            expected_words <= 0
            and manifest.get("functional_gate") != "operation_results"
        )
    ):
        raise TraceTranslationError(
            "FuncSim canonical bit-exact gate failed: "
            f"status={completed.returncode} launches={launches}/"
            f"{expected_launches} boundaries={boundaries}/{expected_words}"
            f" operations={operations}/{manifest.get('operation_count')}"
        )
    evidence = {
        "schema": 1, "status": "pass", "returncode": completed.returncode,
        "boundary_count": len(manifest["output_boundaries"]),
        "compared_words": boundaries,
        "compared_operations": operations,
        "functional_gate": manifest.get("functional_gate", "boundary_words"),
        "expected_launches": expected_launches,
        "completed_launches": launches,
        "funcsim_sha256": provenance["funcsim_sha256"],
        "config_sha256": provenance["config_sha256"],
        "package_sha256": _sha256_file(manifest_path),
        "trace_sha256": provenance["trace_sha256"],
        "input_sha256": provenance["input_sha256"],
        "patch_sha256": provenance["patch_sha256"],
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        "command": command,
    }
    if evidence_path is not None:
        contract.atomic_write_json(Path(evidence_path), evidence)
    return evidence


def _verify_funcsim_evidence_log(root, functional_evidence, stream):
    path_value = functional_evidence.get(f"{stream}_path")
    expected_sha256 = functional_evidence.get(f"{stream}_sha256")
    if not isinstance(path_value, str) or _SHA256.fullmatch(
        str(expected_sha256)
    ) is None:
        raise TraceTranslationError(
            f"FuncSim evidence {stream} log binding is invalid"
        )
    path = Path(path_value).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise TraceTranslationError(
            f"FuncSim evidence {stream} log escapes package root"
        ) from error
    if _sha256_file(_require_file(path, f"FuncSim {stream} log")) != (
        expected_sha256
    ):
        raise TraceTranslationError(
            f"FuncSim evidence {stream} log SHA-256 differs"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise TraceTranslationError(
            f"FuncSim evidence {stream} log is unreadable"
        ) from error


def _verify_funcsim_evidence_cardinality(
    root, manifest, boundaries, functional_evidence
):
    expected_gate = "boundary_words" if boundaries else "operation_results"
    if manifest.get("functional_gate") != expected_gate:
        raise TraceTranslationError("M2NDP package functional gate is invalid")
    expected = {
        "schema": 1,
        "boundary_count": len(boundaries),
        "compared_words": sum(
            len(record["raw_words"]) for record in boundaries.values()
        ),
        "compared_operations": manifest.get("operation_count"),
        "functional_gate": expected_gate,
        "expected_launches": manifest.get("dynamic_launches"),
        "completed_launches": manifest.get("dynamic_launches"),
        "returncode": 0,
        "status": "pass",
    }
    for field, value in expected.items():
        observed = functional_evidence.get(field)
        if isinstance(value, int) and (
            isinstance(observed, bool) or not isinstance(observed, int)
        ):
            raise TraceTranslationError(
                f"FuncSim evidence cardinality differs: {field}"
            )
        if observed != value:
            raise TraceTranslationError(
                f"FuncSim evidence cardinality differs: {field}"
            )
    stdout = _verify_funcsim_evidence_log(
        root, functional_evidence, "stdout"
    )
    _verify_funcsim_evidence_log(root, functional_evidence, "stderr")
    marker_rows = re.findall(
        r"^M2NDP_CANONICAL_([A-Z]+)=(.+)$", stdout, flags=re.MULTILINE
    )
    if len(marker_rows) != 5 or len({name for name, _ in marker_rows}) != 5:
        raise TraceTranslationError("FuncSim evidence stdout markers differ")
    markers = dict(marker_rows)
    expected_markers = {
        "MODE": "1", "MATCH": "PASS",
        "LAUNCHES": str(expected["completed_launches"]),
        "BOUNDARIES": str(expected["compared_words"]),
        "OPERATIONS": str(expected["compared_operations"]),
    }
    if markers != expected_markers:
        raise TraceTranslationError("FuncSim evidence stdout markers differ")


def run_ndpsim_package(
    manifest_path, *, functional_evidence, calibration,
    ndpsim=None, evidence_path=None,
):
    """Run timing only after complete functional and exact-1us gates."""
    if (
        not isinstance(functional_evidence, dict)
        or functional_evidence.get("status") != "pass"
    ):
        raise artifact_helpers.EvidenceError(
            "NDPSim timing requires complete FuncSim PASS"
        )
    manifest_path = Path(manifest_path).resolve()
    root = manifest_path.parent
    manifest = contract.load_json(manifest_path)
    if manifest.get("schema") != 1:
        raise TraceTranslationError("M2NDP package schema is invalid")
    boundaries = _verify_output_boundaries(manifest)
    provenance = manifest.get("provenance", {})
    bindings = {
        "package_sha256": _sha256_file(manifest_path),
        "trace_sha256": provenance.get("trace_sha256"),
        "input_sha256": provenance.get("input_sha256"),
        "funcsim_sha256": provenance.get("funcsim_sha256"),
        "config_sha256": provenance.get("config_sha256"),
        "patch_sha256": provenance.get("patch_sha256"),
    }
    for field, expected in bindings.items():
        if functional_evidence.get(field) != expected:
            raise TraceTranslationError(
                f"FuncSim evidence {field.removesuffix('_sha256').replace('_', ' ')} "
                "differs"
            )
    _verify_funcsim_evidence_cardinality(
        root, manifest, boundaries, functional_evidence
    )
    artifact_helpers.require_ndpsim_timing_gate(
        functional_evidence, calibration
    )
    configured = Path(provenance.get("ndpsim_path", "")).resolve()
    selected = configured if ndpsim is None else Path(ndpsim).resolve()
    if selected != configured or _sha256_file(
        _require_file(selected, "NDPSim")
    ) != provenance.get("ndpsim_sha256"):
        raise TraceTranslationError("NDPSim provenance differs")
    timing_config_record = manifest.get("timing_config")
    config = _verify_manifest_file(
        root, timing_config_record, "packaged NDPSim config"
    )
    if _sha256_file(config) != provenance.get(
        "ndpsim_config_sha256"
    ):
        raise TraceTranslationError("NDPSim config provenance differs")
    if _sha256_tree(config.parent) != provenance.get(
        "ndpsim_config_tree_sha256"
    ):
        raise TraceTranslationError("NDPSim config tree provenance differs")
    if calibration.get("derived_m2ndp_config_sha256") != _sha256_file(config):
        raise TraceTranslationError("M2NDP calibration config differs")
    link_configs = tuple(config.parent.rglob("cxl_link.icnt"))
    if len(link_configs) != 1 or calibration.get(
        "derived_cxl_link_config_sha256"
    ) != _sha256_file(link_configs[0]):
        raise TraceTranslationError("M2NDP calibration CXL link config differs")
    patch_paths = provenance.get("patch_paths")
    if not isinstance(patch_paths, list) or _sha256_files(
        tuple(_require_file(path, "M2NDP patch") for path in patch_paths)
    ) != provenance.get("patch_sha256"):
        raise TraceTranslationError("M2NDP patch provenance differs")
    kernels_list = _verify_manifest_file(
        root, manifest.get("timing_kernels"), "timing kernel list"
    )
    _verify_package_payloads(root, manifest, timing=True)
    _verify_manifest_file(root, manifest.get("timing_input"), "timing input")
    _verify_manifest_file(root, manifest.get("timing_output"), "timing output")
    runtime_root = root / "timing-run"
    if runtime_root.exists():
        raise TraceTranslationError("fresh NDPSim output root required")
    shutil.copytree(config.parent, runtime_root)
    runtime_config = runtime_root / config.name
    if _sha256_tree(runtime_root) != provenance.get(
        "ndpsim_config_tree_sha256"
    ):
        raise TraceTranslationError("NDPSim runtime config copy differs")
    for generated_name in ("ndpsim.out", "energy_ndpsim.out",
                           "ramulator.stats"):
        (runtime_root / generated_name).unlink(missing_ok=True)
    output = runtime_root / "ndpsim.out"
    command = [
        str(selected), "--trace", str(kernels_list.parent),
        "--num_hosts", "1", "--num_m2ndps", "1",
        "--config", str(runtime_config), "--output", output.name,
        "--serial_launch", "true", "--synthetic_memory", "false",
    ]
    completed = subprocess.run(
        command, cwd=runtime_root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    stdout_path = root / "ndpsim.stdout.log"
    stderr_path = root / "ndpsim.stderr.log"
    _atomic_write_text(stdout_path, completed.stdout)
    _atomic_write_text(stderr_path, completed.stderr)
    combined = completed.stdout + "\n" + completed.stderr
    matches = re.findall(r"EXPR FINISHED\s+(\d+)", combined)
    completed_launches = len(re.findall(
        r"Gantt info:\s*host\s+\d+\s+finished NDP kernel\b", combined
    ))
    memory_matches = len(re.findall(r"\bMEMROY MATCH SUCCESS\b", combined))
    expected_launches = manifest.get("dynamic_launches")
    if (
        completed.returncode != 0 or len(matches) != 1
        or memory_matches != 1 or completed_launches != expected_launches
    ):
        raise TraceTranslationError(
            "NDPSim timing evidence is incomplete: "
            f"status={completed.returncode} cycle_markers={len(matches)} "
            f"memory_matches={memory_matches} launches="
            f"{completed_launches}/{expected_launches}"
        )
    cycles = int(matches[0])
    if cycles <= 0 or not output.is_file():
        raise TraceTranslationError("NDPSim timing evidence is incomplete")
    immutable_tree_sha256 = _sha256_tree(config.parent)
    if immutable_tree_sha256 != provenance.get("ndpsim_config_tree_sha256"):
        raise TraceTranslationError("NDPSim mutated immutable config provenance")
    evidence = {
        "schema": 1, "status": "pass", "returncode": completed.returncode,
        "cycles": cycles, "functional": functional_evidence,
        "expected_launches": expected_launches,
        "completed_launches": completed_launches,
        "memory_match": "pass",
        "calibration": calibration,
        "ndpsim_sha256": provenance["ndpsim_sha256"],
        "config_sha256": provenance["ndpsim_config_sha256"],
        "config_tree_sha256": immutable_tree_sha256,
        "package_sha256": bindings["package_sha256"],
        "trace_sha256": bindings["trace_sha256"],
        "input_sha256": bindings["input_sha256"],
        "patch_sha256": provenance["patch_sha256"],
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "output_path": str(output), "output_sha256": _sha256_file(output),
        "command": command,
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
    }
    if evidence_path is not None:
        contract.atomic_write_json(Path(evidence_path), evidence)
    return evidence


def _lazy_raw_words(state, bundle, *, bits, count, base):
    arrays = {
        array.logical_base: array for array in bundle.arrays
    }
    array = arrays.get(base)
    if array is not None:
        expected_bits = 32 if array.element_type in {"u32", "f32"} else 64
        if expected_bits != bits or count > array.count:
            raise TraceTranslationError("lazy boundary shape differs")
        return tuple(
            state.load_raw(array.name, index)[1] for index in range(count)
        )
    scalars = {
        address: name
        for name, address in bundle.meta["scalar_addresses"].items()
    }
    name = scalars.get(base)
    if name is None or bits != 64 or count != 1:
        raise TraceTranslationError("lazy boundary address is not declared")
    return (state.load_scalar(name),)


def _lower_lazy_bundle(trace_root, outdir, provenance):
    bundle = lazy.read_bundle(trace_root)
    descriptor_sha256 = _sha256_file(bundle.root / "trace.v2.json")
    if descriptor_sha256 != provenance.trace_sha256:
        raise TraceTranslationError("source trace SHA-256 differs")
    if bundle.meta["input_sha256"] != provenance.input_sha256:
        raise TraceTranslationError("source input SHA-256 differs")
    commitments = bundle.meta.get("boundary_commitments")
    if not isinstance(commitments, dict) or not commitments:
        raise TraceTranslationError("lazy trace boundary commitments are missing")
    outdir = Path(outdir)
    if outdir.exists():
        raise TraceTranslationError(f"fresh M2NDP package root required: {outdir}")
    outdir.mkdir(parents=True)
    phase_root = outdir / "kernels"
    phase_root.mkdir()
    operations_path = outdir / "operations.jsonl"
    sequence_path = outdir / "funcsim.sequence"
    kernels_list_path = phase_root / "kernelslist.g"
    sequence = 0
    selected_boundaries = set()
    boundaries = {}
    kernels = []
    events = []
    sequence_lines = []
    timing_names = []
    with (
        lazy.MappedState(bundle) as state,
        _atomic_text_stream(operations_path) as operation_stream,
    ):
        for launch, invocation in enumerate(bundle.invocations):
            try:
                expander = npb.EXPANDERS[invocation.kernel]
            except KeyError as error:
                raise TraceTranslationError(
                    f"unknown lazy kernel {invocation.kernel}"
                ) from error
            stem = f"launch-{launch}-phase-{invocation.phase}"
            functional = phase_root / f"{stem}-functional.traceg"
            timing_kernel = phase_root / f"{stem}.traceg"
            launch_path = phase_root / f"{stem}_launch.txt"
            first = sequence
            commit_sequence = None
            with (
                _atomic_text_stream(functional) as functional_stream,
                _atomic_text_stream(timing_kernel) as timing_stream,
            ):
                header = (
                    f"-kernel name = CANONICAL_LAUNCH_{launch}_PHASE_"
                    f"{invocation.phase}\n-kernel id = {launch}\n\nKERNELBODY:\n"
                )
                functional_stream.write(header)
                timing_stream.write(header)
                for operation in expander(state, invocation, 1024):
                    lazy._validate_expanded_operation(
                        bundle, invocation, operation
                    )
                    operation = dataclasses.replace(
                        operation, sequence=sequence
                    )
                    formatter = LOWERING.get(operation.opcode)
                    if formatter is None:
                        raise TraceTranslationError(
                            f"canonical operation {sequence} is not lowerable"
                        )
                    row = LoweredOperation(
                        sequence, operation.phase, operation.work_item,
                        operation.opcode.name, operation.address,
                        operation.operand0, operation.operand1,
                        operation.result,
                        operation.operand1 if operation.opcode in {
                            canonical.Opcode.LOAD_U32,
                            canonical.Opcode.LOAD_U64,
                            canonical.Opcode.LOAD_F32,
                            canonical.Opcode.LOAD_F64,
                        } else 0,
                        formatter(operation),
                    )
                    functional_stream.write(row.instruction + "\n")
                    timing_stream.write(row.instruction + "\n")
                    operation_stream.write(
                        json.dumps(dataclasses.asdict(row), sort_keys=True,
                                   separators=(",", ":")) + "\n"
                    )
                    if operation.opcode == canonical.Opcode.COMMIT:
                        commit_sequence = sequence
                    sequence += 1
                if commit_sequence is None:
                    raise TraceTranslationError(
                        f"lazy invocation {launch} has no COMMIT"
                    )
                for name, bits, count, base in npb.invocation_boundary_specs(
                    bundle, invocation
                ):
                    if name not in commitments:
                        continue
                    if name in selected_boundaries:
                        raise TraceTranslationError(
                            f"duplicate lazy boundary {name}"
                        )
                    words = _lazy_raw_words(
                        state, bundle, bits=bits, count=count, base=base
                    )
                    payload = b"".join(
                        word.to_bytes(bits // 8, "little") for word in words
                    )
                    digest = hashlib.sha256(payload).hexdigest()
                    if digest != commitments[name]:
                        raise TraceTranslationError(
                            f"lazy boundary {name} differs from commitment"
                        )
                    step = bits // 8
                    for index, word in enumerate(words):
                        functional_stream.write(
                            f"c_check_u{bits} x0,{base + index * step},"
                            f"{word},0,0\n"
                        )
                    boundaries[name] = {
                        "element_type": f"u{bits}", "word_bits": bits,
                        "raw_words": list(words), "sha256": digest,
                    }
                    selected_boundaries.add(name)
            last = sequence - 1
            _atomic_write_text(launch_path, _launch(launch))
            sequence_lines.append(
                f"{functional.relative_to(outdir).as_posix()}\t"
                f"{launch_path.read_text().strip()}"
            )
            timing_names.append(stem)
            kernels.append({
                "phase": invocation.phase, "launch": launch,
                "path": functional.relative_to(outdir).as_posix(),
                "sha256": _sha256_file(functional),
                "timing_path": timing_kernel.relative_to(outdir).as_posix(),
                "timing_sha256": _sha256_file(timing_kernel),
                "launch_path": launch_path.relative_to(outdir).as_posix(),
                "launch_sha256": _sha256_file(launch_path),
            })
            common = {"launch": launch, "phase": invocation.phase}
            events.extend((
                {**common, "kind": "fixed_launch", "before_sequence": first},
                {**common, "kind": "dynamic", "first_sequence": first,
                 "last_sequence": last,
                 "operation_count": last - first + 1},
                {**common, "kind": "fixed_completion", "after_sequence": last},
            ))
        if sequence != bundle.dynamic_work["primitive_records"]:
            raise TraceTranslationError(
                f"lazy operation count {sequence} differs from descriptor"
            )
        if set(commitments) != selected_boundaries:
            missing = sorted(set(commitments) - selected_boundaries)
            raise TraceTranslationError(
                f"lazy boundary commitments were not selected: {missing[:3]}"
            )
        memory_map, target_map, initial_images = _lazy_maps(
            bundle, state, outdir
        )
    _atomic_write_text(sequence_path, "\n".join(sequence_lines) + "\n")
    _atomic_write_text(kernels_list_path, "\n".join(timing_names) + "\n")
    first_name = timing_names[0]
    last_name = timing_names[-1]
    timing_input = phase_root / f"{first_name}_input.data"
    timing_output = phase_root / f"{last_name}_output.data"
    shutil.copyfile(memory_map, timing_input)
    shutil.copyfile(target_map, timing_output)
    timing_config_root = outdir / "timing-config"
    shutil.copytree(Path(provenance.ndpsim_config_path).parent,
                    timing_config_root)
    timing_config = timing_config_root / Path(provenance.ndpsim_config_path).name
    if _sha256_tree(timing_config_root) != provenance.ndpsim_config_tree_sha256:
        raise TraceTranslationError("packaged NDPSim config tree differs")
    manifest_path = outdir / "package.json"
    contract.atomic_write_json(manifest_path, {
        "schema": 1, "source_schema": 2,
        "workload": bundle.meta["workload"],
        "operation_count": sequence,
        "operations": {
            "path": sequence_path.name, "sha256": _sha256_file(sequence_path),
            "records_path": operations_path.name,
            "records_sha256": _sha256_file(operations_path),
        },
        "dynamic_launches": len(kernels),
        "timing_kernels": {
            "path": kernels_list_path.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(kernels_list_path),
        },
        "launch_events": events, "kernels": kernels,
        "memory_map": {"path": memory_map.name,
                       "sha256": _sha256_file(memory_map)},
        "target_map": {"path": target_map.name,
                       "sha256": _sha256_file(target_map)},
        "timing_input": {
            "path": timing_input.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_input),
        },
        "timing_output": {
            "path": timing_output.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_output),
        },
        "timing_config": {
            "path": timing_config.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_config),
            "tree_sha256": _sha256_tree(timing_config_root),
        },
        "initial_images": initial_images,
        "output_boundaries": boundaries,
        "functional_gate": "boundary_words",
        "provenance": provenance.as_dict(),
    })
    return manifest_path


def lower_bundle(trace_root, outdir, *, provenance):
    if not isinstance(provenance, PackageProvenance):
        raise TraceTranslationError("M2NDP package provenance is invalid")
    trace_root = Path(trace_root)
    eager = (trace_root / "trace.meta.json").is_file()
    lazy_descriptor = (trace_root / "trace.v2.json").is_file()
    if eager == lazy_descriptor:
        raise TraceTranslationError(
            "trace root must contain exactly one canonical schema"
        )
    if lazy_descriptor:
        return _lower_lazy_bundle(trace_root, outdir, provenance)
    bundle = canonical.read_bundle(trace_root)
    if bundle.meta["trace_sha256"] != provenance.trace_sha256:
        raise TraceTranslationError("source trace SHA-256 differs")
    if bundle.meta["input_sha256"] != provenance.input_sha256:
        raise TraceTranslationError("source input SHA-256 differs")
    lowered = lower_operations(bundle.operations)
    groups, events = _launch_events(lowered)

    outdir = Path(outdir)
    if outdir.exists():
        raise TraceTranslationError(f"fresh M2NDP package root required: {outdir}")
    outdir.mkdir(parents=True)
    checks = _boundary_checks(bundle)
    phase_root = outdir / "kernels"
    phase_root.mkdir()
    sequence_lines = []
    timing_kernel_names = []
    kernels = []
    for kernel_id, (phase, first, last) in enumerate(groups):
        rows = []
        timing_rows = []
        for row in lowered[first:last + 1]:
            rows.append(row.instruction)
            timing_rows.append(row.instruction)
            rows.extend(checks.get(row.sequence, ()))
        kernel = phase_root / f"phase-{phase}-functional.traceg"
        timing_kernel = phase_root / f"phase-{phase}.traceg"
        launch = phase_root / f"phase-{phase}_launch.txt"
        _atomic_write_text(kernel, _kernel(f"CANONICAL_PHASE_{phase}", kernel_id, rows))
        _atomic_write_text(
            timing_kernel,
            _kernel(f"CANONICAL_PHASE_{phase}", kernel_id, timing_rows),
        )
        _atomic_write_text(launch, _launch(kernel_id))
        sequence_lines.append(
            f"{kernel.relative_to(outdir).as_posix()}\t"
            f"{launch.read_text().strip()}"
        )
        timing_kernel_names.append(f"phase-{phase}")
        kernels.append({
            "phase": phase, "path": kernel.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(kernel),
            "timing_path": timing_kernel.relative_to(outdir).as_posix(),
            "timing_sha256": _sha256_file(timing_kernel),
            "launch_path": launch.relative_to(outdir).as_posix(),
            "launch_sha256": _sha256_file(launch),
        })
    instructions = outdir / "funcsim.sequence"
    _atomic_write_text(instructions, "\n".join(sequence_lines) + "\n")
    timing_kernels = phase_root / "kernelslist.g"
    _atomic_write_text(timing_kernels, "\n".join(timing_kernel_names) + "\n")
    operation_records = outdir / "operations.jsonl"
    _atomic_write_text(
        operation_records,
        "".join(
            json.dumps(dataclasses.asdict(row), sort_keys=True,
                       separators=(",", ":")) + "\n"
            for row in lowered
        ),
    )
    memory_map, target_map, initial_images = _write_memory_map(
        bundle, Path(trace_root), outdir
    )
    first_phase = groups[0][0]
    last_phase = groups[-1][0]
    timing_input = phase_root / f"phase-{first_phase}_input.data"
    timing_output = phase_root / f"phase-{last_phase}_output.data"
    shutil.copyfile(memory_map, timing_input)
    shutil.copyfile(target_map, timing_output)
    timing_config_root = outdir / "timing-config"
    shutil.copytree(
        Path(provenance.ndpsim_config_path).parent, timing_config_root
    )
    timing_config = timing_config_root / Path(
        provenance.ndpsim_config_path
    ).name
    if _sha256_tree(timing_config_root) != provenance.ndpsim_config_tree_sha256:
        raise TraceTranslationError("packaged NDPSim config tree differs")
    manifest_path = outdir / "package.json"
    contract.atomic_write_json(manifest_path, {
        "schema": 1,
        "workload": bundle.meta["workload"],
        "operation_count": len(lowered),
        "operations": {
            "path": instructions.name,
            "sha256": _sha256_file(instructions),
            "records_path": operation_records.name,
            "records_sha256": _sha256_file(operation_records),
        },
        "dynamic_launches": len(groups),
        "timing_kernels": {
            "path": timing_kernels.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_kernels),
        },
        "launch_events": events,
        "kernels": kernels,
        "memory_map": {
            "path": memory_map.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(memory_map),
        },
        "target_map": {
            "path": target_map.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(target_map),
        },
        "timing_input": {
            "path": timing_input.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_input),
        },
        "timing_output": {
            "path": timing_output.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_output),
        },
        "timing_config": {
            "path": timing_config.relative_to(outdir).as_posix(),
            "sha256": _sha256_file(timing_config),
            "tree_sha256": _sha256_tree(timing_config_root),
        },
        "initial_images": initial_images,
        "output_boundaries": {
            name: {
                "element_type": (
                    "u32" if bundle.meta["output_boundaries"][name]["word_bits"]
                    == 32 else "u64"
                ),
                "word_bits": bundle.meta["output_boundaries"][name]["word_bits"],
                "raw_words": list(bundle.outputs[name]),
                "sha256": bundle.meta["outputs"][name]["sha256"],
            }
            for name in sorted(bundle.outputs)
        },
        "functional_gate": (
            "boundary_words" if bundle.outputs else "operation_results"
        ),
        **({
            "derived_window": {
                "source_trace_sha256": bundle.meta["source_trace_sha256"],
                "window_index": bundle.meta["window_index"],
                "warmup_start": bundle.meta["warmup_start"],
                "measure_start": bundle.meta["measure_start"],
                "measure_stop": bundle.meta["measure_stop"],
            }
        } if not bundle.outputs and all(
            field in bundle.meta for field in (
                "source_trace_sha256", "window_index", "warmup_start",
                "measure_start", "measure_stop",
            )
        ) else {}),
        "provenance": provenance.as_dict(),
    })
    return manifest_path


if __name__ == "__main__":
    raise SystemExit("use scripts/run_cira_amu_m2ndp_breadth.py")
