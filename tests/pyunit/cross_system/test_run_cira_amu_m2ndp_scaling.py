# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import freeze_pr_scaling_inputs as freeze
from scripts import run_cira_amu_m2ndp_scaling as scaling


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ScalingRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        graphs = []
        frozen_rows = []
        for scale in (4, 12, 14, 20):
            graph = self.root / f"g{scale}.sg"
            manifest = self.root / f"g{scale}.manifest.json"
            generator = self.root / f"converter-{scale}"
            graph.write_bytes(f"graph-{scale}".encode())
            manifest.write_text("{}\n", encoding="utf-8")
            generator.write_bytes(f"generator-{scale}".encode())
            os.chmod(generator, 0o755)
            command = [
                str(generator.resolve()), "-g", str(scale), "-b",
                str(graph.resolve()),
            ]
            graphs.append({
                "scale": scale, "path": str(graph.resolve()),
                "sha256": sha(f"graph-{scale}"),
                "manifest": str(manifest.resolve()),
                "manifest_sha256": sha("{}\n"),
                "num_nodes": 1 << scale, "directed_edges": scale,
                "generator": str(generator.resolve()),
                "generator_sha256": sha(f"generator-{scale}"),
                "generator_command": command,
            })
            frozen_rows.append(freeze.profiles.FrozenGraphManifest(
                schema=1, scale=scale, graph=str(graph.resolve()),
                graph_sha256=sha(f"graph-{scale}"),
                generator=str(generator.resolve()),
                generator_sha256=sha(f"generator-{scale}"),
                generator_command=tuple(command), num_nodes=1 << scale,
                directed_edges=scale,
            ))
        profile_patch = mock.patch.object(
            freeze.profiles,
            "load_scaling_graphs",
            return_value=tuple(frozen_rows),
        )
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        self.inputs = self.root / "inputs.json"
        self.inputs.write_text(json.dumps({
            "schema": 1, "status": "accepted", "scope": "pr_scaling",
            "profile": "pr-scaling-4thread-1us", "graphs": graphs,
            "graph_set_sha256": freeze.graph_set_sha256(graphs),
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

    def write_real_config(
        self, *, delay="1000000", cores=4, link_range="0:4294967296",
        path=None,
    ):
        config = Path(path) if path is not None else self.root / "config.ini"
        config.parent.mkdir(parents=True, exist_ok=True)
        sections = [
            "[board]",
            "type=System",
            "mem_ranges=0:4294967296",
            "[board.cxl_mem_link0]",
            "type=SerialLink",
            f"delay={delay}",
            f"ranges={link_range}",
            "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[0]",
            "mem_side_port=board.cxl_device_xbar0.cpu_side_ports[0]",
            "[board.cxl_device_xbar0]",
            "type=NoncoherentXBar",
            "cpu_side_ports=board.cxl_mem_link0.mem_side_port",
            "mem_side_ports=board.memory.mem_ctrl.port",
            "[board.memory.mem_ctrl]",
            "type=MemCtrl",
            "port=board.cxl_device_xbar0.mem_side_ports[0]",
            "[board.memory.mem_ctrl.dram]",
            "type=DRAMInterface",
            "range=0:4294967296",
        ]
        for index in range(cores):
            sections.extend(
                (
                    f"[board.processor.cores{index}.core]",
                    "type=BaseTimingSimpleCPU",
                )
            )
        config.write_text("\n".join(sections) + "\n", encoding="utf-8")
        return config

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
        config = self.write_real_config(delay="500000")
        with self.assertRaisesRegex(scaling.ScalingError, "delay"):
            scaling.validate_config(config)

    def test_config_accepts_real_four_core_all_cxl_shape(self):
        topology = scaling.validate_config(self.write_real_config())
        self.assertEqual(topology["cores"], 4)
        self.assertEqual(topology["range"], "0:4294967296")

    def test_config_rejects_missing_core_or_range_bypass(self):
        config = self.write_real_config(cores=3)
        with self.assertRaisesRegex(scaling.ScalingError, "four cores"):
            scaling.validate_config(config)

        config = self.write_real_config(link_range="4096:8192")
        with self.assertRaisesRegex(scaling.ScalingError, "range mismatch"):
            scaling.validate_config(config)

    def test_vanilla_point_validates_its_generated_config(self):
        base = self.options.root / "scales/g4/m2ndp"
        run_dir = base / "gem5/run/m5out"
        summary = base / "gem5/run/summary.csv"
        summary.parent.mkdir(parents=True, exist_ok=True)
        with summary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=("status", "verification", "run_dir"),
            )
            writer.writeheader()
            writer.writerow({
                "status": "ok", "verification": "pass",
                "run_dir": str(run_dir),
            })
        reference = base / "reference/scores.raw"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"\x00" * (16 * 4))
        self.write_real_config(delay="500000", path=run_dir / "config.ini")

        with self.assertRaisesRegex(scaling.ScalingError, "delay"):
            scaling._point_outputs(
                scaling.MatrixEntry(4, "vanilla"), self.options
            )

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
            scaling.record_pass(
                state, entry, {"summary": sha(str(entry))},
                latency_seconds=str(entry.scale),
                output_elements=1 << entry.scale,
                mechanism={"verification": "pass"},
            )
        self.assertFalse(scaling.is_complete(state))
        last = scaling.build_matrix()[-1]
        scaling.record_pass(
            state, last, {"summary": "f" * 64},
            latency_seconds=str(last.scale),
            output_elements=1 << last.scale,
            mechanism={"verification": "pass"},
        )
        self.assertTrue(scaling.is_complete(state))
        self.assertEqual(state["points"]["g20:m2ndp"]["speedup"], "1")

    def test_record_pass_recomputes_speedup_from_absolute_seconds(self):
        state = scaling.new_state(self.options)
        vanilla = scaling.MatrixEntry(4, "vanilla")
        amu = scaling.MatrixEntry(4, "amu")
        common = {
            "output_elements": 16,
            "mechanism": {"verification": "pass"},
        }
        scaling.record_pass(
            state, vanilla, {"summary": sha("vanilla")},
            latency_seconds="4", **common,
        )
        scaling.record_pass(
            state, amu, {"summary": sha("amu")},
            latency_seconds="2", **common,
        )
        self.assertEqual(state["points"][amu.key]["latency_seconds"], "2")
        self.assertEqual(state["points"][amu.key]["speedup"], "2")

    def test_record_pass_rejects_nonpositive_time_or_missing_mechanism(self):
        state = scaling.new_state(self.options)
        entry = scaling.MatrixEntry(4, "vanilla")
        with self.assertRaisesRegex(scaling.ScalingError, "latency"):
            scaling.record_pass(
                state, entry, {"summary": sha("x")},
                latency_seconds="0", output_elements=16,
                mechanism={"verification": "pass"},
            )
        with self.assertRaisesRegex(scaling.ScalingError, "mechanism"):
            scaling.record_pass(
                state, entry, {"summary": sha("x")},
                latency_seconds="1", output_elements=16,
                mechanism={},
            )

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

    def test_runner_rejects_general_breadth_manifest(self):
        value = json.loads(self.inputs.read_text())
        value["scope"] = "scaling_and_breadth"
        self.inputs.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(scaling.ScalingError, "scope"):
            scaling.load_inputs(self.inputs)

    def test_state_records_graph_set_and_g20_identity(self):
        inputs = scaling.load_inputs(self.inputs)
        state = scaling.new_state(self.options)
        self.assertEqual(
            state["graph_set_sha256"], inputs["graph_set_sha256"]
        )
        self.assertEqual(
            state["g20_graph_sha256"],
            next(
                row for row in inputs["graphs"] if row["scale"] == 20
            )["sha256"],
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
