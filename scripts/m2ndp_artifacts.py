#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Fail-closed artifact helpers for the M2NDP PageRank experiment."""

import csv
import dataclasses
import hashlib
import json
import os
import struct
from collections.abc import Sequence
from pathlib import Path


EXPECTED_G20_SHA256 = (
    "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
)
EXPECTED_M2NDP_COMMIT = (
    "fe418e8c30d7c3821f7c91293c74c5c34939a063"
)
REFERENCE_MAGIC = b"M2PRREF1"
REFERENCE_SCHEMA = 1
REFERENCE_KEYS = frozenset(
    {
        "schema",
        "graph_sha256",
        "num_nodes",
        "iterations",
        "measured_trial",
        "binary_sha256",
        "source_sha256",
    }
)


class EvidenceError(RuntimeError):
    """Evidence is absent, malformed, or inconsistent."""


@dataclasses.dataclass(frozen=True)
class GraphMeta:
    graph_sha256: str
    num_nodes: int
    num_directed_edges: int
    directed: bool


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_replace(path: Path, writer) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: dict) -> None:
    encoded = (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _atomic_replace(Path(path), lambda stream: stream.write(encoded))


def atomic_write_csv(
    path: Path, fieldnames: tuple[str, ...], rows: list[dict]
) -> None:
    def write(stream):
        text = os.fdopen(os.dup(stream.fileno()), "w", newline="", closefd=True)
        try:
            writer = csv.DictWriter(text, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            text.flush()
        finally:
            text.close()

    _atomic_replace(Path(path), write)


def validate_publication_graph(meta: GraphMeta, smoke_test: bool) -> None:
    if meta.num_nodes <= 0:
        raise EvidenceError("graph must contain at least one node")
    if meta.num_directed_edges < 0:
        raise EvidenceError("graph directed edge count is negative")
    if not meta.directed:
        raise EvidenceError("PageRank publication graph must be directed")
    if not smoke_test and meta.graph_sha256 != EXPECTED_G20_SHA256:
        raise EvidenceError(
            "graph SHA-256 does not match the fixed g20 publication graph"
        )


def _validate_reference_header(header: dict) -> None:
    missing = sorted(REFERENCE_KEYS.difference(header))
    if missing:
        raise EvidenceError(
            "reference header missing keys: " + ", ".join(missing)
        )
    if header["schema"] != REFERENCE_SCHEMA:
        raise EvidenceError(
            f"unsupported reference schema {header['schema']!r}"
        )
    if not isinstance(header["num_nodes"], int) or header["num_nodes"] < 0:
        raise EvidenceError("reference num_nodes must be a nonnegative integer")
    for key in ("graph_sha256", "binary_sha256", "source_sha256"):
        value = header[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise EvidenceError(f"reference {key} is not a SHA-256")


def write_reference(
    path: Path, header: dict, words: Sequence[int]
) -> None:
    header = dict(header)
    _validate_reference_header(header)
    if header["num_nodes"] != len(words):
        raise EvidenceError(
            "reference word count does not match header num_nodes"
        )
    encoded_header = json.dumps(
        header, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")

    def write(stream):
        stream.write(REFERENCE_MAGIC)
        stream.write(struct.pack("<I", len(encoded_header)))
        stream.write(encoded_header)
        for index, word in enumerate(words):
            if not isinstance(word, int) or not 0 <= word <= 0xFFFFFFFF:
                raise EvidenceError(
                    f"reference word {index} is outside uint32 range"
                )
            stream.write(struct.pack("<I", word))

    _atomic_replace(Path(path), write)


def read_reference(path: Path) -> tuple[dict, list[int]]:
    data = Path(path).read_bytes()
    prefix_size = len(REFERENCE_MAGIC) + 4
    if len(data) < prefix_size:
        raise EvidenceError("reference is truncated before its header")
    if data[: len(REFERENCE_MAGIC)] != REFERENCE_MAGIC:
        raise EvidenceError("reference magic does not match M2PRREF1")
    header_size = struct.unpack_from("<I", data, len(REFERENCE_MAGIC))[0]
    header_end = prefix_size + header_size
    if header_end > len(data):
        raise EvidenceError("reference JSON header is truncated")
    try:
        header = json.loads(data[prefix_size:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"invalid reference JSON: {error}") from error
    if not isinstance(header, dict):
        raise EvidenceError("reference JSON must be an object")
    _validate_reference_header(header)
    word_bytes = data[header_end:]
    if len(word_bytes) % 4:
        raise EvidenceError("reference has a trailing partial word")
    words = [word[0] for word in struct.iter_unpack("<I", word_bytes)]
    if len(words) != header["num_nodes"]:
        raise EvidenceError(
            "reference word count does not match header num_nodes"
        )
    return header, words
