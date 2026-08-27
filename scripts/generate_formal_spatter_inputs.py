#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate reproducible formal inputs from official Spatter traces."""

import argparse
import dataclasses
import hashlib
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
except ImportError:
    import cross_system_contract as contract


U64_MAX = (1 << 64) - 1
SUPPORTED_KERNELS = ("Gather", "Scatter")
EXPANSION_VERSION = "spatter-trace-epoch-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


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


@dataclasses.dataclass(frozen=True)
class GenerationSpec:
    workload: str
    mode: str
    selected_kernel: str
    source_trace: Path
    source_trace_sha256: str
    source_commit: str
    minimum_bytes: int
    source_root: Path | None = None


@dataclasses.dataclass(frozen=True)
class GeneratedArtifacts:
    workload: str
    mode: str
    epochs: int
    values_count: int
    index_count: int
    maximum_index: int
    resident_bytes: int
    values_path: Path
    values_sha256: str
    index_path: Path
    index_sha256: str


@dataclasses.dataclass(frozen=True)
class VerifiedGeneration:
    spec: GenerationSpec
    artifacts: GeneratedArtifacts
    provenance: dict
    artifact_id: str
    staging_root: Path


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


def _validate_spec(spec):
    if not isinstance(spec, GenerationSpec):
        raise GenerationError("generation spec is invalid")
    if not isinstance(spec.workload, str) or not spec.workload:
        raise GenerationError("generation workload is invalid")
    expected = {"gather": "Gather", "scatter": "Scatter"}
    if spec.mode not in expected or spec.selected_kernel != expected[spec.mode]:
        raise GenerationError("generation mode and selected kernel differ")
    if _SHA256.fullmatch(spec.source_trace_sha256) is None:
        raise GenerationError("source trace SHA-256 is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", spec.source_commit) is None:
        raise GenerationError("source commit is invalid")
    if spec.source_root is not None:
        root = Path(spec.source_root)
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise GenerationError("source root must be a resolved directory")
        try:
            Path(spec.source_trace).relative_to(root)
        except ValueError as error:
            raise GenerationError("source trace is outside source root") from error
    _integer(spec.minimum_bytes, "minimum bytes", positive=True)
    return spec


def _write_words(path, word_bits, values):
    if word_bits not in (32, 64):
        raise GenerationError("output word width is invalid")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    code = "I" if word_bits == 32 else "Q"
    maximum = (1 << word_bits) - 1
    count = 0
    observed_maximum = 0
    chunk = []
    with path.open("xb") as stream:
        for value in values:
            if (
                isinstance(value, bool) or not isinstance(value, int)
                or not 0 <= value <= maximum
            ):
                raise GenerationError("output word is outside encoded range")
            chunk.append(value)
            observed_maximum = max(observed_maximum, value)
            count += 1
            if len(chunk) == 65536:
                stream.write(struct.pack(f"<{len(chunk)}{code}", *chunk))
                chunk.clear()
        if chunk:
            stream.write(struct.pack(f"<{len(chunk)}{code}", *chunk))
        stream.flush()
        os.fsync(stream.fileno())
    return count, observed_maximum


def generate_once(spec, outdir):
    spec = _validate_spec(spec)
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise GenerationError(f"fresh generation root required: {outdir}")
    outdir.mkdir(parents=True)
    try:
        records = load_records(
            spec.source_trace, spec.source_trace_sha256,
            spec.selected_kernel,
        )
        trace_layout = layout(records)
        epochs = required_epochs(
            trace_layout, spec.mode, spec.minimum_bytes
        )
        index_count = trace_layout.index_count * epochs
        maximum_index = trace_layout.index_span * epochs - 1
        values_count = (
            maximum_index + 1 if spec.mode == "gather" else index_count
        )
        index_path = outdir / "index.u64le"
        written_indices, observed_maximum = _write_words(
            index_path, 64, indices(trace_layout, epochs=epochs)
        )
        if written_indices != index_count or observed_maximum != maximum_index:
            raise GenerationError("generated index shape differs")
        values_path = outdir / "values.f32le"
        written_values, _ = _write_words(
            values_path, 32,
            (value_bits(position) for position in range(values_count)),
        )
        if written_values != values_count:
            raise GenerationError("generated values shape differs")
        allocated = resident_bytes(trace_layout, epochs, spec.mode)
        return GeneratedArtifacts(
            workload=spec.workload,
            mode=spec.mode,
            epochs=epochs,
            values_count=values_count,
            index_count=index_count,
            maximum_index=maximum_index,
            resident_bytes=allocated,
            values_path=values_path,
            values_sha256=_sha256_file(values_path),
            index_path=index_path,
            index_sha256=_sha256_file(index_path),
        )
    except Exception:
        shutil.rmtree(outdir, ignore_errors=True)
        raise


