#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Audit candidate paper inputs without creating accepted evidence."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import freeze_cross_system_inputs as freeze
except ImportError:
    import cross_system_contract as contract
    import freeze_cross_system_inputs as freeze


FILE_FIELDS = {
    "pr_spmv": ("input",),
    "mcf": ("input", "source"),
    "amg_gather": ("input", "index"),
    "lulesh_scatter": ("input", "index"),
    "npb_cg": ("parameter_file",),
    "npb_mg": ("parameter_file",),
}
SOURCE_ROOT_WORKLOADS = ("npb_cg", "npb_mg")


def template_record():
    return {
        "pr_spmv": {
            "input": "REQUIRED_ABSOLUTE_GRAPH_PATH",
            "input_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 240_000_000,
            "scale": 20,
        },
        "mcf": {
            "input": "REQUIRED_ABSOLUTE_INPUT_PATH",
            "input_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 345_000_000,
            "source": "REQUIRED_ABSOLUTE_SOURCE_PATH",
            "source_sha256": "REQUIRED_SHA256",
            "synthetic": False,
        },
        "amg_gather": {
            "input": "REQUIRED_ABSOLUTE_DATA_PATH",
            "input_sha256": "REQUIRED_SHA256",
            "index": "REQUIRED_ABSOLUTE_INDEX_PATH",
            "index_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 1 << 30,
        },
        "lulesh_scatter": {
            "input": "REQUIRED_ABSOLUTE_DATA_PATH",
            "input_sha256": "REQUIRED_SHA256",
            "index": "REQUIRED_ABSOLUTE_INDEX_PATH",
            "index_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 1 << 30,
        },
        "npb_cg": {
            "source_root": "REQUIRED_ABSOLUTE_CLEAN_GIT_ROOT",
            "source_commit": "REQUIRED_EXACT_COMMIT",
            "parameter_file": "REQUIRED_ABSOLUTE_PARAMETER_PATH",
            "parameter_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 12_800_000_000,
            "class": "REQUIRED_CLASS",
        },
        "npb_mg": {
            "source_root": "REQUIRED_ABSOLUTE_CLEAN_GIT_ROOT",
            "source_commit": "REQUIRED_EXACT_COMMIT",
            "parameter_file": "REQUIRED_ABSOLUTE_PARAMETER_PATH",
            "parameter_sha256": "REQUIRED_SHA256",
            "allocated_bytes": 12_800_000_000,
            "class": "REQUIRED_CLASS",
        },
    }


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observe_file(value, workload, field, reasons):
    label = f"{workload}.{field}"
    if not isinstance(value, str) or not value:
        return {}
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        reasons.append(f"{label} path must be resolved and absolute")
        return {}
    if not path.is_file():
        reasons.append(f"{label} does not exist: {path}")
        return {}
    return {
        field: str(path),
        f"{field}_sha256": _sha256_file(path),
        f"{field}_size_bytes": path.stat().st_size,
    }


def _git_output(root, *arguments):
    root = Path(root).resolve()
    return subprocess.check_output(
        (
            "git", "-c", f"safe.directory={root}",
            "-C", str(root), *arguments,
        ),
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _observe_source_root(value, workload, reasons):
    label = f"{workload}.source_root"
    if not isinstance(value, str) or not value:
        return {}
    root = Path(value)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        reasons.append(f"{label} must be an existing resolved directory")
        return {}
    try:
        commit = _git_output(root, "rev-parse", "HEAD")
        dirty = _git_output(root, "status", "--porcelain")
    except subprocess.CalledProcessError as error:
        detail = error.output.strip()
        reasons.append(f"cannot inspect {label}: {detail}")
        return {"source_root": str(root)}
    return {
        "source_root": str(root),
        "source_commit": commit,
        "source_dirty": bool(dirty),
    }


def audit_record(value, *, initial_reasons=()):
    reasons = list(initial_reasons)
    if not isinstance(value, dict):
        reasons.append("candidate record must be an object")
        value = {}
    missing_workloads = sorted(set(freeze.WORKLOADS) - set(value))
    extra_workloads = sorted(set(value) - set(freeze.WORKLOADS))
    if extra_workloads:
        reasons.append(
            "candidate record has unknown workloads: "
            + ", ".join(extra_workloads)
        )
    workloads = {}
    for workload in freeze.WORKLOADS:
        row = value.get(workload)
        if not isinstance(row, dict):
            workloads[workload] = {
                "candidate": row,
                "missing_fields": sorted(freeze.REQUIRED[workload]),
                "observed": {},
            }
            continue
        missing_fields = sorted(freeze.REQUIRED[workload] - set(row))
        observed = {}
        for field in FILE_FIELDS[workload]:
            observed.update(
                _observe_file(row.get(field), workload, field, reasons)
            )
        if workload in SOURCE_ROOT_WORKLOADS:
            observed.update(
                _observe_source_root(
                    row.get("source_root"), workload, reasons
                )
            )
        workloads[workload] = {
            "candidate": json.loads(json.dumps(row, sort_keys=True)),
            "missing_fields": missing_fields,
            "observed": observed,
        }
    status = "incomplete"
    exact_shape = not missing_workloads and not extra_workloads
    if exact_shape:
        try:
            freeze.validate_bound_inputs(value)
        except freeze.InputError as error:
            reasons.append(str(error))
        else:
            status = "ready_for_freeze"
    elif missing_workloads:
        reasons.append(
            "candidate record is missing workloads: "
            + ", ".join(missing_workloads)
        )
    return {
        "schema": 1,
        "status": status,
        "missing_workloads": missing_workloads,
        "extra_workloads": extra_workloads,
        "workloads": workloads,
        "reasons": sorted(set(reasons)),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-record", type=Path, required=True)
    parser.add_argument("--discovery-output", type=Path, required=True)
    parser.add_argument("--template-output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    reasons = []
    try:
        candidate = json.loads(
            options.candidate_record.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        candidate = {}
        reasons.append(
            f"candidate record is unavailable: "
            f"{options.candidate_record}: {error}"
        )
    discovery = audit_record(candidate, initial_reasons=reasons)
    contract.atomic_write_json(options.template_output, template_record())
    contract.atomic_write_json(options.discovery_output, discovery)
    return 0 if discovery["status"] == "ready_for_freeze" else 2


if __name__ == "__main__":
    raise SystemExit(main())
