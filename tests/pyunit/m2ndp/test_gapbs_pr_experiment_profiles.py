# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
import json
import os
import struct
from pathlib import Path
from unittest import mock

from scripts import gapbs_pr_experiment_profiles as profiles
from scripts import m2ndp_artifacts as artifacts


def write_serialized_graph(path, *, nodes, edges):
    offsets = [0] * nodes + [edges]
    neighbors = [0] * edges
    path.write_bytes(
        struct.pack("<?qq", False, edges, nodes)
        + struct.pack(f"<{nodes + 1}q", *offsets)
        + struct.pack(f"<{edges}i", *neighbors)
    )


def write_generator(path, payload=b"generator-v1"):
    path.write_bytes(payload)
    os.chmod(path, 0o755)


class ExperimentProfileTest(unittest.TestCase):
    def make_manifest(self, root, scale, *, edges=3):
        graph = root / f"g{scale}.sg"
        generator = root / f"generator-{scale}"
        manifest_path = root / f"g{scale}.manifest.json"
        write_serialized_graph(graph, nodes=1 << scale, edges=edges)
        write_generator(generator)
        value = {
            "schema": 1,
            "scale": scale,
            "graph": str(graph.resolve()),
            "graph_sha256": artifacts.sha256_file(graph),
            "generator": str(generator.resolve()),
            "generator_sha256": artifacts.sha256_file(generator),
            "generator_command": [
                str(generator.resolve()), "-g", str(scale), "-b",
                str(graph.resolve()),
            ],
            "num_nodes": 1 << scale,
            "directed_edges": edges,
        }
        manifest_path.write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )
        return manifest_path, value

    def test_g4_profile_is_four_thread_four_latency_contract(self):
        profile = profiles.get_profile("g4-4thread-sweep")
        self.assertEqual(profile.graph_scale, 4)
        self.assertEqual(profile.graph_sha256, profiles.G4_SHA256)
        self.assertEqual(profile.num_nodes, 16)
        self.assertEqual(profile.cores, 4)
        self.assertEqual(profile.threads, 4)
        self.assertEqual(
            profile.latencies,
            ("200ns", "500ns", "1us", "2us"),
        )

    def test_scaling_profile_is_four_thread_one_microsecond_contract(self):
        profile = profiles.get_scaling_profile()
        self.assertEqual(profile.name, "pr-scaling-4thread-1us")
        self.assertEqual(profile.scales, (4, 12, 14, 20))
        self.assertEqual(profile.cores, 4)
        self.assertEqual(profile.threads, 4)
        self.assertEqual(profile.latencies, ("1us",))
        self.assertEqual(profile.trials, 2)
        self.assertEqual(profile.measured_trial, 1)
        self.assertEqual(profile.page_rank_iterations, 20)

    def test_formal_offload_profile_is_four_way_g12_g14_g20(self):
        profile = profiles.get_formal_offload_profile()
        self.assertEqual(profile.name, "pr-offload-4thread-1us")
        self.assertEqual(profile.scales, (12, 14, 20))
        self.assertEqual(profile.cores, 4)
        self.assertEqual(profile.threads, 4)
        self.assertEqual(profile.logical_partitions, 4)
        self.assertEqual(profile.latencies, ("1us",))
        self.assertEqual(profile.trials, 2)
        self.assertEqual(profile.measured_trial, 1)
        self.assertEqual(profile.page_rank_iterations, 20)

    def test_formal_offload_spectrum_profile_preserves_scale_contract(self):
        profile = profiles.get_formal_offload_spectrum_profile()
        self.assertEqual(profile.name, "pr-offload-4thread-spectrum")
        self.assertEqual(profile.scales, (12, 14, 20))
        self.assertEqual(profile.cores, 4)
        self.assertEqual(profile.threads, 4)
        self.assertEqual(profile.logical_partitions, 4)
        self.assertEqual(
            profile.latencies,
            ("200ns", "500ns", "1us", "2us"),
        )
        self.assertEqual(profile.trials, 2)
        self.assertEqual(profile.measured_trial, 1)
        self.assertEqual(profile.page_rank_iterations, 20)
        self.assertTrue(profiles.is_formal_offload_profile(profile.name))
        self.assertTrue(
            profiles.is_formal_offload_profile("pr-offload-4thread-1us")
        )
        self.assertFalse(
            profiles.is_formal_offload_profile("pr-scaling-4thread-1us")
        )

    def test_formal_offload_spectrum_loads_g20_and_keeps_legacy_1us_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path, manifest = self.make_manifest(Path(tmp), 20)
            with mock.patch.dict(
                profiles.SCALING_GRAPH_HASHES,
                {20: manifest["graph_sha256"]},
            ):
                spectrum = profiles.validate_formal_offload_spectrum_profile(
                    profiles.load_formal_offload_spectrum_profile(manifest_path)
                )
                legacy = profiles.load_formal_offload_profile(manifest_path)
            self.assertEqual(
                profiles.require_latency(spectrum, "500ns"),
                profiles.LATENCY_TICKS["500ns"],
            )
            with self.assertRaisesRegex(
                profiles.ProfileError,
                "latency 500ns is outside profile pr-offload-4thread-1us",
            ):
                profiles.require_latency(legacy, "500ns")

    def test_legacy_two_thread_profile_is_diagnostic_only(self):
        self.assertNotIn("g20-2thread-1us", profiles.FORMAL_PROFILE_NAMES)
        legacy = profiles.get_legacy_diagnostic_profile(
            "g20-2thread-1us"
        )
        self.assertEqual(legacy.cores, 2)
        self.assertEqual(legacy.logical_partitions, 2)

    def test_graph_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "g4.sg"
            graph.write_bytes(b"wrong graph")
            with self.assertRaisesRegex(
                profiles.ProfileError, "graph SHA-256"
            ):
                profiles.validate_graph(
                    profiles.get_profile("g4-4thread-sweep"), graph
                )

    def test_latency_outside_profile_is_rejected(self):
        with self.assertRaisesRegex(
            profiles.ProfileError, "latency 3us"
        ):
            profiles.require_latency(
                profiles.get_profile("g4-4thread-sweep"), "3us"
            )

    def test_frozen_g14_profile_loads_hash_from_manifest(self):
        self.assertTrue(
            hasattr(profiles, "load_frozen_profile"),
            "load_frozen_profile must load a frozen graph manifest",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g14.sg"
            generator = root / "converter"
            manifest_path = root / "g14.manifest.json"
            write_serialized_graph(graph, nodes=1 << 14, edges=7)
            write_generator(generator)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "scale": 14,
                        "graph": str(graph.resolve()),
                        "graph_sha256": artifacts.sha256_file(graph),
                        "generator": str(generator.resolve()),
                        "generator_sha256": artifacts.sha256_file(generator),
                        "generator_command": [
                            str(generator.resolve()),
                            "-g",
                            "14",
                            "-b",
                            str(graph.resolve()),
                        ],
                        "num_nodes": 1 << 14,
                        "directed_edges": 7,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            profile = profiles.load_frozen_profile(
                "g14-4thread-sweep", manifest_path
            )

            self.assertEqual(profile.graph_scale, 14)
            self.assertEqual(profile.graph_sha256, artifacts.sha256_file(graph))
            self.assertEqual(profile.num_nodes, 1 << 14)
            self.assertEqual(profile.cores, 4)
            self.assertEqual(profile.threads, 4)
            self.assertEqual(
                profile.latencies, ("200ns", "500ns", "1us", "2us")
            )

    def test_frozen_profile_rejects_changed_graph_or_generator(self):
        self.assertTrue(hasattr(profiles, "load_frozen_profile"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g12.sg"
            generator = root / "converter"
            manifest_path = root / "g12.manifest.json"
            write_serialized_graph(graph, nodes=1 << 12, edges=5)
            write_generator(generator)
            manifest = {
                "schema": 1,
                "scale": 12,
                "graph": str(graph.resolve()),
                "graph_sha256": artifacts.sha256_file(graph),
                "generator": str(generator.resolve()),
                "generator_sha256": artifacts.sha256_file(generator),
                "generator_command": [
                    str(generator.resolve()), "-g", "12", "-b",
                    str(graph.resolve()),
                ],
                "num_nodes": 1 << 12,
                "directed_edges": 5,
            }
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )

            graph.write_bytes(graph.read_bytes() + b"changed")
            with self.assertRaisesRegex(profiles.ProfileError, "graph SHA-256"):
                profiles.load_frozen_profile(
                    "g12-4thread-qualification", manifest_path
                )

            write_serialized_graph(graph, nodes=1 << 12, edges=5)
            manifest["graph_sha256"] = artifacts.sha256_file(graph)
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )
            write_generator(generator, b"generator-v2")
            with self.assertRaisesRegex(
                profiles.ProfileError, "generator SHA-256"
            ):
                profiles.load_frozen_profile(
                    "g12-4thread-qualification", manifest_path
                )

    def test_any_frozen_graph_supports_all_scaling_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for scale in (4, 12, 14, 20):
                path, _ = self.make_manifest(root, scale)
                rows.append(profiles.load_any_frozen_graph(path))
            self.assertEqual(tuple(row.scale for row in rows), (4, 12, 14, 20))
            self.assertEqual(tuple(row.num_nodes for row in rows),
                             tuple(1 << scale for scale in (4, 12, 14, 20)))

    def test_scaling_sequence_rejects_reordered_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for scale in (4, 12, 14, 20):
                path, _ = self.make_manifest(root, scale)
                rows.append(profiles.load_any_frozen_graph(path))
            with self.assertRaisesRegex(profiles.ProfileError, "g4,g12,g14,g20"):
                profiles.validate_scaling_sequence(
                    (rows[0], rows[2], rows[1], rows[3])
                )

    def test_scaling_endpoint_hashes_are_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for scale in (4, 12, 14, 20):
                path, _ = self.make_manifest(root, scale)
                rows.append(profiles.load_any_frozen_graph(path))
            with self.assertRaisesRegex(profiles.ProfileError, "g4 graph SHA-256"):
                profiles.validate_scaling_endpoint_hashes(rows)


if __name__ == "__main__":
    unittest.main()