def _semantic_artifact_fields(value):
    return (
        value.workload, value.mode, value.epochs, value.values_count,
        value.index_count, value.maximum_index, value.resident_bytes,
        value.values_sha256, value.index_sha256,
    )


def compare_generations(first, second):
    if not isinstance(first, GeneratedArtifacts) or not isinstance(
        second, GeneratedArtifacts
    ):
        raise GenerationError("independent regeneration records are invalid")
    if (
        _sha256_file(first.values_path) != first.values_sha256
        or _sha256_file(first.index_path) != first.index_sha256
        or _sha256_file(second.values_path) != second.values_sha256
        or _sha256_file(second.index_path) != second.index_sha256
        or _semantic_artifact_fields(first) != _semantic_artifact_fields(second)
    ):
        raise GenerationError("independent regeneration differs")
    return True


def _generation_identity(spec, artifacts):
    identity = {
        "schema": 1,
        "source_kind": "official_spatter_application_trace",
        "workload": artifacts.workload,
        "mode": artifacts.mode,
        "selected_kernel": spec.selected_kernel,
        "source_trace": str(spec.source_trace),
        "source_trace_sha256": spec.source_trace_sha256,
        "source_commit": spec.source_commit,
        "generator_sha256": _sha256_file(Path(__file__).resolve()),
        "expansion_version": EXPANSION_VERSION,
        "selection_rule": f"all {spec.selected_kernel} records in source order",
        "minimum_bytes": spec.minimum_bytes,
        "epochs": artifacts.epochs,
        "values_count": artifacts.values_count,
        "index_count": artifacts.index_count,
        "maximum_index": artifacts.maximum_index,
        "resident_bytes": artifacts.resident_bytes,
        "values_sha256": artifacts.values_sha256,
        "index_sha256": artifacts.index_sha256,
    }
    if spec.source_root is not None:
        identity["source_root"] = str(spec.source_root)
    return identity


def generate_twice(spec, staging_root):
    spec = _validate_spec(spec)
    staging_root = Path(staging_root).resolve()
    if staging_root.exists():
        raise GenerationError(f"fresh staging root required: {staging_root}")
    staging_root.mkdir(parents=True)
    try:
        primary = generate_once(spec, staging_root / "primary")
        replay = generate_once(spec, staging_root / "replay")
        compare_generations(primary, replay)
        identity = _generation_identity(spec, primary)
        artifact_id = hashlib.sha256(contract.canonical_json(identity)).hexdigest()
        provenance = {
            **identity,
            "status": "generated",
            "artifact_id": artifact_id,
            "artifacts": {
                "values": {
                    "name": primary.values_path.name,
                    "sha256": primary.values_sha256,
                    "size_bytes": primary.values_path.stat().st_size,
                },
                "index": {
                    "name": primary.index_path.name,
                    "sha256": primary.index_sha256,
                    "size_bytes": primary.index_path.stat().st_size,
                },
            },
            "independent_regeneration": {
                "status": "pass",
                "values_sha256": replay.values_sha256,
                "index_sha256": replay.index_sha256,
            },
        }
        contract.atomic_write_json(primary.values_path.parent / "provenance.json", provenance)
        shutil.rmtree(replay.values_path.parent)
        return VerifiedGeneration(
            spec=spec,
            artifacts=primary,
            provenance=provenance,
            artifact_id=artifact_id,
            staging_root=staging_root,
        )
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise


