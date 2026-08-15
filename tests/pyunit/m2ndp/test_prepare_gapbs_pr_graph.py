# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts


def write_serialized_graph(path, *, nodes, edges, directed=False):
    offsets = [0] * nodes + [edges]
    neighbors = [0] * edges
    payload = (
        struct.pack("<?qq", directed, edges, nodes)
        + struct.pack(f"<{nodes + 1}q", *offsets)
        + struct.pack(f"<{edges}i", *neighbors)
    )
    if directed:
        payload += (
            struct.pack(f"<{nodes + 1}q", *offsets)
            + struct.pack(f"<{edges}i", *neighbors)
        )
    path.write_bytes(payload)


class PrepareGapbsPrGraphTest(unittest.TestCase):
    def load_module(self):
        try:
            return importlib.import_module("scripts.prepare_gapbs_pr_graph")
        except ModuleNotFoundError:
            return None

    def test_prepare_graph_module_exists(self):
        self.assertIsNotNone(
            self.load_module(), "scripts.prepare_gapbs_pr_graph must exist"
        )

    def test_write_manifest_records_and_freezes_complete_provenance(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g14.sg"
            generator = root / "converter"
            output = root / "g14.manifest.json"
            write_serialized_graph(graph, nodes=1 << 14, edges=9)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)
            command = [
                str(generator.resolve()), "-g", "14", "-b",
                str(graph.resolve()),
            ]

            manifest = graph_prep.write_graph_manifest(
                graph=graph,
                scale=14,
                generator=generator,
                generator_command=command,
                num_nodes=1 << 14,
                directed_edges=9,
                output=output,
            )

            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["scale"], 14)
            self.assertEqual(manifest["graph"], str(graph.resolve()))
            self.assertEqual(
                manifest["graph_sha256"], artifacts.sha256_file(graph)
            )
            self.assertEqual(manifest["generator"], str(generator.resolve()))
            self.assertEqual(
                manifest["generator_sha256"],
                artifacts.sha256_file(generator),
            )
            self.assertEqual(manifest["generator_command"], command)
            self.assertEqual(manifest["num_nodes"], 1 << 14)
            self.assertEqual(manifest["directed_edges"], 9)
            self.assertEqual(json.loads(output.read_text()), manifest)
            self.assertEqual(output.stat().st_mode & 0o777, 0o444)

            repeated = graph_prep.write_graph_manifest(
                graph=graph,
                scale=14,
                generator=generator,
                generator_command=command,
                num_nodes=1 << 14,
                directed_edges=9,
                output=output,
            )
            self.assertEqual(repeated, manifest)

    def test_existing_manifest_rejects_different_contents(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g12.sg"
            generator = root / "converter"
            output = root / "g12.manifest.json"
            write_serialized_graph(graph, nodes=1 << 12, edges=3)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)
            command = [
                str(generator.resolve()), "-g", "12", "-b",
                str(graph.resolve()),
            ]
            graph_prep.write_graph_manifest(
                graph=graph,
                scale=12,
                generator=generator,
                generator_command=command,
                num_nodes=1 << 12,
                directed_edges=3,
                output=output,
            )
            generator.write_bytes(b"changed generator")
            os.chmod(generator, 0o755)

            with self.assertRaisesRegex(
                graph_prep.GraphPreparationError,
                "already exists with different contents",
            ):
                graph_prep.write_graph_manifest(
                    graph=graph,
                    scale=12,
                    generator=generator,
                    generator_command=command,
                    num_nodes=1 << 12,
                    directed_edges=3,
                    output=output,
                )

    def test_manifest_rejects_invalid_contract_fields(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g14.sg"
            generator = root / "converter"
            write_serialized_graph(graph, nodes=1 << 14, edges=2)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)
            command = [
                str(generator.resolve()), "-g", "14", "-b",
                str(graph.resolve()),
            ]
            cases = (
                ({"scale": 13}, "scale must be 4, 12, 14, or 20"),
                ({"num_nodes": (1 << 14) - 1}, "node count"),
                ({"directed_edges": 0}, "edge count"),
                ({"generator_command": []}, "generator command"),
            )
            for index, (override, message) in enumerate(cases):
                kwargs = {
                    "graph": graph,
                    "scale": 14,
                    "generator": generator,
                    "generator_command": command,
                    "num_nodes": 1 << 14,
                    "directed_edges": 2,
                    "output": root / f"invalid-{index}.json",
                    **override,
                }
                with self.subTest(override=override):
                    with self.assertRaisesRegex(
                        graph_prep.GraphPreparationError, message
                    ):
                        graph_prep.write_graph_manifest(**kwargs)

    def test_serialized_graph_header_and_size_are_validated(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid.sg"
            write_serialized_graph(valid, nodes=4, edges=3, directed=True)
            self.assertEqual(
                graph_prep.inspect_serialized_graph(valid), (4, 3, True)
            )

            truncated = root / "truncated.sg"
            truncated.write_bytes(valid.read_bytes()[:-1])
            with self.assertRaisesRegex(
                graph_prep.GraphPreparationError, "serialized graph size"
            ):
                graph_prep.inspect_serialized_graph(truncated)

    def test_prepare_graph_runs_generator_once_then_reuses_frozen_manifest(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generator = root / "converter"
            generator.write_text(
                "#!/usr/bin/env python3\n"
                "import struct, sys\n"
                "scale = int(sys.argv[2])\n"
                "path = sys.argv[4]\n"
                "nodes, edges = 1 << scale, 2\n"
                "offsets = [0] * nodes + [edges]\n"
                "payload = struct.pack('<?qq', False, edges, nodes)\n"
                "payload += struct.pack(f'<{nodes + 1}q', *offsets)\n"
                "payload += struct.pack('<2i', 0, 1)\n"
                "open(path, 'wb').write(payload)\n",
                encoding="utf-8",
            )
            os.chmod(generator, 0o755)

            first = graph_prep.prepare_graph(
                scale=12, root=root / "graphs", generator=generator
            )
            second = graph_prep.prepare_graph(
                scale=12, root=root / "graphs", generator=generator
            )

            self.assertEqual(first, second)
            self.assertEqual(first["scale"], 12)
            self.assertEqual(first["num_nodes"], 1 << 12)
            self.assertEqual(first["directed_edges"], 2)
            self.assertNotIn("profile", first)

    def test_adopt_existing_endpoint_graph_is_read_only_and_frozen(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g4.sg"
            generator = root / "converter"
            output = root / "g4.manifest.json"
            write_serialized_graph(graph, nodes=16, edges=7)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)
            before = graph.read_bytes()
            digest = artifacts.sha256_file(graph)

            with mock.patch.dict(
                graph_prep.profiles.SCALING_GRAPH_HASHES,
                {4: digest},
                clear=True,
            ):
                manifest = graph_prep.adopt_existing_graph(
                    graph=graph,
                    scale=4,
                    generator=generator,
                    output=output,
                )

            self.assertEqual(graph.read_bytes(), before)
            self.assertEqual(manifest["graph_sha256"], digest)
            self.assertEqual(
                manifest["generator_command"],
                [
                    str(generator.resolve()),
                    "-g",
                    "4",
                    "-b",
                    str(graph.resolve()),
                ],
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o444)

    def test_adoption_rejects_nonendpoint_scale(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g12.sg"
            generator = root / "converter"
            write_serialized_graph(graph, nodes=1 << 12, edges=2)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)

            with self.assertRaisesRegex(
                graph_prep.GraphPreparationError,
                "g4 or g20",
            ):
                graph_prep.adopt_existing_graph(
                    graph=graph,
                    scale=12,
                    generator=generator,
                    output=root / "manifest.json",
                )

    def test_adoption_rejects_endpoint_hash_drift(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g20.sg"
            generator = root / "converter"
            write_serialized_graph(graph, nodes=1 << 20, edges=2)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)

            with mock.patch.dict(
                graph_prep.profiles.SCALING_GRAPH_HASHES,
                {20: "0" * 64},
                clear=True,
            ):
                with self.assertRaisesRegex(
                    graph_prep.GraphPreparationError,
                    "g20 graph SHA-256 differs",
                ):
                    graph_prep.adopt_existing_graph(
                        graph=graph,
                        scale=20,
                        generator=generator,
                        output=root / "manifest.json",
                    )

    def test_cli_adopts_existing_endpoint_graph(self):
        graph_prep = self.load_module()
        self.assertIsNotNone(graph_prep)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g4.sg"
            generator = root / "converter"
            output = root / "g4.manifest.json"
            write_serialized_graph(graph, nodes=16, edges=4)
            generator.write_bytes(b"fixed generator")
            os.chmod(generator, 0o755)
            digest = artifacts.sha256_file(graph)

            with mock.patch.dict(
                graph_prep.profiles.SCALING_GRAPH_HASHES,
                {4: digest},
                clear=True,
            ):
                graph_prep.main(
                    [
                        "--scale",
                        "4",
                        "--existing-graph",
                        str(graph),
                        "--generator",
                        str(generator),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(json.loads(output.read_text())["scale"], 4)


if __name__ == "__main__":
    unittest.main()
