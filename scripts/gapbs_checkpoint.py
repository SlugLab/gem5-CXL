#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Content-addressed provenance for reusable GAPBS SE checkpoints."""

import hashlib
import json
import os
from pathlib import Path


MANIFEST_NAME = "manifest.json"
CHECKPOINT_PAYLOAD = "m5.cpt"


class CheckpointError(RuntimeError):
    """Raised when checkpoint evidence is missing, malformed, or stale."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_identity(
    *,
    binary,
    graph,
    graph_scale,
    arguments,
    cores,
    memory_size,
    gem5,
    config,
    kind,
    model_parameters,
):
    binary = Path(binary).resolve()
    graph = Path(graph).resolve()
    gem5 = Path(gem5).resolve()
    config = Path(config).resolve()
    return {
        "schema": 1,
        "kind": str(kind),
        "binary_path": str(binary),
        "binary_sha256": sha256_file(binary),
        "graph_path": str(graph),
        "graph_sha256": sha256_file(graph),
        "graph_scale": int(graph_scale),
        "arguments": [str(argument) for argument in arguments],
        "cores": int(cores),
        "memory_size": str(memory_size),
        "gem5_path": str(gem5),
        "gem5_sha256": sha256_file(gem5),
        "config_path": str(config),
        "config_sha256": sha256_file(config),
        "model_parameters": {
            str(key): str(value)
            for key, value in sorted(model_parameters.items())
        },
    }


def identity_key(identity):
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_payload(identity):
    return {
        "checkpoint_id": identity_key(identity),
        "identity": identity,
    }


def write_manifest(root, identity):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / MANIFEST_NAME
    temporary = root / f".{MANIFEST_NAME}.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(
            _manifest_payload(identity),
            stream,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, manifest)
    return manifest


def load_manifest(root):
    path = Path(root) / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CheckpointError(f"missing checkpoint manifest: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(
            f"invalid checkpoint manifest: {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise CheckpointError(f"invalid checkpoint manifest object: {path}")
    return payload


def validate_reuse(root, identity):
    root = Path(root)
    payload_path = root / CHECKPOINT_PAYLOAD
    if not payload_path.is_file():
        raise CheckpointError(
            f"missing checkpoint payload: {payload_path}"
        )
    manifest = load_manifest(root)
    expected_key = identity_key(identity)
    if manifest.get("identity") != identity:
        raise CheckpointError(f"checkpoint identity mismatch: {root}")
    if manifest.get("checkpoint_id") != expected_key:
        raise CheckpointError(f"checkpoint id mismatch: {root}")
    return True