def _require_validation(artifacts, validation):
    if (
        not isinstance(validation, dict)
        or validation.get("schema") != 1
        or validation.get("status") != "accepted"
        or validation.get("workload") != artifacts.workload
        or validation.get("values_sha256") != artifacts.values_sha256
        or validation.get("index_sha256") != artifacts.index_sha256
        or _SHA256.fullmatch(str(validation.get("destination_sha256", ""))) is None
        or _SHA256.fullmatch(
            str(validation.get("reference_binary_sha256", ""))
        ) is None
    ):
        raise GenerationError("reference validation is not accepted")
    return validation


def _published_hashes_match(target, artifacts, provenance_sha, validation_sha):
    expected = {
        "values.f32le": artifacts.values_sha256,
        "index.u64le": artifacts.index_sha256,
        "provenance.json": provenance_sha,
        "validation.json": validation_sha,
    }
    return all(
        (target / name).is_file() and _sha256_file(target / name) == digest
        for name, digest in expected.items()
    )


def promote_validated(generation, validation, output_root):
    if not isinstance(generation, VerifiedGeneration):
        raise GenerationError("verified generation is invalid")
    validation = _require_validation(generation.artifacts, validation)
    primary = generation.artifacts.values_path.parent
    contract.atomic_write_json(primary / "validation.json", validation)
    validation_sha = _sha256_file(primary / "validation.json")
    provenance = {
        **generation.provenance,
        "status": "accepted",
        "validation": {
            "name": "validation.json", "sha256": validation_sha,
        },
    }
    contract.atomic_write_json(primary / "provenance.json", provenance)
    provenance_sha = _sha256_file(primary / "provenance.json")
    output_root = Path(output_root).resolve()
    workload_root = output_root / generation.artifacts.workload
    workload_root.mkdir(parents=True, exist_ok=True)
    target = workload_root / generation.artifact_id
    if target.exists():
        if not _published_hashes_match(
            target, generation.artifacts, provenance_sha, validation_sha
        ):
            raise GenerationError(f"content-addressed publication conflict: {target}")
        shutil.rmtree(generation.staging_root, ignore_errors=True)
        return target
    os.replace(primary, target)
    shutil.rmtree(generation.staging_root, ignore_errors=True)
    if not _published_hashes_match(
        target, generation.artifacts, provenance_sha, validation_sha
    ):
        raise GenerationError("published artifact hashes differ")
    return target


def _iter_u64(path):
    with Path(path).open("rb") as stream:
        while True:
            payload = stream.read(1 << 20)
            if not payload:
                return
            if len(payload) % 8:
                raise GenerationError("index file is not aligned u64")
            yield from (row[0] for row in struct.iter_unpack("<Q", payload))


def _gather_destination_sha256(artifacts):
    digest = hashlib.sha256()
    chunk = []
    count = 0
    for source in _iter_u64(artifacts.index_path):
        if source >= artifacts.values_count:
            raise GenerationError("gather index is outside generated values")
        chunk.append(value_bits(source))
        count += 1
        if len(chunk) == 65536:
            digest.update(struct.pack(f"<{len(chunk)}I", *chunk))
            chunk.clear()
    if chunk:
        digest.update(struct.pack(f"<{len(chunk)}I", *chunk))
    if count != artifacts.index_count:
        raise GenerationError("gather oracle index count differs")
    return digest.hexdigest(), count


def _scatter_destination_sha256(artifacts, work_root):
    expected = work_root / "expected-destination.u32"
    byte_count = (artifacts.maximum_index + 1) * 4
    with expected.open("w+b") as stream:
        stream.truncate(byte_count)
        with mmap.mmap(stream.fileno(), byte_count, access=mmap.ACCESS_WRITE) as image:
            count = 0
            for position, target in enumerate(_iter_u64(artifacts.index_path)):
                if target > artifacts.maximum_index:
                    raise GenerationError("scatter index exceeds destination")
                struct.pack_into("<I", image, target * 4, value_bits(position))
                count += 1
            image.flush()
        stream.flush()
        os.fsync(stream.fileno())
    if count != artifacts.index_count:
        raise GenerationError("scatter oracle index count differs")
    return _sha256_file(expected), artifacts.maximum_index + 1


