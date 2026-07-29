#!/usr/bin/env python3
"""Fail-closed live process checkpoint orchestration for long simulations."""

import hashlib
import json
import os
import platform
from pathlib import Path


class CheckpointError(RuntimeError):
    """Raised when live checkpoint evidence is incomplete or inconsistent."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_manifest(
    *,
    name,
    unit,
    root_pid,
    process_tree,
    inputs,
    image_dir,
    progress,
    host,
):
    if not process_tree:
        raise CheckpointError("process tree is empty")
    image_dir = Path(image_dir).resolve()
    images = {
        path.name: _file_record(path)
        for path in sorted(image_dir.iterdir())
        if path.is_file()
    }
    if not images:
        raise CheckpointError("checkpoint image directory is empty")
    return {
        "schema": 1,
        "name": name,
        "unit": unit,
        "root_pid": int(root_pid),
        "process_tree": process_tree,
        "inputs": {
            str(Path(path).resolve()): _file_record(path)
            for path in inputs
        },
        "image_dir": str(image_dir),
        "images": images,
        "progress": progress,
        "host": host,
    }


def _validate_record(record, *, kind):
    path = Path(record["path"])
    if not path.is_file():
        raise CheckpointError(f"missing {kind}: {path}")
    if path.stat().st_size != record["size"]:
        raise CheckpointError(f"{kind} size mismatch: {path}")
    if sha256_file(path) != record["sha256"]:
        raise CheckpointError(f"{kind} hash mismatch: {path}")


def validate_manifest(manifest, *, manifest_path, require_same_kernel):
    if manifest.get("schema") != 1:
        raise CheckpointError("unsupported checkpoint manifest schema")
    if not manifest.get("process_tree"):
        raise CheckpointError("process tree is empty")
    if require_same_kernel:
        captured = manifest.get("host", {}).get("kernel_release")
        current = platform.release()
        if captured != current:
            raise CheckpointError(
                f"kernel release mismatch: captured={captured} current={current}"
            )
    for record in manifest.get("inputs", {}).values():
        _validate_record(record, kind="checkpoint input")
    for record in manifest.get("images", {}).values():
        _validate_record(record, kind="checkpoint image")
    return manifest


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(f"cannot load JSON {path}: {error}") from error


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def validate_transaction(
    transaction, *, require_ready, require_same_kernel
):
    if transaction.get("schema") != 1:
        raise CheckpointError("unsupported checkpoint transaction schema")
    if require_ready and transaction.get("state") != "ready_for_reboot":
        raise CheckpointError("transaction is not ready for reboot")
    workloads = transaction.get("workloads", {})
    for name in ("amu", "m2ndp"):
        if name not in workloads:
            raise CheckpointError(f"transaction is missing workload {name}")
        manifest_path = Path(workloads[name])
        manifest = load_json(manifest_path)
        if manifest.get("name") != name:
            raise CheckpointError(
                f"transaction workload name mismatch for {name}"
            )
        validate_manifest(
            manifest,
            manifest_path=manifest_path,
            require_same_kernel=require_same_kernel,
        )
    return transaction

