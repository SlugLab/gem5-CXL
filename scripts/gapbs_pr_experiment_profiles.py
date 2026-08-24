#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Immutable contracts for formal GAPBS PageRank experiments."""

import dataclasses
import json
import struct
from pathlib import Path

try:
    from scripts import cxl_latency_spectrum
    from scripts import m2ndp_artifacts
except ImportError:
    import cxl_latency_spectrum
    import m2ndp_artifacts


G4_SHA256 = (
    "f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d"
)
SCALING_SCALES = (4, 12, 14, 20)
SCALING_PROFILE_NAME = "pr-scaling-4thread-1us"
FORMAL_PROFILE_NAME = "pr-offload-4thread-1us"
FORMAL_SCALES = (12, 14, 20)
SCALING_GRAPH_HASHES = {
    4: G4_SHA256,
    20: m2ndp_artifacts.EXPECTED_G20_SHA256,
}
LATENCY_TICKS = cxl_latency_spectrum.TICKS


class ProfileError(RuntimeError):
    """An experiment input falls outside its immutable profile."""


@dataclasses.dataclass(frozen=True)
class ExperimentProfile:
    name: str
    graph_scale: int
    graph_sha256: str
    num_nodes: int
    cores: int
    threads: int
    latencies: tuple[str, ...]
    logical_partitions: int = 4
    trials: int = 2
    measured_trial: int = 1
    page_rank_iterations: int = 20


@dataclasses.dataclass(frozen=True)
class ScalingExperimentProfile:
    name: str = SCALING_PROFILE_NAME
    scales: tuple[int, ...] = SCALING_SCALES
    cores: int = 4
    threads: int = 4
    logical_partitions: int = 4
    latencies: tuple[str, ...] = ("1us",)
    trials: int = 2
    measured_trial: int = 1
    page_rank_iterations: int = 20


@dataclasses.dataclass(frozen=True)
class FrozenGraphManifest:
    schema: int
    scale: int
    graph: str
    graph_sha256: str
    generator: str
    generator_sha256: str
    generator_command: tuple[str, ...]
    num_nodes: int
    directed_edges: int


FROZEN_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "scale",
        "graph",
        "graph_sha256",
        "generator",
        "generator_sha256",
        "generator_command",
        "num_nodes",
        "directed_edges",
    }
)
FROZEN_PROFILE_CONTRACTS = {
    "g12-4thread-qualification": (12, ("1us",)),
    "g14-4thread-sweep": (14, ("200ns", "500ns", "1us", "2us")),
}


LEGACY_DIAGNOSTIC_PROFILES = {
    "g20-2thread-1us": ExperimentProfile(
        name="g20-2thread-1us",
        graph_scale=20,
        graph_sha256=m2ndp_artifacts.EXPECTED_G20_SHA256,
        num_nodes=1 << 20,
        cores=2,
        threads=2,
        latencies=("1us",),
        logical_partitions=2,
    ),
}

PROFILES = {
    **LEGACY_DIAGNOSTIC_PROFILES,
    "g4-4thread-sweep": ExperimentProfile(
        name="g4-4thread-sweep",
        graph_scale=4,
        graph_sha256=G4_SHA256,
        num_nodes=1 << 4,
        cores=4,
        threads=4,
        latencies=("200ns", "500ns", "1us", "2us"),
    ),
}
FORMAL_PROFILE_NAMES = frozenset(
    {FORMAL_PROFILE_NAME, SCALING_PROFILE_NAME}
    | set(FROZEN_PROFILE_CONTRACTS)
)


def get_scaling_profile() -> ScalingExperimentProfile:
    return ScalingExperimentProfile()


def get_formal_offload_profile() -> ScalingExperimentProfile:
    return ScalingExperimentProfile(
        name=FORMAL_PROFILE_NAME,
        scales=FORMAL_SCALES,
    )


def get_legacy_diagnostic_profile(name: str) -> ExperimentProfile:
    try:
        return LEGACY_DIAGNOSTIC_PROFILES[name]
    except KeyError as error:
        raise ProfileError(
            f"unknown legacy diagnostic profile: {name}"
        ) from error


def get_profile(name: str) -> ExperimentProfile:
    try:
        return PROFILES[name]
    except KeyError as error:
        raise ProfileError(f"unknown experiment profile: {name}") from error


def validate_graph(profile: ExperimentProfile, graph: Path) -> Path:
    graph = Path(graph)
    if m2ndp_artifacts.sha256_file(graph) != profile.graph_sha256:
        raise ProfileError("graph SHA-256 does not match experiment profile")
    return graph