def _validate_binary_record(binary):
    if not isinstance(binary, dict):
        raise GenerationError("reference binary record is invalid")
    path = Path(binary.get("path", ""))
    if (
        not path.is_absolute() or path.resolve() != path or not path.is_file()
        or _SHA256.fullmatch(str(binary.get("sha256", ""))) is None
        or _sha256_file(path) != binary["sha256"]
    ):
        raise GenerationError("reference binary identity differs")
    for name in ("source_sha256", "trace_abi_sha256"):
        if _SHA256.fullmatch(str(binary.get(name, ""))) is None:
            raise GenerationError(f"reference binary {name} is invalid")
    return path


def validate_reference(artifacts, binary, work_root):
    if not isinstance(artifacts, GeneratedArtifacts):
        raise GenerationError("generated artifacts are invalid")
    if _sha256_file(artifacts.values_path) != artifacts.values_sha256:
        raise GenerationError("generated values SHA-256 differs")
    if _sha256_file(artifacts.index_path) != artifacts.index_sha256:
        raise GenerationError("generated index SHA-256 differs")
    binary_path = _validate_binary_record(binary)
    work_root = Path(work_root).resolve()
    if work_root.exists():
        raise GenerationError(f"fresh reference root required: {work_root}")
    work_root.mkdir(parents=True)
    destination = work_root / "destination.u32"
    command = [
        str(binary_path), "--kind", artifacts.mode,
        "--values", str(artifacts.values_path),
        "--index", str(artifacts.index_path),
        "--destination", str(destination),
        "--trace", "/dev/null",
    ]
    completed = subprocess.run(
        command, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode != 0:
        raise GenerationError(
            f"reference adapter failed: {completed.stdout.strip()}"
        )
    phase = (
        "amg_gather" if artifacts.mode == "gather"
        else "lulesh_scatter"
    )
    expected_line = f"MATCHED_PHASE_WORK={phase}:{artifacts.index_count}"
    if expected_line not in completed.stdout.splitlines():
        raise GenerationError("reference adapter phase work differs")
    if artifacts.mode == "gather":
        expected_sha, output_words = _gather_destination_sha256(artifacts)
    elif artifacts.mode == "scatter":
        expected_sha, output_words = _scatter_destination_sha256(
            artifacts, work_root
        )
    else:
        raise GenerationError("reference adapter mode is invalid")
    if (
        not destination.is_file()
        or destination.stat().st_size != output_words * 4
        or _sha256_file(destination) != expected_sha
    ):
        raise GenerationError("reference destination is not bit-exact")
    return {
        "schema": 1,
        "status": "accepted",
        "workload": artifacts.workload,
        "mode": artifacts.mode,
        "values_sha256": artifacts.values_sha256,
        "index_sha256": artifacts.index_sha256,
        "destination_sha256": expected_sha,
        "output_words": output_words,
        "reference_binary": str(binary_path),
        "reference_binary_sha256": binary["sha256"],
        "reference_source_sha256": binary["source_sha256"],
        "trace_abi_sha256": binary["trace_abi_sha256"],
        "command_sha256": hashlib.sha256(
            contract.canonical_json(command)
        ).hexdigest(),
        "stdout_sha256": hashlib.sha256(
            completed.stdout.encode("utf-8")
        ).hexdigest(),
    }


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise GenerationError(f"{label} must be a JSON object")
    return value


def _git_head(root):
    root = Path(root).resolve()
    completed = subprocess.run(
        [
            "git", "-c", f"safe.directory={root}",
            "-C", str(root), "rev-parse", "HEAD",
        ],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise GenerationError(
            f"cannot inspect source commit: {completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _fresh_child(parent, prefix):
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    path.rmdir()
    return path


def _install_reference_binary(output_root, cxx):
    try:
        from scripts import build_matched_breadth_workloads as builder
    except ImportError:
        import build_matched_breadth_workloads as builder
    tools = Path(output_root).resolve() / ".tools"
    temporary = _fresh_child(tools, ".spatter-reference-")
    row = builder.build_spatter_reference_binary(cxx, temporary)
    target = tools / f"spatter-reference-{row['sha256']}"
    if target.exists():
        if not target.is_file() or _sha256_file(target) != row["sha256"]:
            temporary.unlink(missing_ok=True)
            raise GenerationError("stored reference binary identity differs")
        temporary.unlink()
    else:
        os.replace(temporary, target)
    return {**row, "path": str(target)}


def _candidate_row(target, artifacts):
    provenance = target / "provenance.json"
    validation = target / "validation.json"
    return {
        "input": str(target / "values.f32le"),
        "input_sha256": artifacts.values_sha256,
        "index": str(target / "index.u64le"),
        "index_sha256": artifacts.index_sha256,
        "allocated_bytes": artifacts.resident_bytes,
        "synthetic": False,
        "provenance": str(provenance),
        "provenance_sha256": _sha256_file(provenance),
        "validation": str(validation),
        "validation_sha256": _sha256_file(validation),
        "artifact_id": target.name,
    }


def _generate_command(options):
    source_root = options.source_root.resolve()
    if _git_head(source_root) != options.source_commit:
        raise GenerationError("source commit differs")
    trace_rows = (
        (
            "amg_gather", "gather", "Gather", options.amg_trace,
            options.amg_sha256,
        ),
        (
            "lulesh_scatter", "scatter", "Scatter",
            options.lulesh_trace, options.lulesh_sha256,
        ),
    )
    specs = []
    for workload, mode, kernel, relative, digest in trace_rows:
        trace = (source_root / relative).resolve()
        try:
            trace.relative_to(source_root)
        except ValueError as error:
            raise GenerationError("source trace path escapes source root") from error
        specs.append(GenerationSpec(
            workload=workload,
            mode=mode,
            selected_kernel=kernel,
            source_trace=trace,
            source_trace_sha256=digest,
            source_commit=options.source_commit,
            minimum_bytes=options.minimum_bytes,
            source_root=source_root,
        ))
    output_root = options.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    binary = _install_reference_binary(output_root, options.cxx)
    rows = {}
    for spec in specs:
        staging = _fresh_child(output_root, f".stage-{spec.workload}-")
        generation = generate_twice(spec, staging)
        reference_root = _fresh_child(
            output_root, f".reference-{spec.workload}-"
        )
        try:
            validation = validate_reference(
                generation.artifacts, binary, reference_root
            )
            target = promote_validated(generation, validation, output_root)
        finally:
            shutil.rmtree(reference_root, ignore_errors=True)
        rows[spec.workload] = _candidate_row(
            target, generation.artifacts
        )
        print(
            "FORMAL_SPATTER_INPUT_PASS "
            f"workload={spec.workload} epochs={generation.artifacts.epochs} "
            f"resident_bytes={generation.artifacts.resident_bytes}"
        )
    candidate = _load_json(options.candidate_record, "candidate record")
    candidate.update(rows)
    contract.atomic_write_json(options.candidate_record, candidate)
    return 0


def _identity_from_provenance(value):
    fields = (
        "schema", "source_kind", "workload", "mode", "selected_kernel",
        "source_trace", "source_trace_sha256", "source_commit",
        "generator_sha256", "expansion_version", "selection_rule",
        "minimum_bytes", "epochs", "values_count", "index_count",
        "maximum_index", "resident_bytes", "values_sha256",
        "index_sha256",
    )
    identity = {name: value.get(name) for name in fields}
    if "source_root" in value:
        identity["source_root"] = value["source_root"]
    return identity


def verify_published(path):
    path = Path(path).resolve()
    if not path.is_dir():
        raise GenerationError("published artifact directory is missing")
    provenance_path = path / "provenance.json"
    validation_path = path / "validation.json"
    provenance = _load_json(provenance_path, "Spatter provenance")
    validation = _load_json(validation_path, "Spatter validation")
    identity = _identity_from_provenance(provenance)
    artifact_id = hashlib.sha256(contract.canonical_json(identity)).hexdigest()
    if (
        provenance.get("status") != "accepted"
        or provenance.get("artifact_id") != artifact_id
        or path.name != artifact_id
        or provenance.get("generator_sha256")
        != _sha256_file(Path(__file__).resolve())
        or provenance.get("expansion_version") != EXPANSION_VERSION
    ):
        raise GenerationError("published provenance identity differs")
    source_trace = Path(provenance.get("source_trace", ""))
    if (
        not source_trace.is_file()
        or _sha256_file(source_trace) != provenance.get("source_trace_sha256")
    ):
        raise GenerationError("published source trace identity differs")
    source_root = provenance.get("source_root")
    if source_root is not None and _git_head(source_root) != provenance.get(
        "source_commit"
    ):
        raise GenerationError("published source commit differs")
    artifacts = provenance.get("artifacts", {})
    for kind, filename, width, count_name in (
        ("values", "values.f32le", 4, "values_count"),
        ("index", "index.u64le", 8, "index_count"),
    ):
        row = artifacts.get(kind, {})
        artifact = path / filename
        if (
            row.get("name") != filename
            or row.get("sha256") != provenance.get(f"{kind}_sha256")
            or not artifact.is_file()
            or artifact.stat().st_size != provenance.get(count_name) * width
            or row.get("size_bytes") != artifact.stat().st_size
            or _sha256_file(artifact) != row.get("sha256")
        ):
            raise GenerationError(f"published {kind} artifact differs")
    replay = provenance.get("independent_regeneration", {})
    if (
        replay.get("status") != "pass"
        or replay.get("values_sha256") != provenance.get("values_sha256")
        or replay.get("index_sha256") != provenance.get("index_sha256")
    ):
        raise GenerationError("published independent regeneration differs")
    validation_record = provenance.get("validation", {})
    if (
        validation_record.get("name") != "validation.json"
        or _sha256_file(validation_path) != validation_record.get("sha256")
        or validation.get("status") != "accepted"
        or validation.get("workload") != provenance.get("workload")
        or validation.get("values_sha256") != provenance.get("values_sha256")
        or validation.get("index_sha256") != provenance.get("index_sha256")
    ):
        raise GenerationError("published reference validation differs")
    binary = Path(validation.get("reference_binary", ""))
    if (
        not binary.is_file()
        or _sha256_file(binary) != validation.get("reference_binary_sha256")
    ):
        raise GenerationError("published reference binary differs")
    mode = provenance.get("mode")
    values_count = provenance.get("values_count")
    index_count = provenance.get("index_count")
    maximum_index = provenance.get("maximum_index")
    if mode == "gather":
        allocated = 4 * values_count + 12 * index_count
    elif mode == "scatter":
        allocated = 12 * index_count + 4 * (maximum_index + 1)
    else:
        raise GenerationError("published mode differs")
    if (
        allocated != provenance.get("resident_bytes")
        or allocated < provenance.get("minimum_bytes")
    ):
        raise GenerationError("published resident allocation differs")
    return {
        "status": "accepted", "artifact_id": artifact_id,
        "workload": provenance["workload"],
        "resident_bytes": allocated,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--source-root", type=Path, required=True)
    generate.add_argument("--source-commit", required=True)
    generate.add_argument("--amg-trace", type=Path, required=True)
    generate.add_argument("--amg-sha256", required=True)
    generate.add_argument("--lulesh-trace", type=Path, required=True)
    generate.add_argument("--lulesh-sha256", required=True)
    generate.add_argument("--minimum-bytes", type=int, required=True)
    generate.add_argument("--output-root", type=Path, required=True)
    generate.add_argument("--candidate-record", type=Path, required=True)
    generate.add_argument("--cxx", default="g++")
    verify = commands.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        if options.command == "generate":
            return _generate_command(options)
        result = verify_published(options.artifact)
    except GenerationError as error:
        if options.command == "generate":
            output_root = options.output_root.resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            contract.atomic_write_json(output_root / "failed-input.json", {
                "schema": 1, "status": "failed_input", "reason": str(error),
            })
        print(f"FORMAL_SPATTER_INPUT_FAILED error={error}")
        return 2
    print(
        "FORMAL_SPATTER_INPUT_VERIFY_PASS "
        f"workload={result['workload']} artifact_id={result['artifact_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
