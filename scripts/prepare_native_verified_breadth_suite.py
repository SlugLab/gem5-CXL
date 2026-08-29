#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Prepare executable six-workload breadth evidence without eager expansion."""

import hashlib
import json
import argparse
import gc
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from scripts import build_matched_breadth_workloads as builder
    from scripts import cross_system_contract as contract
except ImportError:
    import build_matched_breadth_workloads as builder
    import cross_system_contract as contract


WORKLOADS = (
    "pr_spmv",
    "mcf",
    "amg_gather",
    "lulesh_scatter",
    "npb_cg",
    "npb_mg",
)
FUNCTIONAL_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp-funcsim")
TIMING_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")


class PreparationError(RuntimeError):
    """A prepared-suite artifact violates the executable formal contract."""


_SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise PreparationError(f"prepared file is missing: {path}")
    return {"path": str(path), "sha256": sha256_file(path)}


def _load_committed_checkpoint(checkpoint_path, state, workload):
    """Load only a checkpoint atomically committed into the resume state."""

    checkpoint_path = Path(checkpoint_path).resolve()
    state_row = (state.get("workloads") or {}).get(workload)
    if (
        not isinstance(state_row, dict)
        or state_row.get("status") != "verified"
        or state_row.get("checkpoint") != str(checkpoint_path)
    ):
        raise PreparationError(
            f"formal NPB {workload} checkpoint is not committed by state"
        )
    if state_row.get("checkpoint_sha256") != sha256_file(checkpoint_path):
        raise PreparationError(
            f"formal NPB {workload} checkpoint hash differs"
        )
    return contract.load_json(checkpoint_path)


def _validate_npb_checkpoint_record(record, source, outdir, workload, row):
    """Revalidate a native checkpoint against every retained artifact."""

    required_values = {
        "class": row["class"],
        "status": "verified",
        "correctness_policy": "native-verified",
        "official_verification": "pass",
        "raw_verification": "pass",
        "runtime_threads": 4,
        "measured_allocated_bytes": row["allocated_bytes"],
        "parameter_sha256": row["parameter_sha256"],
        "config_sha256": row["parameter_sha256"],
        "expansion_policy": "lazy-descriptor-native-verified",
    }
    if not isinstance(record, dict):
        raise PreparationError(
            f"formal NPB {workload} checkpoint status differs"
        )
    for name, expected in required_values.items():
        if record.get(name) != expected:
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} differs"
            )
    for name in ("capture_sha256", "binary_sha256", "source_sha256"):
        if (
            not isinstance(record.get(name), str)
            or _SHA256.fullmatch(record[name]) is None
        ):
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} is invalid"
            )
    if (
        not isinstance(record.get("capture_bytes"), int)
        or isinstance(record["capture_bytes"], bool)
        or record["capture_bytes"] <= 0
    ):
        raise PreparationError(
            f"formal NPB {workload} checkpoint capture size is invalid"
        )
    for name in (
        "boundary_map", "lazy_boundary_map", "boundary_crosswalk"
    ):
        value = record.get(name)
        if not isinstance(value, (dict, list)) or not value:
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} is invalid"
            )
        if record.get(f"{name}_sha256") != builder._json_sha256(value):
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} hash differs"
            )

    source = Path(source).resolve()
    outdir = Path(outdir).resolve()
    binary = source / "bin" / f"{workload}.{row['class']}.x"
    descriptor_root = outdir / f"{workload}-formal"
    descriptor = descriptor_root / "trace.v2.json"
    parameter = source / workload.upper() / "npbparams.h"
    transformed_source = source / workload.upper() / f"{workload}.f"
    file_bindings = {
        "binary": (binary, "binary_file", "binary_sha256"),
        "descriptor": (
            descriptor, "descriptor_file", "descriptor_sha256"
        ),
        "built parameter": (
            parameter, "built_parameter_file", "built_parameter_sha256"
        ),
    }
    for label, (path, path_field, hash_field) in file_bindings.items():
        if record.get(path_field) != str(path):
            raise PreparationError(
                f"formal NPB {workload} checkpoint {label} path differs"
            )
        if record.get(hash_field) != sha256_file(path):
            raise PreparationError(
                f"formal NPB {workload} checkpoint {label} hash differs"
            )
    if record["source_sha256"] != sha256_file(transformed_source):
        raise PreparationError(
            f"formal NPB {workload} checkpoint source hash differs"
        )
    code_bindings = {
        "patch_sha256": builder.NPB_PATCHES[workload],
        "hook_header_sha256": builder.NPB_TRACE_HOOKS,
        "hook_implementation_sha256": builder.NPB_TRACE_IMPLEMENTATION,
        "expander_sha256": builder.NPB_EXPANDER_SOURCE,
        "lazy_runtime_sha256": builder.LAZY_TRACE_SOURCE,
        "canonical_trace_source_sha256": builder.CANONICAL_TRACE_SOURCE,
        "trace_abi_sha256": builder.TRACE_ABI,
    }
    for name, path in code_bindings.items():
        if record.get(name) != sha256_file(path):
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} differs"
            )

    bundle = builder.lazy.read_bundle(descriptor_root)
    expected_meta = {
        "source_sha256": record["source_sha256"],
        "binary_sha256": record["binary_sha256"],
        "config_sha256": row["parameter_sha256"],
        "workload": f"npb_{workload}",
    }
    for name, expected in expected_meta.items():
        if bundle.meta.get(name) != expected:
            raise PreparationError(
                f"formal NPB {workload} checkpoint descriptor {name} differs"
            )
    bundle_identity = builder._npb_bundle_identity(bundle)
    for name, expected in bundle_identity.items():
        if record.get(name) != expected:
            raise PreparationError(
                f"formal NPB {workload} checkpoint {name} differs"
            )
    if record.get("lazy_boundary_map") != bundle.meta.get(
        "boundary_commitments"
    ):
        raise PreparationError(
            f"formal NPB {workload} checkpoint lazy boundary map differs"
        )
    if record.get("primitive_records") != bundle.dynamic_work.get(
        "primitive_records"
    ):
        raise PreparationError(
            f"formal NPB {workload} checkpoint primitive work differs"
        )
    del bundle
    gc.collect()
    return record


