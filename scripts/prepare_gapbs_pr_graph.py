#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate and immutably freeze deterministic g12/g14 GAPBS graphs."""

import argparse
import json
import os
import subprocess
from pathlib import Path

try:
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
DEFAULT_GENERATOR = (
    REPO
    / "m5out/g4_4thread_latency_sweep_20260809/build/baseline/bin/converter"
)


class GraphPreparationError(RuntimeError):
    """The graph cannot be generated or frozen without provenance loss."""


def inspect_serialized_graph(path: Path) -> tuple[int, int, bool]:
    try:
        return profiles.inspect_serialized_graph(path)
    except profiles.ProfileError as error:
        raise GraphPreparationError(str(error)) from error


def _canonical_json(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _require_plain_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise GraphPreparationError(f"{label} must be an integer")
    return value


def write_graph_manifest(
    *,
    graph: Path,
    scale: int,
    generator: Path,
    generator_command: list[str],
    num_nodes: int,
    directed_edges: int,
    output: Path,
) -> dict:
    scale = _require_plain_int(scale, "scale")
    num_nodes = _require_plain_int(num_nodes, "node count")
    directed_edges = _require_plain_int(directed_edges, "edge count")
    if scale not in profiles.SCALING_SCALES:
        raise GraphPreparationError("scale must be 4, 12, 14, or 20")
    if num_nodes != 1 << scale:
        raise GraphPreparationError("node count does not match scale")
    if directed_edges <= 0:
        raise GraphPreparationError("edge count must be positive")
    graph = Path(graph).resolve()
    generator = Path(generator).resolve()
    output = Path(output).resolve()
    if not graph.is_file():
        raise GraphPreparationError(f"graph does not exist: {graph}")
    if not generator.is_file() or not os.access(generator, os.X_OK):
        raise GraphPreparationError(
            f"generator is not an executable file: {generator}"
        )
    if (
        not isinstance(generator_command, list)
        or not generator_command
        or any(
            not isinstance(argument, str) or not argument
            for argument in generator_command
        )
    ):
        raise GraphPreparationError("generator command must be nonempty strings")
    canonical_command = [
        str(generator), "-g", str(scale), "-b", str(graph)
    ]
    if generator_command != canonical_command:
        raise GraphPreparationError("generator command is not canonical")
    actual_nodes, actual_edges, _ = inspect_serialized_graph(graph)
    if actual_nodes != num_nodes:
        raise GraphPreparationError("node count does not match serialized graph")
    if actual_edges != directed_edges:
        raise GraphPreparationError("edge count does not match serialized graph")
    manifest = {
        "schema": 1,
        "scale": scale,
        "graph": str(graph),
        "graph_sha256": artifacts.sha256_file(graph),
        "generator": str(generator),
        "generator_sha256": artifacts.sha256_file(generator),
        "generator_command": canonical_command,
        "num_nodes": num_nodes,
        "directed_edges": directed_edges,
    }
    payload = _canonical_json(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o444,
        )
    except FileExistsError:
        try:
            existing = output.read_bytes()
        except OSError as error:
            raise GraphPreparationError(
                f"cannot read existing manifest: {error}"
            ) from error
        if existing != payload:
            raise GraphPreparationError(
                "frozen graph manifest already exists with different contents"
            )
        return manifest
    try:
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        output.unlink(missing_ok=True)
        raise
    return manifest


def adopt_existing_graph(
    *, graph: Path, scale: int, generator: Path, output: Path
) -> dict:
    """Freeze a selected endpoint graph without modifying its bytes."""
    if scale not in (4, 20):
        raise GraphPreparationError(
            "existing graph adoption supports g4 or g20"
        )
    graph = Path(graph).resolve()
    generator = Path(generator).resolve()
    if not graph.is_file():
        raise GraphPreparationError(f"graph does not exist: {graph}")
    if not generator.is_file() or not os.access(generator, os.X_OK):
        raise GraphPreparationError(
            f"generator is not an executable file: {generator}"
        )
    nodes, edges, _ = inspect_serialized_graph(graph)
    if nodes != 1 << scale:
        raise GraphPreparationError("node count does not match scale")
    if artifacts.sha256_file(graph) != profiles.SCALING_GRAPH_HASHES[scale]:
        raise GraphPreparationError(f"g{scale} graph SHA-256 differs")
    command = [str(generator), "-g", str(scale), "-b", str(graph)]
    return write_graph_manifest(
        graph=graph,
        scale=scale,
        generator=generator,
        generator_command=command,
        num_nodes=nodes,
        directed_edges=edges,
        output=output,
    )


def prepare_graph(*, scale: int, root: Path, generator: Path) -> dict:
    if scale not in (12, 14):
        raise GraphPreparationError("scale must be 12 or 14")
    root = Path(root).resolve()
    generator = Path(generator).resolve()
    graph = root / f"g{scale}.sg"
    manifest_path = root / f"g{scale}.manifest.json"
    if manifest_path.exists():
        name = (
            "g12-4thread-qualification"
            if scale == 12
            else "g14-4thread-sweep"
        )
        profiles.load_frozen_profile(name, manifest_path)
        manifest = profiles.load_graph_manifest(manifest_path)
        return {
            **manifest.__dict__,
            "generator_command": list(manifest.generator_command),
        }
    if graph.exists():
        raise GraphPreparationError(
            f"unfrozen graph already exists and will not be overwritten: {graph}"
        )
    if not generator.is_file() or not os.access(generator, os.X_OK):
        raise GraphPreparationError(
            f"generator is not an executable file: {generator}"
        )
    root.mkdir(parents=True, exist_ok=True)
    command = [str(generator), "-g", str(scale), "-b", str(graph)]
    try:
        subprocess.run(command, check=True)
        nodes, edges, _ = inspect_serialized_graph(graph)
        return write_graph_manifest(
            graph=graph,
            scale=scale,
            generator=generator,
            generator_command=command,
            num_nodes=nodes,
            directed_edges=edges,
            output=manifest_path,
        )
    except BaseException:
        if not manifest_path.exists():
            graph.unlink(missing_ok=True)
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate and freeze a deterministic GAPBS PR graph."
    )
    parser.add_argument(
        "--scale", type=int, choices=profiles.SCALING_SCALES, required=True
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--existing-graph", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--generator", type=Path, default=DEFAULT_GENERATOR)
    args = parser.parse_args(argv)
    if args.existing_graph is None:
        if args.root is None:
            parser.error("--root is required when generating a graph")
        if args.output is not None:
            parser.error("--output requires --existing-graph")
    else:
        if args.root is not None:
            parser.error("--root and --existing-graph are mutually exclusive")
        if args.output is None:
            parser.error("--output is required with --existing-graph")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.existing_graph is None:
            manifest = prepare_graph(
                scale=args.scale, root=args.root, generator=args.generator
            )
        else:
            manifest = adopt_existing_graph(
                graph=args.existing_graph,
                scale=args.scale,
                generator=args.generator,
                output=args.output,
            )
    except (GraphPreparationError, profiles.ProfileError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