def require_latency(profile: ExperimentProfile, latency: str) -> int:
    if latency not in profile.latencies:
        raise ProfileError(
            f"latency {latency} is outside profile {profile.name}"
        )
    return cxl_latency_spectrum.ticks(latency)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_sha256(value, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProfileError(f"{label} SHA-256 is invalid")
    return value


def inspect_serialized_graph(path: Path) -> tuple[int, int, bool]:
    """Return node count, directed-edge count, and directed flag for .sg."""
    path = Path(path)
    header_size = struct.calcsize("<?qq")
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(header_size)
    except OSError as error:
        raise ProfileError(f"cannot read serialized graph: {error}") from error
    if len(header) != header_size:
        raise ProfileError("serialized graph header is truncated")
    directed, directed_edges, num_nodes = struct.unpack("<?qq", header)
    if num_nodes <= 0:
        raise ProfileError("serialized graph node count must be positive")
    if directed_edges <= 0:
        raise ProfileError("serialized graph edge count must be positive")
    csr_size = (num_nodes + 1) * 8 + directed_edges * 4
    expected_size = header_size + csr_size * (2 if directed else 1)
    if size != expected_size:
        raise ProfileError(
            f"serialized graph size {size} does not match expected "
            f"{expected_size}"
        )
    return num_nodes, directed_edges, directed


def load_graph_manifest(path: Path) -> FrozenGraphManifest:
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"invalid frozen graph manifest: {error}") from error
    if not isinstance(value, dict):
        raise ProfileError("frozen graph manifest must be a JSON object")
    if set(value) != FROZEN_MANIFEST_KEYS:
        missing = sorted(FROZEN_MANIFEST_KEYS - set(value))
        extra = sorted(set(value) - FROZEN_MANIFEST_KEYS)
        raise ProfileError(
            f"frozen graph manifest keys mismatch: missing={missing} extra={extra}"
        )
    if value["schema"] != 1 or not _is_int(value["schema"]):
        raise ProfileError("frozen graph manifest schema must be integer 1")
    if value["scale"] not in SCALING_SCALES or not _is_int(value["scale"]):
        raise ProfileError("frozen graph scale must be 4, 12, 14, or 20")
    if not _is_int(value["num_nodes"]) or value["num_nodes"] <= 0:
        raise ProfileError("frozen graph node count is invalid")
    if (
        not _is_int(value["directed_edges"])
        or value["directed_edges"] <= 0
    ):
        raise ProfileError("frozen graph edge count is invalid")
    for field in ("graph", "generator"):
        if not isinstance(value[field], str) or not value[field]:
            raise ProfileError(f"frozen graph {field} path is invalid")
        candidate = Path(value[field])
        if not candidate.is_absolute() or candidate.resolve() != candidate:
            raise ProfileError(
                f"frozen graph {field} path must be resolved and absolute"
            )
    command = value["generator_command"]
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(argument, str) or not argument for argument in command)
    ):
        raise ProfileError("frozen graph generator command is invalid")
    expected_command = [
        value["generator"],
        "-g",
        str(value["scale"]),
        "-b",
        value["graph"],
    ]
    if command != expected_command:
        raise ProfileError("frozen graph generator command is not canonical")
    _validate_sha256(value["graph_sha256"], "graph")
    _validate_sha256(value["generator_sha256"], "generator")
    return FrozenGraphManifest(
        **{
            **value,
            "generator_command": tuple(value["generator_command"]),
        }
    )


def validate_frozen_graph(manifest: FrozenGraphManifest) -> None:
    graph = Path(manifest.graph)
    generator = Path(manifest.generator)
    if not graph.is_file():
        raise ProfileError(f"frozen graph does not exist: {graph}")
    if m2ndp_artifacts.sha256_file(graph) != manifest.graph_sha256:
        raise ProfileError("graph SHA-256 does not match frozen manifest")
    if not generator.is_file():
        raise ProfileError(f"frozen graph generator does not exist: {generator}")
    if m2ndp_artifacts.sha256_file(generator) != manifest.generator_sha256:
        raise ProfileError("generator SHA-256 does not match frozen manifest")
    num_nodes, directed_edges, _ = inspect_serialized_graph(graph)
    if num_nodes != manifest.num_nodes:
        raise ProfileError("serialized graph node count does not match manifest")
    if directed_edges != manifest.directed_edges:
        raise ProfileError("serialized graph edge count does not match manifest")


def load_any_frozen_graph(path: Path) -> FrozenGraphManifest:
    manifest = load_graph_manifest(path)
    if manifest.num_nodes != 1 << manifest.scale:
        raise ProfileError("graph node count does not match scale")
    validate_frozen_graph(manifest)
    return manifest