def native_verified_npb_evidence(capture, workload, bundle):
    """Bind native verification to a lazy descriptor without expanding it."""

    try:
        commitments = bundle.meta["boundary_commitments"]
        primitive_records = bundle.dynamic_work["primitive_records"]
    except (AttributeError, KeyError, TypeError) as error:
        raise PreparationError("formal NPB lazy descriptor is incomplete") from error
    if (
        not isinstance(primitive_records, int)
        or isinstance(primitive_records, bool)
        or primitive_records <= 0
    ):
        raise PreparationError("formal NPB primitive record count is invalid")
    if not isinstance(commitments, dict) or not commitments:
        raise PreparationError("formal NPB boundary commitments are missing")
    crosswalk = builder._validate_npb_boundary_commitments(
        capture, workload, commitments
    )
    return {
        "expansion_policy": "lazy-descriptor-native-verified",
        "primitive_records": primitive_records,
        "lazy_boundary_map": dict(sorted(commitments.items())),
        "lazy_boundary_map_sha256": builder._json_sha256(commitments),
        "boundary_crosswalk": crosswalk,
        "boundary_crosswalk_sha256": builder._json_sha256(crosswalk),
    }


def _native_run_identity(binary, workload):
    return {
        "argv": [str(Path(binary).resolve())],
        "cwd": str(
            (Path(binary).resolve().parent.parent / workload.upper()).resolve()
        ),
        "environment": {
            "OMP_NUM_THREADS": "4",
            "OMP_DYNAMIC": "FALSE",
            "OMP_PROC_BIND": "TRUE",
        },
    }


