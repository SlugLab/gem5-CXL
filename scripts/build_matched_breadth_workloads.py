#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build exact MCF and Spatter canonical-region adapters."""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import cross_system_contract as contract
except ImportError:
    import canonical_work_trace as canonical
    import cross_system_contract as contract


REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "util/amu/matched_workloads"
SOURCES = {
    "mcf": SOURCE_ROOT / "mcf_regions.cc",
    "spatter": SOURCE_ROOT / "spatter_regions.cc",
}
TRACE_ABI = SOURCE_ROOT / "canonical_trace.hh"
BACKENDS = ("reference", "vanilla", "amu", "cira")
STRICT_FLAGS = ("-O3", "-fopenmp", "-ffp-contract=off", "-fno-fast-math")
COMMAND_FLAGS = ("-std=c++17", *STRICT_FLAGS, "-Wall", "-Wextra", "-Werror")
BACKEND_IDS = {name: index for index, name in enumerate(BACKENDS)}
MCF_OUTPUTS = (
    "objective", "flow", "cost", "potential", "predecessor", "depth",
    "orientation", "tree",
)


class BuildError(RuntimeError):
    """A matched breadth build or reference execution failed closed."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve()


def _float_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def fixture_gather_values():
    return tuple(float(index) + 0.25 for index in range(24))


def fixture_gather_index():
    return tuple((index * 7 + 3) % 24 for index in range(24))


def fixture_gather_expected_bits():
    values = fixture_gather_values()
    return tuple(_float_bits(values[index]) for index in fixture_gather_index())


def fixture_scatter_values():
    return tuple(float(index) + 0.5 for index in range(24))


def fixture_scatter_index():
    # Positions 4 and 19 deliberately target the same element.  Canonical
    # program order therefore makes position 19 the last writer.
    result = list(range(24))
    result[19] = result[4]
    return tuple(result)


def fixture_scatter_expected_bits():
    values = fixture_scatter_values()
    index = fixture_scatter_index()
    destination = [0] * (max(index) + 1)
    for position, target in enumerate(index):
        destination[target] = _float_bits(values[position])
    return tuple(destination)


def _pack_f32(values):
    return struct.pack(f"<{len(values)}f", *values)


def _pack_u64(values):
    return struct.pack(f"<{len(values)}Q", *values)


def _fixture_mcf_payload():
    nodes = 4
    arcs = (
        (0, 1, -5, 0),
        (1, 2, 2, 1),
        (2, 3, -3, 0),
        (0, 3, 1, 0),
    )
    pricing_offsets = (0, 3, 6)
    pricing_index = (0, 1, 3, 2, 3, 1)
    price_out_index = (0, 2, 3)
    payload = bytearray(struct.pack(
        "<8sQQQQQ", b"MCFREG1\0", nodes, len(arcs), 2,
        len(pricing_index), len(price_out_index),
    ))
    for arc in arcs:
        payload.extend(struct.pack("<QQqq", *arc))
    for values in (
        (0, 1, -1, 2),
        (-1, -1, -1, -1),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        (-1, -1, -1, -1),
    ):
        payload.extend(struct.pack(f"<{nodes}q", *values))
    payload.extend(_pack_u64(pricing_offsets))
    payload.extend(_pack_u64(pricing_index))
    payload.extend(_pack_u64(price_out_index))
    return bytes(payload)


def _make_fixture_inputs(root):
    root = Path(root)
    files = {
        "mcf": _write_bytes(root / "mcf.regions", _fixture_mcf_payload()),
        "amg_values": _write_bytes(
            root / "amg.values.f32", _pack_f32(fixture_gather_values())
        ),
        "amg_index": _write_bytes(
            root / "amg.index.u64", _pack_u64(fixture_gather_index())
        ),
        "lulesh_values": _write_bytes(
            root / "lulesh.values.f32", _pack_f32(fixture_scatter_values())
        ),
        "lulesh_index": _write_bytes(
            root / "lulesh.index.u64", _pack_u64(fixture_scatter_index())
        ),
    }
    return {
        name: {"path": str(path), "sha256": _sha256_file(path)}
        for name, path in files.items()
    }


def validate_mode(*, formal, fixture, synthetic):
    if formal and fixture:
        raise BuildError("formal mode rejects fixture inputs")
    if formal and synthetic:
        raise BuildError("formal mode rejects synthetic inputs")
    if formal is fixture:
        raise BuildError("select exactly one of formal or fixture mode")
    return True


def _compiler_version(cxx):
    try:
        completed = subprocess.run(
            [cxx, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"cannot identify compiler {cxx}: {error}") from error
    return completed.stdout.splitlines()[0]


def _compiler_identity(cxx):
    resolved = shutil.which(cxx)
    if resolved is None:
        raise BuildError(f"compiler is missing: {cxx}")
    path = Path(resolved).resolve()
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "version": _compiler_version(str(path)),
    }


def _compile(cxx, workload, backend, output, *, fixture):
    source = SOURCES[workload]
    command = [
        cxx,
        *COMMAND_FLAGS,
        f"-DMATCHED_BACKEND={BACKEND_IDS[backend]}",
    ]
    if fixture:
        command.append("-DMATCHED_FIXTURE=1")
    command.extend((
        "-I", str(SOURCE_ROOT), str(source), "-o", str(output),
    ))
    completed = subprocess.run(
        command, cwd=REPO, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"{workload}:{backend} compilation failed:\n{completed.stdout}"
        )
    return command


def _formal_file(path_value, digest_value, label):
    path = Path(path_value or "")
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise BuildError(f"formal {label} input is missing")
    if _sha256_file(path) != digest_value:
        raise BuildError(f"formal {label} input SHA-256 differs")
    return {"path": str(path), "sha256": digest_value}


def load_formal_inputs(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"invalid frozen inputs manifest: {error}") from error
    if value.get("schema") != 1 or value.get("status") != "accepted":
        raise BuildError("formal inputs manifest is not accepted schema 1")
    workloads = value.get("workloads", {})
    for workload, minimum in (
        ("mcf", 345_000_000),
        ("amg_gather", 1 << 30),
        ("lulesh_scatter", 1 << 30),
    ):
        allocated = workloads.get(workload, {}).get("allocated_bytes")
        if (
            not isinstance(allocated, int)
            or isinstance(allocated, bool)
            or allocated < minimum
        ):
            raise BuildError(
                f"formal {workload} allocated_bytes is below paper input"
            )
    if workloads.get("mcf", {}).get("synthetic") is not False:
        raise BuildError("formal mcf synthetic input is forbidden")
    records = {
        "mcf": (workloads.get("mcf", {}).get("input"),
                workloads.get("mcf", {}).get("input_sha256")),
        "amg_values": (workloads.get("amg_gather", {}).get("input"),
                       workloads.get("amg_gather", {}).get("input_sha256")),
        "amg_index": (workloads.get("amg_gather", {}).get("index"),
                      workloads.get("amg_gather", {}).get("index_sha256")),
        "lulesh_values": (workloads.get("lulesh_scatter", {}).get("input"),
                          workloads.get("lulesh_scatter", {}).get("input_sha256")),
        "lulesh_index": (workloads.get("lulesh_scatter", {}).get("index"),
                         workloads.get("lulesh_scatter", {}).get("index_sha256")),
        "mcf_source": (workloads.get("mcf", {}).get("source"),
                       workloads.get("mcf", {}).get("source_sha256")),
    }
    result = {
        name: _formal_file(path_value, expected, name)
        for name, (path_value, expected) in records.items()
    }
    for prefix, workload in (
        ("amg", "amg_gather"),
        ("lulesh", "lulesh_scatter"),
    ):
        values = Path(result[f"{prefix}_values"]["path"])
        index = Path(result[f"{prefix}_index"]["path"])
        if values.stat().st_size == 0 or values.stat().st_size % 4:
            raise BuildError(f"formal {workload} values are not nonempty f32")
        if index.stat().st_size == 0 or index.stat().st_size % 8:
            raise BuildError(f"formal {workload} index is not nonempty u64")
        count = index.stat().st_size // 8
        value_count = values.stat().st_size // 4
        if workload == "lulesh_scatter" and value_count != count:
            raise BuildError("formal lulesh_scatter value/index counts differ")
        result[f"{prefix}_values"]["element_count"] = value_count
        result[f"{prefix}_index"]["element_count"] = count
        result[f"{prefix}_values"]["allocated_bytes"] = workloads[
            workload
        ]["allocated_bytes"]
    return result, _sha256_file(path)


def build_suite(
    outdir, *, inputs, cxx="g++", fixture=False,
    input_manifest_sha256=None,
):
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh build root required: {outdir}")
    (outdir / "bin").mkdir(parents=True)
    binaries = {}
    commands = {}
    for workload in SOURCES:
        for backend in BACKENDS:
            output = outdir / "bin" / f"{workload}-{backend}"
            command = _compile(
                cxx, workload, backend, output, fixture=fixture
            )
            key = f"{workload}:{backend}"
            commands[key] = command
            binaries[key] = {
                "path": str(output.resolve()),
                "sha256": _sha256_file(output),
                "source_sha256": _sha256_file(SOURCES[workload]),
                "trace_abi_sha256": _sha256_file(TRACE_ABI),
            }
    manifest = {
        "schema": 1,
        "mode": "fixture" if fixture else "formal",
        "root": str(outdir),
        "threads": 4,
        "compiler": _compiler_identity(cxx),
        "flags": list(STRICT_FLAGS),
        "command_flags": list(COMMAND_FLAGS),
        "inputs": inputs,
        "input_manifest_sha256": (
            input_manifest_sha256
            or hashlib.sha256(contract.canonical_json(inputs)).hexdigest()
        ),
        "binaries": binaries,
        "commands": commands,
    }
    contract.atomic_write_json(outdir / "manifest.json", manifest)
    return manifest


def build_fixture_suite(outdir, cxx="g++"):
    outdir = Path(outdir).resolve()
    inputs = _make_fixture_inputs(outdir.parent / f".{outdir.name}.inputs")
    return build_suite(outdir, inputs=inputs, cxx=cxx, fixture=True)


def _read_words(path, word_bits):
    payload = Path(path).read_bytes()
    width = word_bits // 8
    if len(payload) % width:
        raise BuildError(f"raw output width differs: {path}")
    code = "I" if word_bits == 32 else "Q"
    return tuple(struct.unpack(f"<{len(payload) // width}{code}", payload))


def _run(command, label):
    completed = subprocess.run(
        [str(item) for item in command], cwd=REPO, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        env={**os.environ, "OMP_NUM_THREADS": "4"},
    )
    if completed.returncode != 0:
        raise BuildError(f"{label} exited {completed.returncode}:\n{completed.stdout}")
    return completed.stdout


def _markers(output):
    work = {}
    invocations = {}
    duplicate_policy = None
    state_shape = None
    for line in output.splitlines():
        if line.startswith("MATCHED_PHASE_WORK="):
            phase, value = line.split("=", 1)[1].rsplit(":", 1)
            work[phase] = int(value)
        elif line.startswith("MATCHED_PHASE_INVOCATIONS="):
            phase, value = line.split("=", 1)[1].rsplit(":", 1)
            invocations[phase] = int(value)
        elif line.startswith("MATCHED_DUPLICATE_POLICY="):
            duplicate_policy = line.split("=", 1)[1]
        elif line.startswith("MATCHED_STATE_SHAPE="):
            state_shape = {
                name: int(value)
                for name, value in (
                    item.split(":", 1)
                    for item in line.split("=", 1)[1].split(",")
                )
            }
    if not work or set(work) != set(invocations):
        raise BuildError("reference phase markers are incomplete")
    return work, invocations, duplicate_policy, state_shape


def _combined_input_sha256(inputs, names):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(inputs[name]["sha256"]))
    return digest.hexdigest()


def _write_reference_bundle(
    *, bundle_root, workload, phases, input_sha256, binary_row,
    manifest_sha256, trace_path, output_paths, stdout,
):
    operations = canonical.decode_operations(Path(trace_path).read_bytes())
    work, invocations, duplicate_policy, state_shape = _markers(stdout)
    outputs = {
        name: _read_words(path, bits)
        for name, (path, bits) in output_paths.items()
    }
    meta = {
        "schema": 1,
        "workload": workload,
        "input_sha256": input_sha256,
        "source_sha256": binary_row["source_sha256"],
        "binary_sha256": binary_row["sha256"],
        "config_sha256": manifest_sha256,
        "phases": list(phases),
        "phase_work": work,
        "phase_invocations": invocations,
        "output_boundaries": {
            name: {"word_bits": bits, "count": len(outputs[name])}
            for name, (_, bits) in output_paths.items()
        },
    }
    if duplicate_policy is not None:
        meta["duplicate_policy"] = duplicate_policy
    if state_shape is not None:
        meta["state_shape"] = state_shape
    canonical.write_bundle(bundle_root, meta, operations, outputs)
    return Path(bundle_root).resolve()


def _run_mcf_reference(manifest, root):
    root.mkdir(parents=True)
    raw = root / "raw"
    raw.mkdir()
    trace_path = raw / "trace.bin"
    row = manifest["binaries"]["mcf:reference"]
    stdout = _run([
        row["path"], "--input", manifest["inputs"]["mcf"]["path"],
        "--output-root", raw, "--trace", trace_path,
    ], "MCF reference")
    return _write_reference_bundle(
        bundle_root=root / "bundle", workload="mcf",
        phases=("pricing_kernel", "price_out_impl"),
        input_sha256=manifest["inputs"]["mcf"]["sha256"],
        binary_row=row,
        manifest_sha256=_sha256_file(Path(manifest["root"]) / "manifest.json"),
        trace_path=trace_path,
        output_paths={name: (raw / f"{name}.u64", 64) for name in MCF_OUTPUTS},
        stdout=stdout,
    )


def _run_spatter_reference(manifest, root, *, kind, faulty=False):
    root.mkdir(parents=True)
    raw = root / "raw"
    raw.mkdir()
    prefix = "amg" if kind == "gather" else "lulesh"
    workload = "amg_gather" if kind == "gather" else "lulesh_scatter"
    phase = workload
    row = manifest["binaries"]["spatter:reference"]
    command = [
        row["path"], "--kind", kind,
        "--values", manifest["inputs"][f"{prefix}_values"]["path"],
        "--index", manifest["inputs"][f"{prefix}_index"]["path"],
        "--destination", raw / "destination.u32",
        "--trace", raw / "trace.bin",
    ]
    if faulty:
        command.append("--reverse-duplicate-stores")
    stdout = _run(command, f"{workload} reference")
    return _write_reference_bundle(
        bundle_root=root / "bundle", workload=workload, phases=(phase,),
        input_sha256=_combined_input_sha256(
            manifest["inputs"], (f"{prefix}_values", f"{prefix}_index")
        ),
        binary_row=row,
        manifest_sha256=_sha256_file(Path(manifest["root"]) / "manifest.json"),
        trace_path=raw / "trace.bin",
        output_paths={"destination": (raw / "destination.u32", 32)},
        stdout=stdout,
    )


def run_fixture_references(manifest, outdir):
    if manifest.get("mode") != "fixture":
        raise BuildError("fixture reference execution requires a fixture build")
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh reference root required: {outdir}")
    outdir.mkdir(parents=True)
    return {
        "mcf": _run_mcf_reference(manifest, outdir / "mcf"),
        "amg_gather": _run_spatter_reference(
            manifest, outdir / "amg_gather", kind="gather"
        ),
        "lulesh_scatter": _run_spatter_reference(
            manifest, outdir / "lulesh_scatter", kind="scatter"
        ),
    }


def run_faulty_scatter_reversed_duplicates(manifest, outdir):
    if manifest.get("mode") != "fixture":
        raise BuildError("fault injection requires a fixture build")
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh faulty root required: {outdir}")
    outdir.mkdir(parents=True)
    return _run_spatter_reference(
        manifest, outdir / "lulesh_scatter", kind="scatter", faulty=True
    )


def verify_reference_bundle(reference, actual):
    expected = canonical.read_bundle(reference)
    observed = canonical.read_bundle(actual)
    if expected.meta["workload"] != observed.meta["workload"]:
        raise canonical.TraceError("workload identity differs")
    for field in (
        "input_sha256",
        "source_sha256",
        "binary_sha256",
        "config_sha256",
        "phases",
        "phase_work",
        "phase_invocations",
        "duplicate_policy",
        "state_shape",
    ):
        if expected.meta.get(field) != observed.meta.get(field):
            raise canonical.TraceError(f"{field} identity differs")
    if set(expected.outputs) != set(observed.outputs):
        raise canonical.TraceError("output boundary set differs")
    for name in expected.outputs:
        bits = expected.meta["output_boundaries"][name]["word_bits"]
        canonical.compare_words(
            expected.outputs[name], observed.outputs[name], name,
            word_bits=bits,
        )
    canonical.validate_translation(expected.operations, observed.operations)
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--formal", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--cxx", default="g++")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        validate_mode(
            formal=options.formal,
            fixture=options.fixture,
            synthetic=options.synthetic,
        )
        if options.formal:
            if options.inputs is None:
                raise BuildError("formal mode requires --inputs")
            load_formal_inputs(options.inputs)
            raise BuildError(
                "formal MCF is failed_input: the frozen SPEC MCF source and "
                "345 MB input are not available to the exact instrumentation path"
            )
        else:
            if options.inputs is not None:
                raise BuildError("fixture mode rejects --inputs")
            manifest = build_fixture_suite(options.outdir, cxx=options.cxx)
        print(f"MATCHED_BREADTH_BUILD_PASS manifest={manifest['root']}/manifest.json")
        return 0
    except (BuildError, OSError) as error:
        print(f"MATCHED_BREADTH_BUILD_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
