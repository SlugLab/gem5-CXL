#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Immutable contracts for formal GAPBS PageRank experiments."""

import dataclasses
from pathlib import Path

from scripts import m2ndp_artifacts


G4_SHA256 = (
    "f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d"
)
LATENCY_TICKS = {
    "200ns": 200_000,
    "500ns": 500_000,
    "1us": 1_000_000,
    "2us": 2_000_000,
}


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
    trials: int = 2
    measured_trial: int = 1
    page_rank_iterations: int = 20


PROFILES = {
    "g20-2thread-1us": ExperimentProfile(
        name="g20-2thread-1us",
        graph_scale=20,
        graph_sha256=m2ndp_artifacts.EXPECTED_G20_SHA256,
        num_nodes=1 << 20,
        cores=2,
        threads=2,
        latencies=("1us",),
    ),
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
    return LATENCY_TICKS[latency]
