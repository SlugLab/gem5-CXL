#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Freeze real inputs for the CIRA, AMU, and M2NDP comparison."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import gapbs_pr_experiment_profiles as profiles
except ImportError:
    import cross_system_contract as contract
    import gapbs_pr_experiment_profiles as profiles


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
        "synthetic",
    },
    "amg_gather": {
        "input",
        "input_sha256",
        "index",
        "index_sha256",
        "allocated_bytes",
    },
    "lulesh_scatter": {
        "input",
        "input_sha256",
        "index",
        "index_sha256",
        "allocated_bytes",
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
    "npb_cg": 12_000_000_000,
    "npb_mg": 12_000_000_000,
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


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
    try:
        return subprocess.check_output(
            ("git", "-C", str(root), *arguments),
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
    _verify_file(
        value["mcf"]["input"], value["mcf"]["input_sha256"], "mcf input"
    )
    _verify_file(
        value["mcf"]["source"],
        value["mcf"]["source_sha256"],
        "mcf source",
    )
    for workload in ("amg_gather", "lulesh_scatter"):
        _verify_file(
            value[workload]["input"],
            value[workload]["input_sha256"],
            f"{workload} input",
        )
        _verify_file(
            value[workload]["index"],
            value[workload]["index_sha256"],
            f"{workload} index",
        )
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
