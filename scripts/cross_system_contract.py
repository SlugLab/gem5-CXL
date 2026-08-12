#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Immutable evidence identities and terminal-state transitions."""

import dataclasses
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


TERMINAL = frozenset({"complete", "failed", "inconclusive", "failed_input"})
TRANSITIONS = {
    "planned": frozenset({"functional_pass", "failed", "failed_input"}),
    "functional_pass": frozenset({"timing_in_progress", "failed"}),
    "timing_in_progress": frozenset(
        {"complete", "failed", "inconclusive"}
    ),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ContractError(RuntimeError):
    """An evidence record violates the immutable experiment contract."""


def _require_sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContractError(f"{label} SHA-256 is invalid")
    return value


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class ExperimentIdentity:
    code_sha256: str
    input_manifest_sha256: str
    calibration_manifest_sha256: str
    trace_sha256: str
    config_sha256: str

    def __post_init__(self):
        for field in dataclasses.fields(self):
            _require_sha256(getattr(self, field.name), field.name.removesuffix("_sha256").replace("_", " "))

    def digest(self):
        return hashlib.sha256(canonical_json(dataclasses.asdict(self))).hexdigest()


def transition(state, target, *, reason=""):
    source = state.get("status")
    if target not in TRANSITIONS.get(source, frozenset()):
        raise ContractError(f"illegal transition {source} -> {target}")
    if not isinstance(reason, str):
        raise ContractError("transition reason must be a string")
    return {**state, "status": target, "reason": reason}


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON record {path}: {error}") from error


def bind_root(root, identity):
    if not isinstance(identity, ExperimentIdentity):
        raise ContractError("evidence identity has the wrong type")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "identity.json"
    expected = {
        "schema": 1,
        "digest": identity.digest(),
        "identity": dataclasses.asdict(identity),
    }
    if path.exists() and load_json(path) != expected:
        raise ContractError(
            "fresh evidence root required after experiment identity change"
        )
    if not path.exists():
        atomic_write_json(path, expected)
    return path


def verify_named_hashes(outputs):
    if not isinstance(outputs, dict) or not outputs:
        return False
    for record in outputs.values():
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            return False
        try:
            expected = _require_sha256(record["sha256"], "output")
            path = Path(record["path"])
            if not path.is_absolute() or not path.is_file():
                return False
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                return False
        except (ContractError, OSError, TypeError):
            return False
    return True


def select_resume_checkpoint(records, identity_digest):
    _require_sha256(identity_digest, "identity")
    valid = []
    rejected = []
    for record in records:
        try:
            sequence = record["sequence"]
            is_valid = (
                isinstance(sequence, int)
                and not isinstance(sequence, bool)
                and sequence >= 0
                and record["identity_sha256"] == identity_digest
                and record["boundary"] in {"phase", "window"}
                and verify_named_hashes(record["outputs"])
            )
        except (KeyError, TypeError):
            is_valid = False
        (valid if is_valid else rejected).append(record)
    selected = max(valid, key=lambda row: row["sequence"]) if valid else None
    return selected, tuple(rejected)