def _npb_workload_record(
    *, source, outdir, workload, row, binary, build_command,
    capture_path, allocated, run_identity, recovery,
):
    source = Path(source).resolve()
    outdir = Path(outdir).resolve()
    capture_path = Path(capture_path).resolve()
    if allocated != row["allocated_bytes"]:
        raise PreparationError(
            f"formal NPB {workload} allocation probe {allocated} "
            f"!= inputs.json {row['allocated_bytes']}"
        )
    capture = builder._parse_npb_capture(capture_path)
    boundary_map, boundary_map_sha256 = builder._capture_boundary_map(capture)
    source_file = source / workload.upper() / f"{workload}.f"
    source_sha256 = sha256_file(source_file)
    binary_sha256 = sha256_file(binary)
    descriptor_root = outdir / f"{workload}-formal"
    descriptor = descriptor_root / "trace.v2.json"
    if descriptor.is_file():
        bundle = builder.lazy.read_bundle(descriptor_root)
    elif descriptor_root.exists():
        raise PreparationError(
            f"partial formal NPB descriptor root differs: {descriptor_root}"
        )
    else:
        bundle = builder._write_npb_lazy_bundle(
            capture,
            workload,
            descriptor_root,
            source_sha256=source_sha256,
            binary_sha256=binary_sha256,
            config_sha256=row["parameter_sha256"],
        )
    expected_meta = {
        "source_sha256": source_sha256,
        "binary_sha256": binary_sha256,
        "config_sha256": row["parameter_sha256"],
        "workload": f"npb_{workload}",
    }
    for name, expected in expected_meta.items():
        if bundle.meta.get(name) != expected:
            raise PreparationError(
                f"formal NPB {workload} descriptor {name} differs"
            )
    descriptor_evidence = native_verified_npb_evidence(
        capture, workload, bundle
    )
    built_parameter = builder._validate_built_npb_parameter(
        source, workload, row["parameter_sha256"]
    )
    result = {
        "class": row["class"],
        "status": "verified",
        "correctness_policy": "native-verified",
        "official_verification": "pass",
        "raw_verification": "pass",
        "runtime_threads": 4,
        "recovery": recovery,
        "capture_sha256": capture["capture_sha256"],
        "capture_bytes": capture_path.stat().st_size,
        "boundary_map": boundary_map,
        "boundary_map_sha256": boundary_map_sha256,
        "descriptor_file": str(descriptor),
        "descriptor_sha256": sha256_file(descriptor),
        **builder._npb_bundle_identity(bundle),
        **descriptor_evidence,
        "measured_allocated_bytes": allocated,
        "parameter_sha256": row["parameter_sha256"],
        "built_parameter_file": str(built_parameter),
        "built_parameter_sha256": sha256_file(built_parameter),
        "config_sha256": row["parameter_sha256"],
        "binary_sha256": binary_sha256,
        "binary_file": str(Path(binary).resolve()),
        "source_sha256": source_sha256,
        "patch_sha256": sha256_file(builder.NPB_PATCHES[workload]),
        "hook_header_sha256": sha256_file(builder.NPB_TRACE_HOOKS),
        "hook_implementation_sha256": sha256_file(
            builder.NPB_TRACE_IMPLEMENTATION
        ),
        "expander_sha256": sha256_file(builder.NPB_EXPANDER_SOURCE),
        "lazy_runtime_sha256": sha256_file(builder.LAZY_TRACE_SOURCE),
        "canonical_trace_source_sha256": sha256_file(
            builder.CANONICAL_TRACE_SOURCE
        ),
        "trace_abi_sha256": sha256_file(builder.TRACE_ABI),
        "build_command": [str(item) for item in build_command],
        "formal_run": run_identity,
    }
    del bundle
    del capture
    gc.collect()
    return result


def _recover_existing_npb(source, outdir, workload, row):
    binary = source / "bin" / f"{workload}.{row['class']}.x"
    capture = outdir / f"{workload}-formal.capture.bin"
    allocation = outdir / f"{workload}-formal.allocation.u64"
    descriptor = outdir / f"{workload}-formal/trace.v2.json"
    required = (binary, capture, allocation, descriptor)
    if not all(path.is_file() for path in required):
        return None
    return _npb_workload_record(
        source=source,
        outdir=outdir,
        workload=workload,
        row=row,
        binary=binary,
        build_command=["recovered-existing-build"],
        capture_path=capture,
        allocated=builder._npb_allocation_probe(allocation, workload),
        run_identity=_native_run_identity(binary, workload),
        recovery={
            "mode": "post-gate-artifact-recovery",
            "basis": (
                "descriptor creation follows the official verifier, "
                "four-thread gate, capture authentication, and allocation gate"
            ),
        },
    )


