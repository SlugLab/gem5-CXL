# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import freeze_pr_scaling_inputs as freeze


SCALES = (4, 12, 14, 20)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FreezePrScalingInputsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rows = []
        self.manifest_paths = []
        for scale in SCALES:
            graph = self.root / f"g{scale}.sg"
            generator = self.root / f"converter-{scale}"
            manifest = self.root / f"g{scale}.manifest.json"
            graph.write_bytes(f"graph-{scale}".encode())
            generator.write_bytes(f"generator-{scale}".encode())
            os.chmod(generator, 0o755)
            manifest.write_text("{}\n", encoding="utf-8")
            self.manifest_paths.append(manifest.resolve())
            self.rows.append(
                freeze.profiles.FrozenGraphManifest(
                    schema=1,
                    scale=scale,
                    graph=str(graph.resolve()),
                    graph_sha256=sha256_file(graph),
                    generator=str(generator.resolve()),
                    generator_sha256=sha256_file(generator),
                    generator_command=(
                        str(generator.resolve()),
                        "-g",
                        str(scale),
                        "-b",
                        str(graph.resolve()),
                    ),
                    num_nodes=1 << scale,
                    directed_edges=scale,
                )
            )

    def valid_payload(self):
        with mock.patch.object(
            freeze.profiles,
            "load_scaling_graphs",
            return_value=tuple(self.rows),
        ):
            return freeze.freeze_inputs(self.manifest_paths)

    def validate(self, value):
        with mock.patch.object(
            freeze.profiles,
            "load_scaling_graphs",
            return_value=tuple(self.rows),
        ):
            return freeze.validate_manifest(value)

    def test_freezer_emits_only_scoped_ordered_graphs(self):
        value = self.valid_payload()

        self.assertEqual(
            set(value),
            {
                "schema",
                "status",
                "scope",
                "profile",
                "graphs",
                "graph_set_sha256",
            },
        )
        self.assertEqual(value["schema"], 1)
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(value["scope"], "pr_scaling")
        self.assertEqual(value["profile"], "pr-scaling-4thread-1us")
        self.assertEqual(
            [row["scale"] for row in value["graphs"]], list(SCALES)
        )
        self.assertRegex(value["graph_set_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("workloads", value)
        self.assertEqual(
            set(value["graphs"][0]),
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
            },
        )

    def test_live_manifest_or_graph_hash_drift_is_rejected(self):
        value = self.valid_payload()
        graph_path = Path(value["graphs"][0]["path"])
        graph_bytes = graph_path.read_bytes()
        graph_path.write_bytes(b"changed")
        with self.assertRaisesRegex(
            freeze.ScalingInputError, "graph SHA-256 changed"
        ):
            self.validate(value)
        graph_path.write_bytes(graph_bytes)

        value = self.valid_payload()
        Path(value["graphs"][1]["manifest"]).write_text(
            '{"changed":true}\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(
            freeze.ScalingInputError, "manifest SHA-256 changed"
        ):
            self.validate(value)

    def test_scope_order_and_graph_set_digest_are_exact(self):
        for mutation, message in (
            (lambda value: value.update(scope="scaling_and_breadth"), "scope"),
            (
                lambda value: value["graphs"].reverse(),
                "ordered g4,g12,g14,g20",
            ),
            (
                lambda value: value.update(graph_set_sha256="0" * 64),
                "graph-set SHA-256 differs",
            ),
        ):
            value = self.valid_payload()
            mutation(value)
            with self.subTest(message=message):
                with self.assertRaisesRegex(freeze.ScalingInputError, message):
                    self.validate(value)

    def test_cli_writes_terminal_failed_input_record(self):
        output = self.root / "inputs.json"
        status = freeze.main(
            [
                "--graph-manifest",
                str(self.root / "missing.json"),
                "--output",
                str(output),
            ]
        )

        self.assertEqual(status, 2)
        self.assertFalse(output.exists())
        failure = json.loads(
            (self.root / "failed-input.json").read_text(encoding="utf-8")
        )
        self.assertEqual(failure["schema"], 1)
        self.assertEqual(failure["status"], "failed_input")

    def test_cli_reuses_identical_immutable_output(self):
        output = self.root / "inputs.json"
        arguments = []
        for path in self.manifest_paths:
            arguments.extend(("--graph-manifest", str(path)))
        arguments.extend(("--output", str(output)))
        with mock.patch.object(
            freeze.profiles,
            "load_scaling_graphs",
            return_value=tuple(self.rows),
        ):
            self.assertEqual(freeze.main(arguments), 0)
            before = output.read_bytes()
            self.assertEqual(freeze.main(arguments), 0)

        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(output.stat().st_mode & 0o777, 0o444)
        self.assertFalse((self.root / "failed-input.json").exists())


if __name__ == "__main__":
    unittest.main()
