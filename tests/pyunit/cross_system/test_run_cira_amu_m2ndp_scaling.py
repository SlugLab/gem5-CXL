# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import run_cira_amu_m2ndp_scaling as scaling


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ScalingRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        graphs = []
        for scale in (4, 12, 14, 20):
            graph = self.root / f"g{scale}.sg"
            manifest = self.root / f"g{scale}.manifest.json"
            graph.write_bytes(f"graph-{scale}".encode())
            manifest.write_text("{}\n", encoding="utf-8")
            graphs.append({
                "scale": scale, "path": str(graph.resolve()),
                "sha256": sha(f"graph-{scale}"),
                "manifest": str(manifest.resolve()),
                "manifest_sha256": sha("{}\n"),
                "num_nodes": 1 << scale, "directed_edges": scale,
            })
        self.inputs = self.root / "inputs.json"
        self.inputs.write_text(json.dumps({
            "schema": 1, "status": "accepted", "graphs": graphs,
            "workloads": {},
        }) + "\n", encoding="utf-8")
        self.calibration = self.root / "calibration.json"
        self.calibration.write_text("{}\n", encoding="utf-8")
        self.gem5 = self.root / "gem5.opt"
        self.gem5.write_bytes(b"gem5")
        self.config = self.root / "config.py"
        self.config.write_text("config = 1\n", encoding="utf-8")
        self.options = SimpleNamespace(
            inputs=self.inputs, calibration=self.calibration,
            root=self.root / "evidence", gem5=self.gem5,
            config=self.config, cxlmemuring=self.root / "CXLMemUring",
            m2ndp_root=self.root / "M2NDP", variants_build_root=self.root / "variants",
            timeout=0, resume=False,
        )

    def test_matrix_is_four_scales_by_four_systems_at_1us(self):
        matrix = scaling.build_matrix()
        self.assertEqual(len(matrix), 16)
        self.assertEqual({row.scale for row in matrix}, {4, 12, 14, 20})
        self.assertEqual({row.system for row in matrix},
                         {"vanilla", "amu", "cira", "m2ndp"})
        self.assertTrue(all(row.latency == "1us" and row.full_e2e
                            for row in matrix))

    def test_formal_commands_have_no_sampling_or_smoke_flags(self):
        for system in ("vanilla", "amu", "cira", "m2ndp"):
            command = scaling.command_for(
                scaling.MatrixEntry(20, system), self.options
            )
            joined = " ".join(command)
            self.assertNotIn("--smoke-test", command)
            self.assertNotIn("--window", joined)
            self.assertIn("--profile", command)
            self.assertEqual(
                command[command.index("--profile") + 1],
                "pr-scaling-4thread-1us",
            )
            self.assertIn("--graph-manifest", command)

    def test_vanilla_stops_after_baseline_and_m2ndp_resumes_it(self):
        vanilla = scaling.command_for(scaling.MatrixEntry(14, "vanilla"), self.options)
        m2ndp = scaling.command_for(scaling.MatrixEntry(14, "m2ndp"), self.options)
        self.assertEqual(vanilla[vanilla.index("--stop-after") + 1], "gem5_baseline")
        self.assertIn("--resume", m2ndp)
        self.assertEqual(vanilla[vanilla.index("--outdir") + 1],
                         m2ndp[m2ndp.index("--outdir") + 1])

    def test_config_must_be_four_core_all_cxl_one_microsecond(self):
        config = self.root / "config.ini"
        config.write_text(
            "delay=500000\nnum_cpus=4\nall_memory_cxl=true\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(scaling.ScalingError, "delay"):
            scaling.validate_config(config)

    def test_post_trial0_checkpoint_is_rejected(self):
        with self.assertRaisesRegex(scaling.ScalingError, "trial0_entry"):
            scaling.validate_checkpoint_manifest({"boundary": "trial0_end"})

    def test_one_bit_rank_mismatch_is_rejected(self):
        reference = self.root / "reference.u32"
        actual = self.root / "actual.u32"
        reference.write_bytes(b"\x00\x00\x80\x3f")
        actual.write_bytes(b"\x01\x00\x80\x3f")
        with self.assertRaisesRegex(scaling.ScalingError, "word 0"):
            scaling.validate_rank_bits(reference, actual, expected_words=1)

    def test_complete_requires_all_sixteen_passed_points(self):
        state = scaling.new_state(self.options)
        for entry in scaling.build_matrix()[:-1]:
            scaling.record_pass(state, entry, {"summary": sha(str(entry))})
        self.assertFalse(scaling.is_complete(state))
        scaling.record_pass(state, scaling.build_matrix()[-1], {"summary": "f" * 64})
        self.assertTrue(scaling.is_complete(state))

    def test_state_identity_changes_when_gem5_or_config_changes(self):
        original = scaling.new_state(self.options)
        self.config.write_text("config = 2\n", encoding="utf-8")
        changed_config = scaling.new_state(self.options)
        self.assertNotEqual(
            original["config_sha256"], changed_config["config_sha256"]
        )
        self.gem5.write_bytes(b"different gem5")
        changed_gem5 = scaling.new_state(self.options)
        self.assertNotEqual(
            changed_config["gem5_sha256"], changed_gem5["gem5_sha256"]
        )

    def test_amu_queue_error_and_cira_inactive_core_fail_mechanism_gate(self):
        amu = {
            "status": "ok", "verification": "pass", "asmc_loads": 8,
            "asmc_completed": 8, "asmc_queue_full_errors": 1,
            "asmc_spm_full_errors": 0, "asmc_translation_errors": 0,
            "asmc_pending_errors": 0, "asmc_spm_flag_errors": 0,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "AMU error"):
            scaling.validate_mechanism_row("amu", amu)
        cira = {
            "status": "ok", "verification": "pass",
            "cira_prefetches": 4, "cira_completed": 4,
            "cira_issued_per_core": "2;2;0;0",
            "cira_completed_per_core": "2;2;0;0",
            "cira_rejected_queue_full": 0,
            "cira_rejected_csr_index_queue_full": 0,
            "cira_dropped_csr_descriptors": 0,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "four active cores"):
            scaling.validate_mechanism_row("cira", cira)

    def test_m2ndp_requires_strict_funcsim_and_link_cycle_calibration(self):
        row = {
            "status": "ok", "verification": "pass",
            "funcsim_compared": 16, "funcsim_mismatched": 1,
            "calibration_pass": "pass", "calibration_residual_ns": "0.1",
            "calibration_link_period_ns": "0.5", "kernel_launches": 42,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "FuncSim"):
            scaling.validate_mechanism_row("m2ndp", row)


if __name__ == "__main__":
    unittest.main()
