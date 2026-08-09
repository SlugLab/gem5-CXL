# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path

from scripts import gapbs_pr_experiment_profiles as profiles


class ExperimentProfileTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
