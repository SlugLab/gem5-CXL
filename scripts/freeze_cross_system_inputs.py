#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Freeze real inputs for the CIRA, AMU, and M2NDP comparison."""

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import generate_formal_spatter_inputs as formal_spatter
    from scripts import mcfreg2
except ImportError:
    import cross_system_contract as contract
    import gapbs_pr_experiment_profiles as profiles
    import generate_formal_spatter_inputs as formal_spatter
    import mcfreg2


WORKLOADS = (
    "pr_spmv",
    "mcf",
    "amg_gather",
    "lulesh_scatter",
    "npb_cg",
    "npb_mg",
)
REQUIRED = {
    "pr_spmv": {"input", "input_sha256", "allocated_bytes", "scale"},
    "mcf": {
        "input",
        "input_sha256",
        "allocated_bytes",
        "source",
        "source_sha256",
        "format",
        "source_commit",
        "source_tree_sha256",
        "validation",
        "validation_sha256",
        "synthetic",
    },
    "amg_gather": {
        "input",
        "input_sha256",
        "index",
        "index_sha256",
        "allocated_bytes",
        "synthetic",
        "provenance",
        "provenance_sha256",
        "validation",
        "validation_sha256",
        "artifact_id",
    },
    "lulesh_scatter": {
        "input",
        "input_sha256",
        "index",
        "index_sha256",
        "allocated_bytes",
        "synthetic",
        "provenance",
        "provenance_sha256",
        "validation",
        "validation_sha256",
        "artifact_id",
    },
    "npb_cg": {
        "source_root",
        "source_commit",
        "parameter_file",
        "parameter_sha256",
        "allocated_bytes",
        "class",
    },
    "npb_mg": {
        "source_root",
        "source_commit",
        "parameter_file",
        "parameter_sha256",
        "allocated_bytes",
        "class",
    },
}
MINIMUM_ALLOCATED_BYTES = {
    "pr_spmv": 240_000_000,
    "mcf": 345_000_000,
    "amg_gather": 1 << 30,
    "lulesh_scatter": 1 << 30,
    "npb_cg": 12_800_000_000,
    "npb_mg": 12_800_000_000,
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MCF_SOURCE_ROOT = _REPO_ROOT / "util/amu/matched_workloads"
_SPATTER_SOURCE = _MCF_SOURCE_ROOT / "spatter_regions.cc"
_SPATTER_TRACE_ABI = _MCF_SOURCE_ROOT / "canonical_trace.hh"
_SPATTER_IDENTITY_FIELDS = (
    "schema", "source_kind", "workload", "mode", "selected_kernel",
    "source_trace", "source_trace_sha256", "source_commit",
    "generator_sha256", "expansion_version", "selection_rule",
    "minimum_bytes", "epochs", "values_count", "index_count",
    "maximum_index", "resident_bytes", "values_sha256", "index_sha256",
)


class InputError(RuntimeError):
    """A source cannot be bound to the approved paper input."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_absolute_file(value, label):
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} path is invalid")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise InputError(f"{label} path must be resolved and absolute")
    if not path.is_file():
        raise InputError(f"{label} does not exist: {path}")
    return path


def _require_sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InputError(f"{label} SHA-256 is invalid")
    return value


def _verify_file(path_value, digest_value, label):
    path = _require_absolute_file(path_value, label)
    expected = _require_sha256(digest_value, label)
    if _sha256_file(path) != expected:
        raise InputError(f"{label} SHA-256 differs")
    return path


def _require_allocated_bytes(row, workload):
    value = row["allocated_bytes"]
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < MINIMUM_ALLOCATED_BYTES[workload]
    ):
        raise InputError(
            f"{workload}.allocated_bytes is below the paper input size"
        )


def _read_json_file(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    return value


def _spatter_identity(provenance):
    identity = {
        name: provenance.get(name) for name in _SPATTER_IDENTITY_FIELDS
    }
    if "source_root" in provenance:
        identity["source_root"] = provenance["source_root"]
    return identity


def _scan_u64(path):
    count = 0
    maximum = 0
    with Path(path).open("rb") as stream:
        for payload in iter(lambda: stream.read(1 << 20), b""):
            if len(payload) % 8:
                raise InputError("Spatter index is not aligned u64")
            for (value,) in struct.iter_unpack("<Q", payload):
                count += 1
                maximum = max(maximum, value)
    if count == 0:
        raise InputError("Spatter index is empty")
    return count, maximum


def _scan_f32(path):
    count = 0
    with Path(path).open("rb") as stream:
        for payload in iter(lambda: stream.read(1 << 20), b""):
            if len(payload) % 4:
                raise InputError("Spatter values are not aligned f32")
            for (bits,) in struct.iter_unpack("<I", payload):
                if bits & 0x7F800000 != 0x3F000000:
                    raise InputError("Spatter value is not a finite normal input")
                count += 1
    if count == 0:
        raise InputError("Spatter values are empty")
    return count


def validate_spatter_record(workload, row):
    expected = {
        "amg_gather": ("gather", "Gather"),
        "lulesh_scatter": ("scatter", "Scatter"),
    }
    if workload not in expected or not isinstance(row, dict):
        raise InputError("Spatter workload record is invalid")
    if row.get("synthetic") is not False:
        raise InputError(f"{workload}.synthetic must be false")
    values_path = _verify_file(
        row.get("input"), row.get("input_sha256"), f"{workload} values"
    )
    index_path = _verify_file(
        row.get("index"), row.get("index_sha256"), f"{workload} index"
    )
    provenance_path = _verify_file(
        row.get("provenance"), row.get("provenance_sha256"),
        f"{workload} provenance",
    )
    validation_path = _verify_file(
        row.get("validation"), row.get("validation_sha256"),
        f"{workload} validation",
    )
    artifact_root = provenance_path.parent
    if (
        values_path != artifact_root / "values.f32le"
        or index_path != artifact_root / "index.u64le"
        or validation_path != artifact_root / "validation.json"
    ):
        raise InputError(f"{workload} artifact paths differ")
    provenance = _read_json_file(provenance_path, f"{workload} provenance")
    validation = _read_json_file(validation_path, f"{workload} validation")
    mode, kernel = expected[workload]
    artifact_id = hashlib.sha256(
        contract.canonical_json(_spatter_identity(provenance))
    ).hexdigest()
    if (
        provenance.get("schema") != 1
        or provenance.get("status") != "accepted"
        or provenance.get("source_kind")
        != "official_spatter_application_trace"
        or provenance.get("workload") != workload
        or provenance.get("mode") != mode
        or provenance.get("selected_kernel") != kernel
        or provenance.get("selection_rule")
        != f"all {kernel} records in source order"
        or provenance.get("expansion_version")
        != formal_spatter.EXPANSION_VERSION
        or provenance.get("artifact_id") != artifact_id
        or row.get("artifact_id") != artifact_id
        or artifact_root.name != artifact_id
    ):
        raise InputError(f"{workload} provenance identity differs")
    source_trace = _verify_file(
        provenance.get("source_trace"), provenance.get("source_trace_sha256"),
        f"{workload} source trace",
    )
    if provenance.get("generator_sha256") != _sha256_file(
        Path(formal_spatter.__file__).resolve()
    ):
        raise InputError(f"{workload} generator identity differs")
    source_root = provenance.get("source_root")
    if source_root is not None:
        root = Path(source_root)
        if (
            not root.is_absolute() or root.resolve() != root
            or not root.is_dir()
            or _git_output(root, "rev-parse", "HEAD")
            != provenance.get("source_commit")
        ):
            raise InputError(f"{workload} source commit differs")
        try:
            source_trace.relative_to(root)
        except ValueError as error:
            raise InputError(f"{workload} source trace is outside source root") from error
    values_count = _scan_f32(values_path)
    index_count, maximum_index = _scan_u64(index_path)
    integers = {
        "epochs": provenance.get("epochs"),
        "minimum_bytes": provenance.get("minimum_bytes"),
        "resident_bytes": provenance.get("resident_bytes"),
        "values_count": provenance.get("values_count"),
        "index_count": provenance.get("index_count"),
        "maximum_index": provenance.get("maximum_index"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in integers.values()
    ) or integers["epochs"] == 0 or integers["minimum_bytes"] == 0:
        raise InputError(f"{workload} provenance counts are invalid")
    if (
        values_count != integers["values_count"]
        or index_count != integers["index_count"]
        or maximum_index != integers["maximum_index"]
        or provenance.get("values_sha256") != row.get("input_sha256")
        or provenance.get("index_sha256") != row.get("index_sha256")
    ):
        raise InputError(f"{workload} artifact counts or hashes differ")
    if mode == "gather":
        if maximum_index + 1 != values_count:
            raise InputError("amg_gather values do not span the gathered index")
        allocated = 4 * values_count + 12 * index_count
        output_words = index_count
    else:
        if values_count != index_count:
            raise InputError("lulesh_scatter value/index counts differ")
        allocated = 12 * index_count + 4 * (maximum_index + 1)
        output_words = maximum_index + 1
    if (
        allocated != integers["resident_bytes"]
        or allocated != row.get("allocated_bytes")
        or allocated < MINIMUM_ALLOCATED_BYTES[workload]
        or integers["minimum_bytes"] < MINIMUM_ALLOCATED_BYTES[workload]
    ):
        raise InputError(f"{workload} allocated bytes differ")
    artifact_rows = provenance.get("artifacts", {})
    for name, path, digest, width, count in (
        ("values", values_path, row["input_sha256"], 4, values_count),
        ("index", index_path, row["index_sha256"], 8, index_count),
    ):
        artifact = artifact_rows.get(name, {})
        if (
            artifact.get("name") != path.name
            or artifact.get("sha256") != digest
            or artifact.get("size_bytes") != width * count
        ):
            raise InputError(f"{workload} provenance artifact differs")
    replay = provenance.get("independent_regeneration", {})
    if (
        replay.get("status") != "pass"
        or replay.get("values_sha256") != row["input_sha256"]
        or replay.get("index_sha256") != row["index_sha256"]
    ):
        raise InputError(f"{workload} independent regeneration differs")
    validation_link = provenance.get("validation", {})
    if (
        validation_link.get("name") != validation_path.name
        or validation_link.get("sha256") != row["validation_sha256"]
        or validation.get("schema") != 1
        or validation.get("status") != "accepted"
        or validation.get("workload") != workload
        or validation.get("mode") != mode
        or validation.get("values_sha256") != row["input_sha256"]
        or validation.get("index_sha256") != row["index_sha256"]
        or validation.get("output_words") != output_words
    ):
        raise InputError(f"{workload} reference validation differs")
    for name in (
        "destination_sha256", "reference_binary_sha256",
        "reference_source_sha256", "trace_abi_sha256", "command_sha256",
        "stdout_sha256",
    ):
        _require_sha256(validation.get(name), f"{workload} validation {name}")
    binary = _verify_file(
        validation.get("reference_binary"),
        validation.get("reference_binary_sha256"),
        f"{workload} reference binary",
    )
    if (
        validation.get("reference_source_sha256")
        != _sha256_file(_SPATTER_SOURCE)
        or validation.get("trace_abi_sha256")
        != _sha256_file(_SPATTER_TRACE_ABI)
    ):
        raise InputError(f"{workload} reference source identity differs")
    return {
        "provenance": provenance,
        "validation": validation,
        "values_path": values_path,
        "index_path": index_path,
        "binary_path": binary,
        "resident_bytes": allocated,
    }


def _require_equal_hashes(value, names, label):
    hashes = []
    for name in names:
        digest = _require_sha256(value.get(name), f"mcf validation {name}")
        hashes.append(digest)
    if len(set(hashes)) != 1:
        raise InputError(f"mcf {label} hashes differ")
    return hashes[0]


def _canonical_value_sha256(value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _source_set_sha256(paths):
    rows = [
        {"name": path.name, "sha256": _sha256_file(path)}
        for path in map(Path, paths)
    ]
    return _canonical_value_sha256(
        sorted(rows, key=lambda row: row["name"])
    )


def _require_current_replayer_identity(identity):
    if "cpp_kernel_sha256" not in identity:
        return
    current = {
        "wire_abi_sha256": _sha256_file(
            _MCF_SOURCE_ROOT / "mcfreg2_format.h"
        ),
        "generator_sha256": _sha256_file(
            _REPO_ROOT / "scripts/generate_mcfreg2_state.py"
        ),
        "python_reader_sha256": _sha256_file(
            _REPO_ROOT / "scripts/mcfreg2.py"
        ),
        "cpp_kernel_sha256": _sha256_file(
            _MCF_SOURCE_ROOT / "mcfreg2_kernels.cc"
        ),
        "cpp_reader_sha256": _source_set_sha256((
            _MCF_SOURCE_ROOT / "mcfreg2.hh",
            _MCF_SOURCE_ROOT / "mcfreg2.cc",
            _MCF_SOURCE_ROOT / "mcfreg2_state.hh",
            _MCF_SOURCE_ROOT / "mcfreg2_state.cc",
        )),
    }
    for name, digest in current.items():
        if identity.get(name) != digest:
            raise InputError(f"mcf semantic replay identity {name} differs")


def run_strict_mcfreg2_replay(package_path, output_root):
    """Compile and run the identity-local strict semantic MCFREG2 replay."""
    compiler = shutil.which("g++")
    if compiler is None:
        raise InputError("mcf semantic replay compiler is unavailable")
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    binary = output_root.parent / "mcfreg2-strict-replay"
    command = [
        compiler,
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(_MCF_SOURCE_ROOT),
        str(_MCF_SOURCE_ROOT / "mcf_regions.cc"),
        str(_MCF_SOURCE_ROOT / "mcfreg2.cc"),
        str(_MCF_SOURCE_ROOT / "mcfreg2_state.cc"),
        str(_MCF_SOURCE_ROOT / "mcfreg2_kernels.cc"),
        "-o",
        str(binary),
        "-lz",
    ]
    compiled = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if compiled.returncode != 0:
        raise InputError(
            "mcf semantic replay compilation failed: " + compiled.stdout
        )
    replayed = subprocess.run(
        [
            str(binary),
            "--input",
            str(package_path),
            "--output-root",
            str(output_root),
            "--hash-only",
        ],
        cwd=_REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if replayed.returncode != 0:
        raise InputError(
            "mcf semantic replay failed: " + replayed.stdout.strip()
        )
    replay_path = output_root / "mcfreg2-replay.json"
    replay_record = _read_json_file(replay_path, "mcf semantic replay")
    expected = {
        "boundary_mismatches",
        "operations",
        "price_out_calls",
        "pricing_calls",
        "status",
        "trace_sha256",
    }
    if set(replay_record) != expected:
        raise InputError("mcf semantic replay fields differ")
    counters = (
        replay_record["boundary_mismatches"],
        replay_record["pricing_calls"],
        replay_record["price_out_calls"],
        replay_record["operations"],
    )
    if (
        not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in counters
        )
        or replay_record["status"] != "verified"
        or replay_record["boundary_mismatches"] != 0
        or replay_record["pricing_calls"] < 0
        or replay_record["price_out_calls"] < 0
        or replay_record["operations"] <= 0
        or _SHA256.fullmatch(str(replay_record["trace_sha256"])) is None
    ):
        raise InputError("mcf semantic replay is not verified")
    return replay_record


def validate_mcf_record(row):
    """Validate one generated MCFREG2 candidate without other workloads."""
    if not isinstance(row, dict):
        raise InputError("mcf record must be an object")
    missing = REQUIRED["mcf"] - set(row)
    if missing:
        raise InputError(f"mcf.{sorted(missing)[0]} is required")
    if row.get("format") != "MCFREG2":
        raise InputError("mcf format must be MCFREG2")
    if row.get("synthetic") is not False:
        raise InputError("mcf.synthetic must be false")
    if _GIT_COMMIT.fullmatch(str(row.get("source_commit", ""))) is None:
        raise InputError("mcf.source_commit is invalid")
    _require_sha256(row.get("source_tree_sha256"), "mcf source tree")
    _require_allocated_bytes(row, "mcf")

    package_path = _verify_file(
        row["input"], row["input_sha256"], "mcf package/input"
    )
    source_path = _verify_file(
        row["source"], row["source_sha256"], "mcf source"
    )
    validation_path = _verify_file(
        row["validation"], row["validation_sha256"], "mcf validation"
    )
    try:
        package = mcfreg2.read_package(
            package_path,
            lazy_section_names=tuple(
                name for name in mcfreg2.REQUIRED_SECTIONS
                if name not in {"PROVENANCE", "FINAL"}
            ),
        )
    except mcfreg2.FormatError as error:
        raise InputError(f"mcf MCFREG2 package is invalid: {error}") from error
    schemas = {
        entry.section_type: entry.schema for entry in package.directory
    }
    for name in ("EVENTS", "CALL_INDEX", "BOUNDARIES"):
        if schemas.get(mcfreg2.SECTION_TYPES[name]) != 3:
            raise InputError(
                f"mcf semantic replay requires schema-3 {name}"
            )
    validation = _read_json_file(validation_path, "mcf validation")
    if validation.get("schema") != 2:
        raise InputError("mcf validation schema must be 2")
    if validation.get("status") != "accepted":
        raise InputError("mcf validation status is not accepted")
    if validation.get("boundary_mismatches") != 0:
        raise InputError("mcf validation has boundary mismatches")
    if validation.get("primary_replay_equal") is not True:
        raise InputError("mcf validation primary/replay packages differ")
    if validation.get("native_outputs_equal") is not True:
        raise InputError("mcf validation native outputs differ")

    package_sha256 = _require_equal_hashes(
        validation,
        ("package_sha256", "primary_package_sha256", "replay_package_sha256"),
        "primary/replay package",
    )
    if package_sha256 != row["input_sha256"]:
        raise InputError("mcf package/input SHA-256 differs from validation")
    if (
        validation.get("source_sha256") is not None
        and validation.get("source_sha256") != row["source_sha256"]
    ):
        raise InputError("mcf source SHA-256 differs from validation")
    if source_path.stat().st_size == 0:
        raise InputError("mcf source is empty")

    identity = validation.get("identity")
    legacy_identity_names = {
        "source_commit",
        "source_tree_sha256",
        "input_sha256",
        "common_patch_sha256",
        "capture_patch_sha256",
        "compiler_sha256",
    }
    strict_identity_names = legacy_identity_names | {
        "capture_runtime_sha256",
        "wire_abi_sha256",
        "compiler_version",
        "compiler_target",
        "authority_command_sha256",
        "capture_command_sha256",
        "authority_binary_sha256",
        "capture_binary_sha256",
        "generator_sha256",
        "python_reader_sha256",
        "cpp_reader_sha256",
        "cpp_kernel_sha256",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) not in (legacy_identity_names, strict_identity_names)
    ):
        raise InputError("mcf validation identity fields differ")
    if identity["source_commit"] != row["source_commit"]:
        raise InputError("mcf source commit differs from validation")
    if identity["source_tree_sha256"] != row["source_tree_sha256"]:
        raise InputError("mcf source tree SHA-256 differs from validation")
    for name in set(identity) - {
        "source_commit", "compiler_version", "compiler_target"
    }:
        _require_sha256(identity.get(name), f"mcf identity {name}")
    for name in ("compiler_version", "compiler_target"):
        if name in identity and (
            not isinstance(identity[name], str) or not identity[name]
        ):
            raise InputError(f"mcf identity {name} is invalid")
    _require_current_replayer_identity(identity)

    try:
        provenance = json.loads(package.section("PROVENANCE"))
        final = json.loads(package.section("FINAL"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"mcf package JSON section is invalid: {error}") from error
    if not isinstance(provenance, dict) or provenance.get("schema") != 1:
        raise InputError("mcf package provenance is invalid")
    for name, expected in identity.items():
        if provenance.get(name) != expected:
            raise InputError(f"mcf package provenance {name} differs")
    if not isinstance(final, dict) or final.get("schema") != 1:
        raise InputError("mcf package FINAL section is invalid")

    final_state = _require_equal_hashes(
        validation,
        (
            "authority_final_state_sha256",
            "capture_primary_final_state_sha256",
            "capture_replay_final_state_sha256",
        ),
        "authority/capture final-state",
    )
    output = _require_equal_hashes(
        validation,
        (
            "authority_mcf_output_sha256",
            "capture_primary_mcf_output_sha256",
            "capture_replay_mcf_output_sha256",
        ),
        "authority/capture mcf.out",
    )
    if final.get("final_state_sha256") != final_state:
        raise InputError("mcf package final state differs from validation")
    if final.get("mcf_output_sha256") != output:
        raise InputError("mcf package mcf.out differs from validation")
    peak = validation.get("peak_allocated_bytes")
    if (
        not isinstance(peak, int)
        or isinstance(peak, bool)
        or peak != row["allocated_bytes"]
        or final.get("peak_allocated_bytes") != peak
    ):
        raise InputError("mcf observed allocated bytes differ")
    with tempfile.TemporaryDirectory(prefix="mcfreg2-semantic-replay-") as tmp:
        snapshot = Path(tmp) / "mcf.reg2"
        shutil.copyfile(package_path, snapshot)
        if _sha256_file(snapshot) != row["input_sha256"]:
            raise InputError("mcf semantic replay snapshot SHA-256 differs")
        replay_record = run_strict_mcfreg2_replay(
            snapshot, Path(tmp) / "output"
        )
    if _sha256_file(package_path) != row["input_sha256"]:
        raise InputError("mcf package changed during semantic replay")
    _require_current_replayer_identity(identity)
    if (
        replay_record["pricing_calls"] != package.header.pricing_calls
        or replay_record["price_out_calls"] != package.header.price_out_calls
    ):
        raise InputError("mcf semantic replay call counts differ")
    return {
        "package": package,
        "validation": validation,
        "package_path": package_path,
        "validation_path": validation_path,
        "replay": replay_record,
    }


def validate_paper_record(value):
    if not isinstance(value, dict) or set(value) != set(WORKLOADS):
        raise InputError("paper input record workload set differs")
    for workload in WORKLOADS:
        row = value[workload]
        if not isinstance(row, dict):
            raise InputError(f"{workload} record must be an object")
        missing = REQUIRED[workload] - set(row)
        if missing:
            raise InputError(f"{workload}.{sorted(missing)[0]} is required")
        if row.get("synthetic") is True:
            raise InputError(
                f"{workload} synthetic input is not paper evidence"
            )
        _require_allocated_bytes(row, workload)
    if value["mcf"]["synthetic"] is not False:
        raise InputError("mcf.synthetic must be false")
    if value["pr_spmv"]["scale"] != 20:
        raise InputError("pr_spmv.scale must be 20")
    for workload in ("npb_cg", "npb_mg"):
        row = value[workload]
        if not isinstance(row["class"], str) or not row["class"]:
            raise InputError(f"{workload}.class is invalid")
        if _GIT_COMMIT.fullmatch(row["source_commit"]) is None:
            raise InputError(f"{workload}.source_commit is invalid")
    return value


def _git_output(root, *arguments):
    root = Path(root).resolve()
    try:
        return subprocess.check_output(
            (
                "git", "-c", f"safe.directory={root}",
                "-C", str(root), *arguments,
            ),
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise InputError(
            f"cannot inspect NPB source {root}: {error.output.strip()}"
        ) from error


def _validate_npb_source(workload, row):
    root = Path(row["source_root"])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise InputError(
            f"{workload}.source_root must be an existing resolved directory"
        )
    actual_commit = _git_output(root, "rev-parse", "HEAD")
    if actual_commit != row["source_commit"]:
        raise InputError(f"{workload} source commit differs")
    if _git_output(root, "status", "--porcelain"):
        raise InputError(f"{workload} source tree is dirty")
    parameter = _verify_file(
        row["parameter_file"],
        row["parameter_sha256"],
        f"{workload} parameter",
    )
    try:
        parameter.relative_to(root)
    except ValueError as error:
        raise InputError(
            f"{workload} parameter file is outside source root"
        ) from error


def validate_bound_inputs(value):
    validate_paper_record(value)
    _verify_file(
        value["pr_spmv"]["input"],
        value["pr_spmv"]["input_sha256"],
        "pr_spmv input",
    )
    validate_mcf_record(value["mcf"])
    for workload in ("amg_gather", "lulesh_scatter"):
        validate_spatter_record(workload, value[workload])
    for workload in ("npb_cg", "npb_mg"):
        _validate_npb_source(workload, value[workload])
    return {
        workload: json.loads(json.dumps(value[workload], sort_keys=True))
        for workload in WORKLOADS
    }


def _load_paper_record(path):
    path = Path(path)
    if not path.is_file():
        raise InputError(f"paper input record does not exist: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InputError(f"paper input record is invalid: {error}") from error


def freeze_inputs(options):
    paper_path = Path(options.paper_input_record)
    workloads = validate_bound_inputs(_load_paper_record(paper_path))
    graph_paths = tuple(getattr(options, "graph_manifests", ()))
    if len(graph_paths) != 4:
        raise InputError("exactly four graph manifests are required")
    try:
        graphs = profiles.load_scaling_graphs(graph_paths)
    except profiles.ProfileError as error:
        raise InputError(str(error)) from error
    return {
        "schema": 1,
        "status": "accepted",
        "paper_input_record": str(paper_path.resolve()),
        "paper_input_record_sha256": _sha256_file(paper_path),
        "workloads": workloads,
        "graphs": [
            {
                "scale": row.scale,
                "path": row.graph,
                "sha256": row.graph_sha256,
                "num_nodes": row.num_nodes,
                "directed_edges": row.directed_edges,
                "manifest": str(Path(path).resolve()),
                "manifest_sha256": _sha256_file(path),
            }
            for path, row in zip(graph_paths, graphs)
        ],
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-input-record", type=Path, required=True)
    parser.add_argument(
        "--graph-manifest", dest="graph_manifests", type=Path, action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        result = freeze_inputs(options)
    except InputError as error:
        contract.atomic_write_json(
            options.output.with_name("failed-input.json"),
            {"schema": 1, "status": "failed_input", "reason": str(error)},
        )
        return 2
    contract.atomic_write_json(options.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