def prepare_native_verified_npb(inputs_path, outdir, *, resume=False):
    """Build/recover formal CG/MG with bounded lazy-descriptor evidence."""

    rows, inputs_sha256 = builder.load_frozen_npb_inputs(inputs_path)
    roots = {Path(row["source_root"]).resolve() for row in rows.values()}
    commits = {row["source_commit"] for row in rows.values()}
    if len(roots) != 1 or len(commits) != 1:
        raise PreparationError("formal NPB CG/MG source identity differs")
    source_root = roots.pop()
    source_commit = commits.pop()
    builder.validate_npb_formal_source_identity(
        source_root,
        expected_commit=source_commit,
        parameter_files={
            workload: row["parameter_file"] for workload, row in rows.items()
        },
        expected_parameter_hashes={
            workload: row["parameter_sha256"] for workload, row in rows.items()
        },
        allocated_bytes={
            workload: row["allocated_bytes"] for workload, row in rows.items()
        },
    )
    outdir = Path(outdir).resolve()
    if outdir.exists() and not resume:
        raise PreparationError(f"fresh formal NPB root required: {outdir}")
    outdir.mkdir(parents=True, exist_ok=resume)
    state_path = outdir / "native-verified-state.json"
    expected_identity = {
        "inputs_sha256": inputs_sha256,
        "source_commit": source_commit,
        "correctness_policy": "native-verified",
        "preparer_sha256": sha256_file(__file__),
    }
    if state_path.is_file():
        state = contract.load_json(state_path)
        if state.get("identity") != expected_identity:
            raise PreparationError("formal NPB resume identity differs")
    else:
        state = {
            "schema": 1,
            "status": "in_progress",
            "identity": expected_identity,
            "workloads": {
                workload: {"status": "pending"} for workload in rows
            },
        }
        contract.atomic_write_json(state_path, state)
    source = outdir / "source"
    if not source.exists():
        shutil.copytree(source_root, source)
        builder._run_checked(
            ["make", "clean"], cwd=source, label="formal NPB clean"
        )
        builder._install_frozen_npb_parameters(source_root, source, rows)
        for workload in rows:
            builder._transform_npb_source(source_root, workload, source)
    hook_object = outdir / "npb_trace_hooks.o"
    if not hook_object.is_file():
        builder._compile_npb_hook(hook_object)
    result = {
        "schema": 1,
        "mode": "formal",
        "status": "verified",
        "publishable": True,
        "paper_evidence": True,
        "evidence_scope": "formal_paper_input_native_verified",
        "correctness_policy": "native-verified",
        "threads": 4,
        "source_root": str(source_root),
        "source_commit": source_commit,
        "inputs_sha256": inputs_sha256,
        "preparer_sha256": sha256_file(__file__),
        "semantic_identity": builder._npb_semantic_identity(),
        "workloads": {},
    }
    measured = {}
    for workload, row in rows.items():
        checkpoint_path = outdir / f"{workload}.native-verified.json"
        checkpoint_exists = checkpoint_path.is_file()
        if checkpoint_exists:
            record = _load_committed_checkpoint(
                checkpoint_path, state, workload
            )
        else:
            record = _recover_existing_npb(source, outdir, workload, row)
            if record is None:
                binary, command = builder._build_npb(
                    source, workload, hook_object, row["class"]
                )
                with tempfile.TemporaryDirectory(
                    prefix=f"matched-npb-{workload}-", dir="/tmp"
                ) as temporary:
                    capture_path, _output, allocated, run_identity = (
                        builder._run_npb_binary(
                            binary, workload, Path(temporary), "formal"
                        )
                    )
                    record = _npb_workload_record(
                        source=source,
                        outdir=outdir,
                        workload=workload,
                        row=row,
                        binary=binary,
                        build_command=command,
                        capture_path=capture_path,
                        allocated=allocated,
                        run_identity=run_identity,
                        recovery={"mode": "live-native-run"},
                    )
        _validate_npb_checkpoint_record(
            record, source, outdir, workload, row
        )
        if not checkpoint_exists:
            contract.atomic_write_json(checkpoint_path, record)
        result["workloads"][workload] = record
        measured[workload] = record["measured_allocated_bytes"]
        state["workloads"][workload] = {
            "status": "verified",
            "checkpoint": str(checkpoint_path),
            "checkpoint_sha256": sha256_file(checkpoint_path),
        }
        contract.atomic_write_json(state_path, state)
    builder.validate_npb_formal_source(
        source_root,
        expected_commit=source_commit,
        parameter_files={
            workload: row["parameter_file"] for workload, row in rows.items()
        },
        expected_parameter_hashes={
            workload: row["parameter_sha256"] for workload, row in rows.items()
        },
        allocated_bytes={
            workload: row["allocated_bytes"] for workload, row in rows.items()
        },
        measured_allocated_bytes=measured,
    )
    builder._validate_npb_semantic_identity(result["semantic_identity"])
    contract.atomic_write_json(outdir / "manifest.json", result)
    state["status"] = "complete"
    state["manifest"] = {
        "path": str(outdir / "manifest.json"),
        "sha256": sha256_file(outdir / "manifest.json"),
    }
    contract.atomic_write_json(state_path, state)
    return result