def validate_scaling_sequence(manifests):
    manifests = tuple(manifests)
    if tuple(row.scale for row in manifests) != SCALING_SCALES:
        raise ProfileError("scaling graph manifests must be g4,g12,g14,g20")
    for row in manifests:
        if row.num_nodes != 1 << row.scale:
            raise ProfileError("graph node count does not match scale")
    return manifests


def validate_scaling_endpoint_hashes(manifests):
    manifests = validate_scaling_sequence(manifests)
    for row in manifests:
        expected = SCALING_GRAPH_HASHES.get(row.scale)
        if expected is not None and row.graph_sha256 != expected:
            raise ProfileError(
                f"g{row.scale} graph SHA-256 does not match formal input"
            )
    return manifests


def load_scaling_graphs(paths):
    manifests = validate_scaling_sequence(
        load_any_frozen_graph(path) for path in paths
    )
    return validate_scaling_endpoint_hashes(manifests)


def load_scaling_profile(manifest_path: Path) -> ExperimentProfile:
    manifest = load_any_frozen_graph(manifest_path)
    expected_hash = SCALING_GRAPH_HASHES.get(manifest.scale)
    if expected_hash is not None and manifest.graph_sha256 != expected_hash:
        raise ProfileError(
            f"g{manifest.scale} graph SHA-256 does not match formal input"
        )
    return ExperimentProfile(
        name=SCALING_PROFILE_NAME,
        graph_scale=manifest.scale,
        graph_sha256=manifest.graph_sha256,
        num_nodes=manifest.num_nodes,
        cores=4,
        threads=4,
        latencies=("1us",),
    )


def load_formal_offload_profile(manifest_path: Path) -> ExperimentProfile:
    manifest = load_any_frozen_graph(manifest_path)
    if manifest.scale not in FORMAL_SCALES:
        raise ProfileError(
            f"formal offload profile requires g12/g14/g20, got g{manifest.scale}"
        )
    expected_hash = SCALING_GRAPH_HASHES.get(manifest.scale)
    if expected_hash is not None and manifest.graph_sha256 != expected_hash:
        raise ProfileError(
            f"g{manifest.scale} graph SHA-256 does not match formal input"
        )
    return ExperimentProfile(
        name=FORMAL_PROFILE_NAME,
        graph_scale=manifest.scale,
        graph_sha256=manifest.graph_sha256,
        num_nodes=manifest.num_nodes,
        cores=4,
        threads=4,
        logical_partitions=4,
        latencies=("1us",),
    )


def validate_formal_offload_profile(
    profile: ExperimentProfile,
) -> ExperimentProfile:
    actual = (
        profile.name,
        profile.graph_scale in FORMAL_SCALES,
        profile.cores,
        profile.threads,
        profile.logical_partitions,
        profile.latencies,
        profile.trials,
        profile.measured_trial,
        profile.page_rank_iterations,
    )
    expected = (FORMAL_PROFILE_NAME, True, 4, 4, 4, ("1us",), 2, 1, 20)
    if actual != expected:
        raise ProfileError(f"formal offload profile differs: {actual}")
    return profile


def validate_scaling_profile(profile: ExperimentProfile) -> ExperimentProfile:
    actual = (
        profile.name,
        profile.graph_scale in SCALING_SCALES,
        profile.cores,
        profile.threads,
        profile.latencies,
        profile.trials,
        profile.measured_trial,
        profile.page_rank_iterations,
    )
    expected = (SCALING_PROFILE_NAME, True, 4, 4, ("1us",), 2, 1, 20)
    if actual != expected:
        raise ProfileError(f"formal scaling profile differs: {actual}")
    return profile


def load_frozen_profile(name: str, manifest_path: Path) -> ExperimentProfile:
    try:
        expected_scale, latencies = FROZEN_PROFILE_CONTRACTS[name]
    except KeyError as error:
        raise ProfileError(f"unknown frozen profile: {name}") from error
    manifest = load_graph_manifest(manifest_path)
    if manifest.scale != expected_scale:
        raise ProfileError(
            f"profile {name} requires scale {expected_scale}, "
            f"got {manifest.scale}"
        )
    if manifest.num_nodes != 1 << expected_scale:
        raise ProfileError("graph node count does not match scale")
    validate_frozen_graph(manifest)
    return ExperimentProfile(
        name=name,
        graph_scale=expected_scale,
        graph_sha256=manifest.graph_sha256,
        num_nodes=manifest.num_nodes,
        cores=4,
        threads=4,
        latencies=latencies,
    )
