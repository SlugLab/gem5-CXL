#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Freeze the four real graphs for the formal PR scaling experiment."""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import gapbs_pr_experiment_profiles as profiles
except ImportError:
    import cross_system_contract as contract
    import gapbs_pr_experiment_profiles as profiles


SCOPE = "pr_scaling"
PROFILE = profiles.SCALING_PROFILE_NAME
SCALES = profiles.SCALING_SCALES
TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "status",
        "scope",
        "profile",
        "graphs",
        "graph_set_sha256",
    }
)
GRAPH_KEYS = frozenset(
    {
        "scale",
        "path",
        "sha256",
        "manifest",
        "manifest_sha256",
        "num_nodes",
        "directed_edges",
        "generator",
        "generator_sha256",
        "generator_command",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ScalingInputError(RuntimeError):
    """A graph set cannot be used as formal PR-scaling evidence."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ScalingInputError(f"{label} SHA-256 is invalid")
    return value


def _require_file(value, label):
    if not isinstance(value, str) or not value:
        raise ScalingInputError(f"{label} path is invalid")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ScalingInputError(f"{label} path must be resolved and absolute")
    if not path.is_file():
        raise ScalingInputError(f"{label} is missing: {path}")
    return path


def graph_set_sha256(graphs):
    if not isinstance(graphs, list):
        raise ScalingInputError("graphs must be a list")
    identities = [
        {
            "scale": row["scale"],
            "sha256": row["sha256"],
            "manifest_sha256": row["manifest_sha256"],
        }
        for row in graphs
    ]
    return hashlib.sha256(contract.canonical_json(identities)).hexdigest()


def _graph_record(manifest_path, row):
    manifest_path = Path(manifest_path).resolve()
    return {
        "scale": row.scale,
        "path": row.graph,
        "sha256": row.graph_sha256,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "num_nodes": row.num_nodes,
        "directed_edges": row.directed_edges,
        "generator": row.generator,
        "generator_sha256": row.generator_sha256,
        "generator_command": list(row.generator_command),
    }


def freeze_inputs(manifest_paths):
    paths = tuple(Path(path).resolve() for path in manifest_paths)
    if len(paths) != 4:
        raise ScalingInputError("exactly four graph manifests are required")
    try:
        frozen = profiles.load_scaling_graphs(paths)
    except profiles.ProfileError as error:
        raise ScalingInputError(str(error)) from error
    graphs = [
        _graph_record(path, row) for path, row in zip(paths, frozen)
    ]
    value = {
        "schema": 1,
        "status": "accepted",
        "scope": SCOPE,
        "profile": PROFILE,
        "graphs": graphs,
        "graph_set_sha256": graph_set_sha256(graphs),
    }
    return validate_manifest(value)


def _validate_graph_row(row, scale):
    if not isinstance(row, dict) or set(row) != GRAPH_KEYS:
        raise ScalingInputError(f"g{scale} graph record keys differ")
    if row.get("scale") != scale:
        raise ScalingInputError("frozen graphs must be ordered g4,g12,g14,g20")
    if row.get("num_nodes") != 1 << scale:
        raise ScalingInputError(f"g{scale} node count differs from scale")
    if (
        not isinstance(row.get("directed_edges"), int)
        or isinstance(row.get("directed_edges"), bool)
        or row["directed_edges"] <= 0
    ):
        raise ScalingInputError(f"g{scale} directed-edge count is invalid")
    graph = _require_file(row.get("path"), f"g{scale} graph")
    manifest = _require_file(row.get("manifest"), f"g{scale} manifest")
    generator = _require_file(row.get("generator"), f"g{scale} generator")
    graph_digest = _require_sha256(row.get("sha256"), f"g{scale} graph")
    manifest_digest = _require_sha256(
        row.get("manifest_sha256"), f"g{scale} manifest"
    )
    generator_digest = _require_sha256(
        row.get("generator_sha256"), f"g{scale} generator"
    )
    if _sha256_file(graph) != graph_digest:
        raise ScalingInputError(f"g{scale} graph SHA-256 changed")
    if _sha256_file(manifest) != manifest_digest:
        raise ScalingInputError(f"g{scale} manifest SHA-256 changed")
    if _sha256_file(generator) != generator_digest:
        raise ScalingInputError(f"g{scale} generator SHA-256 changed")
    expected_command = [
        str(generator), "-g", str(scale), "-b", str(graph)
    ]
    if row.get("generator_command") != expected_command:
        raise ScalingInputError(f"g{scale} generator command differs")
    return manifest


def validate_manifest(value):
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_KEYS:
        raise ScalingInputError("PR-scaling input manifest keys differ")
    if value.get("schema") != 1 or isinstance(value.get("schema"), bool):
        raise ScalingInputError("PR-scaling input schema must be integer 1")
    if value.get("status") != "accepted":
        raise ScalingInputError("PR-scaling input status is not accepted")
    if value.get("scope") != SCOPE:
        raise ScalingInputError("PR-scaling input scope differs")
    if value.get("profile") != PROFILE:
        raise ScalingInputError("PR-scaling input profile differs")
    graphs = value.get("graphs")
    if not isinstance(graphs, list) or len(graphs) != len(SCALES):
        raise ScalingInputError("exactly four frozen graphs are required")
    if tuple(
        row.get("scale") for row in graphs if isinstance(row, dict)
    ) != SCALES:
        raise ScalingInputError("frozen graphs must be ordered g4,g12,g14,g20")
    manifest_paths = [
        _validate_graph_row(row, scale)
        for row, scale in zip(graphs, SCALES)
    ]
    expected_set = graph_set_sha256(graphs)
    _require_sha256(value.get("graph_set_sha256"), "graph-set")
    if value["graph_set_sha256"] != expected_set:
        raise ScalingInputError("graph-set SHA-256 differs")
    try:
        frozen = profiles.load_scaling_graphs(manifest_paths)
    except profiles.ProfileError as error:
        raise ScalingInputError(str(error)) from error
    for record, row in zip(graphs, frozen):
        expected = {
            "scale": row.scale,
            "path": row.graph,
            "sha256": row.graph_sha256,
            "num_nodes": row.num_nodes,
            "directed_edges": row.directed_edges,
            "generator": row.generator,
            "generator_sha256": row.generator_sha256,
            "generator_command": list(row.generator_command),
        }
        for field, expected_value in expected.items():
            if record[field] != expected_value:
                raise ScalingInputError(
                    f"g{row.scale} {field} differs from frozen manifest"
                )
    return json.loads(json.dumps(value, sort_keys=True))


def load_and_validate(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScalingInputError(f"invalid PR-scaling input manifest: {error}") from error
    return validate_manifest(value)


def _write_immutable(path, value):
    path = Path(path).resolve()
    payload = contract.canonical_json(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ScalingInputError(
                "immutable PR-scaling input already exists with different contents"
            )
        return path
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444
        )
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ScalingInputError(
                "immutable PR-scaling input already exists with different contents"
            )
        return path
    try:
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--graph-manifest",
        dest="graph_manifests",
        type=Path,
        action="append",
        default=[],
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    failure_path = options.output.with_name("failed-input.json")
    try:
        value = freeze_inputs(options.graph_manifests)
        output = _write_immutable(options.output, value)
    except (ScalingInputError, OSError) as error:
        contract.atomic_write_json(
            failure_path,
            {"schema": 1, "status": "failed_input", "reason": str(error)},
        )
        print(f"PR_SCALING_INPUTS_FAILED error={error}", file=sys.stderr)
        return 2
    failure_path.unlink(missing_ok=True)
    print(f"PR_SCALING_INPUTS_ACCEPTED manifest={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