def action_layout(workload, phases, *, action_driver):
    """Return complete action commands reusable below every latency root."""

    action_driver = str(Path(action_driver).resolve())
    common = [
        sys.executable,
        action_driver,
        "--prepared", "{{prepared_manifest}}",
        "--workload", workload,
        "--evidence", "{{evidence_path}}",
    ]
    reference_evidence = f"evidence/reference/{workload}.json"
    functional = {}
    for system in FUNCTIONAL_SYSTEMS:
        functional[system] = {
            "command": [
                *common, "--stage", "functional", "--system", system,
            ],
            "evidence": f"evidence/functional/{workload}/{system}.json",
        }
    windows = {}
    for phase in phases:
        windows[phase] = {}
        for system in TIMING_SYSTEMS:
            windows[phase][system] = {
                "command": [
                    *common,
                    "--stage", "window",
                    "--system", system,
                    "--phase", phase,
                    "--window-index", "{{window_index}}",
                    "--level", "{{level}}",
                    "--warmup-start", "{{warmup_start}}",
                    "--measure-start", "{{measure_start}}",
                    "--measure-stop", "{{measure_stop}}",
                    "--cxl-link-delay", "{{cxl_link_delay}}",
                ],
                "evidence": (
                    "evidence/timing/{{cxl_link_delay}}/"
                    f"{workload}/{phase}/{system}/{{{{window_index}}}}.json"
                ),
            }
    return {
        "reference": {
            "command": [*common, "--stage", "reference"],
            "evidence": reference_evidence,
        },
        "functional": functional,
        "window": windows,
    }


def prepared_manifest(*, root, workloads, code_files, config_files):
    """Create the exact manifest interface consumed by the breadth runner."""

    if set(workloads or {}) != set(WORKLOADS):
        raise PreparationError("prepared workload set differs")
    if not contract.verify_named_hashes(code_files):
        raise PreparationError("prepared code bindings are invalid")
    if not contract.verify_named_hashes(config_files):
        raise PreparationError("prepared configuration bindings are invalid")
    for name in WORKLOADS:
        row = workloads[name]
        if not isinstance(row, dict) or not row.get("phases"):
            raise PreparationError(f"prepared {name} phases are missing")
        if not isinstance(row.get("actions"), dict):
            raise PreparationError(f"prepared {name} actions are missing")
    return {
        "schema": 1,
        "status": "verified",
        "mode": "formal-native-verified",
        "root": str(Path(root).resolve()),
        "threads": 4,
        "all_memory_cxl": True,
        "functional_systems": list(FUNCTIONAL_SYSTEMS),
        "timing_systems": list(TIMING_SYSTEMS),
        "workloads": {name: workloads[name] for name in WORKLOADS},
        "code_files": dict(sorted(code_files.items())),
        "config_files": dict(sorted(config_files.items())),
    }


def write_manifest(path, value):
    contract.atomic_write_json(Path(path), value)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--npb-outdir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        result = prepare_native_verified_npb(
            options.inputs, options.npb_outdir, resume=options.resume
        )
    except (
        PreparationError, builder.BuildError, contract.ContractError, OSError
    ) as error:
        print(f"NATIVE_VERIFIED_PREPARATION_FAILED error={error}", file=sys.stderr)
        return 1
    print(
        "NATIVE_VERIFIED_NPB_PASS "
        f"manifest={Path(options.npb_outdir).resolve() / 'manifest.json'} "
        f"workloads={len(result['workloads'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
