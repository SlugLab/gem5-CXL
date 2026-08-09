#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Fail-closed artifact helpers for the M2NDP PageRank experiment."""

import csv
import dataclasses
import hashlib
import json
import mmap
import os
import re
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


@dataclasses.dataclass(frozen=True)
class GraphBundle:
    root: Path
    meta: GraphMeta
    in_offsets: "BinaryArray"
    in_neighbors: "BinaryArray"
    out_degree: "BinaryArray"


class BinaryArray(Sequence):
    """Read-only fixed-width array backed by a component file."""

    def __init__(self, path: Path, fmt: str, count: int):
        self.path = Path(path)
        self.fmt = fmt
        self.count = count
        self.width = struct.calcsize(fmt)
        self._stream = None
        self._mapping = None
        if count:
            self._stream = self.path.open("rb")
            self._mapping = mmap.mmap(
                self._stream.fileno(), 0, access=mmap.ACCESS_READ
            )

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(
                self.count
            )))
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        return struct.unpack_from(
            self.fmt, self._mapping, index * self.width
        )[0]

    def __iter__(self):
        if not self.count:
            return iter(())
        return (value[0] for value in struct.iter_unpack(
            self.fmt, self._mapping
        ))

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    def close(self):
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __del__(self):
        self.close()


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
    if not smoke_test and meta.graph_sha256 != EXPECTED_G20_SHA256:
        raise EvidenceError(
            "graph SHA-256 does not match the fixed g20 publication graph"
        )


def validate_profile_graph(meta: GraphMeta, profile) -> None:
    if meta.graph_sha256 != profile.graph_sha256:
        raise EvidenceError("graph SHA-256 does not match profile")
    if meta.num_nodes != profile.num_nodes:
        raise EvidenceError("graph node count does not match profile")
    if meta.num_directed_edges < 0:
        raise EvidenceError("graph directed edge count is negative")


def load_graph_meta(path: Path) -> GraphMeta:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"invalid graph metadata: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceError("graph metadata must be a JSON object")
    if value.get("schema") != 1:
        raise EvidenceError("graph metadata schema must be 1")
    try:
        meta = GraphMeta(
            graph_sha256=value["graph_sha256"],
            num_nodes=value["num_nodes"],
            num_directed_edges=value["num_directed_edges"],
            directed=value["directed"],
        )
    except KeyError as error:
        raise EvidenceError(
            f"graph metadata missing {error.args[0]}"
        ) from error
    if (
        not isinstance(meta.graph_sha256, str)
        or len(meta.graph_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in meta.graph_sha256
        )
    ):
        raise EvidenceError("graph metadata SHA-256 is invalid")
    if not isinstance(meta.num_nodes, int) or meta.num_nodes < 0:
        raise EvidenceError("graph metadata node count is invalid")
    if (
        not isinstance(meta.num_directed_edges, int)
        or meta.num_directed_edges < 0
    ):
        raise EvidenceError("graph metadata edge count is invalid")
    if not isinstance(meta.directed, bool):
        raise EvidenceError("graph metadata directed flag is invalid")
    return meta


_EXPORT_MARKER = re.compile(
    r"^M2NDP_GRAPH_EXPORT nodes=(\d+) "
    r"directed_edges=(\d+) directed=([01])$"
)


def finalize_graph_meta(
    root: Path, graph: Path, exporter_stdout: str
) -> GraphMeta:
    markers = []
    for line in exporter_stdout.splitlines():
        match = _EXPORT_MARKER.fullmatch(line.strip())
        if match:
            markers.append(match)
    if len(markers) != 1:
        raise EvidenceError(
            "exporter output must contain exactly one M2NDP_GRAPH_EXPORT marker"
        )
    marker = markers[0]
    meta = GraphMeta(
        graph_sha256=sha256_file(Path(graph)),
        num_nodes=int(marker.group(1)),
        num_directed_edges=int(marker.group(2)),
        directed=marker.group(3) == "1",
    )
    atomic_write_json(
        Path(root) / "graph.meta.json",
        {"schema": 1, **dataclasses.asdict(meta)},
    )
    return meta


def _read_component(
    path: Path, *, fmt: str, count: int, label: str
) -> BinaryArray:
    path = Path(path)
    width = struct.calcsize(fmt)
    expected = count * width
    try:
        actual = path.stat().st_size
    except OSError as error:
        raise EvidenceError(f"cannot stat {label}: {error}") from error
    if actual != expected:
        raise EvidenceError(
            f"{label} byte size {actual} does not match expected {expected}"
        )
    return BinaryArray(path, fmt, count)


def load_graph_bundle(root: Path) -> GraphBundle:
    root = Path(root)
    meta = load_graph_meta(root / "graph.meta.json")
    offsets = _read_component(
        root / "in_offsets.u64",
        fmt="<Q",
        count=meta.num_nodes + 1,
        label="in_offsets.u64",
    )
    neighbors = _read_component(
        root / "in_neighbors.i32",
        fmt="<i",
        count=meta.num_directed_edges,
        label="in_neighbors.i32",
    )
    degrees = _read_component(
        root / "out_degree.u32",
        fmt="<I",
        count=meta.num_nodes,
        label="out_degree.u32",
    )
    if not offsets or offsets[0] != 0:
        raise EvidenceError("first CSR offset must be zero")
    for index, (left, right) in enumerate(zip(offsets, offsets[1:])):
        if right < left:
            raise EvidenceError(
                f"non-monotonic CSR offsets at vertex {index}"
            )
    if offsets[-1] != meta.num_directed_edges:
        raise EvidenceError(
            "terminal CSR offset does not equal directed edge count"
        )
    for index, neighbor in enumerate(neighbors):
        if neighbor < 0 or neighbor >= meta.num_nodes:
            raise EvidenceError(
                f"neighbor {index} outside vertex range: {neighbor}"
            )
    return GraphBundle(root, meta, offsets, neighbors, degrees)


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
