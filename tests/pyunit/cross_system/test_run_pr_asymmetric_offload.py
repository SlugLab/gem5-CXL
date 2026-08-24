# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import run_pr_asymmetric_offload as runner


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AsymmetricOffloadRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        graphs = []
        for scale in (4, 12, 14, 20):
            graph = self.root / f"g{scale}.sg"
            manifest = self.root / f"g{scale}.manifest.json"
            graph.write_bytes(f"graph-{scale}".encode())
            manifest.write_text(f"{{\"scale\":{scale}}}\n")
            graphs.append({
                "scale": scale, "path": str(graph.resolve()),
                "sha256": sha(graph), "manifest": str(manifest.resolve()),
                "manifest_sha256": sha(manifest),
            })
        self.inputs = self.root / "inputs.json"
        self.inputs.write_text(json.dumps({
            "schema": 1, "profile": "pr-scaling-4thread-1us",
            "graphs": graphs,
        }, sort_keys=True) + "\n")
        files = {}
        for name in ("gem5", "libm5", "config", "calibration", "policy"):
            path = self.root / name
            path.write_bytes(name.encode())
            files[name] = path
        self.options = SimpleNamespace(
            inputs=self.inputs, root=self.root / "campaign",
            gem5=files["gem5"], m5_library=files["libm5"],
            config=files["config"], calibration=files["calibration"],
            policy=files["policy"], cxlmemuring=self.root / "CXLMemUring",
            m2ndp_root=self.root / "M2NDP", variants_build_root=self.root / "variants",
            resume=False, stop_after=None,
            m2ndp_commit="1" * 40,
        )
        self.options.variants_build_root.mkdir()
        (self.options.variants_build_root / "manifest.json").write_text("{}\n")

    def test_selects_only_ordered_g12_g14_g20_and_binds_both_hashes(self):
        selected = runner.select_inputs(self.options)
        self.assertEqual([row["scale"] for row in selected["graphs"]], [12, 14, 20])
        self.assertEqual(selected["profile"], "pr-offload-4thread-1us")
        self.assertEqual(selected["source_inputs_sha256"], sha(self.inputs))
        path = self.options.root / "selected-inputs.json"
        self.assertEqual(selected, json.loads(path.read_text()))
        identity = runner.build_identity(self.options, selected)
        self.assertEqual(identity["source_inputs_sha256"], sha(self.inputs))
        self.assertEqual(identity["selected_inputs_sha256"], sha(path))

    def test_identity_changes_when_m2ndp_trace_generator_changes(self):
        selected = runner.select_inputs(self.options)
        repository = self.root / "repository"
        for relative in (
            "util/pr_offload/source",
            "util/amu/source",
            "util/cira/source",
            "util/m2ndp/patches/patch",
            "scripts/pr_offload_contract.py",
            "scripts/m2ndp_pagerank_trace.py",
            "scripts/run_gapbs_matched_pr_spmv_variants.py",
        ):
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n")
        with mock.patch.object(runner, "REPO", repository):
            before = runner.build_identity(self.options, selected)
            (repository / "scripts/m2ndp_pagerank_trace.py").write_text(
                "changed trace generator\n"
            )
            after = runner.build_identity(self.options, selected)
        self.assertNotEqual(before["source_sha256"], after["source_sha256"])

    def test_identity_changes_when_matched_runner_changes(self):
        selected = runner.select_inputs(self.options)
        repository = self.root / "repository"
        for relative in (
            "util/pr_offload/source",
            "util/amu/source",
            "util/cira/source",
            "util/m2ndp/patches/patch",
            "scripts/pr_offload_contract.py",
            "scripts/m2ndp_pagerank_trace.py",
            "scripts/run_gapbs_matched_pr_spmv_variants.py",
        ):
            path = repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n")
        with mock.patch.object(runner, "REPO", repository):
            before = runner.build_identity(self.options, selected)
            (repository / "scripts/run_gapbs_matched_pr_spmv_variants.py").write_text(
                "changed matched runner\n"
            )
            after = runner.build_identity(self.options, selected)
        self.assertNotEqual(before["source_sha256"], after["source_sha256"])

    def test_missing_reordered_or_changed_formal_graph_fails_closed(self):
        source = json.loads(self.inputs.read_text())
        cases = []
        missing = copy.deepcopy(source); missing["graphs"] = missing["graphs"][:-1]; cases.append(missing)
        reordered = copy.deepcopy(source); reordered["graphs"][1], reordered["graphs"][2] = reordered["graphs"][2], reordered["graphs"][1]; cases.append(reordered)
        changed = copy.deepcopy(source); changed["graphs"][1]["sha256"] = "0" * 64; cases.append(changed)
        for index, value in enumerate(cases):
            path = self.root / f"bad-{index}.json"
            path.write_text(json.dumps(value) + "\n")
            options = copy.copy(self.options); options.inputs = path
            with self.subTest(index=index), self.assertRaises(runner.OffloadError):
                runner.select_inputs(options)

    def test_paths_and_commands_are_formal_unlimited_and_policy_specific(self):
        runner.select_inputs(self.options)
        entries = runner.build_matrix()
        g12 = next(e for e in entries if e.scale == 12 and e.system == "vanilla")
        g20 = next(e for e in entries if e.scale == 20 and e.system == "m2ndp")
        pgo = next(e for e in entries if e.scale == 14 and e.system == "cira-pgo")
        self.assertEqual(
            runner.point_root(self.options.root, g12),
            self.options.root / "qualification/primary/vanilla",
        )
        self.assertEqual(
            runner.point_root(self.options.root, g20),
            self.options.root / "formal/g20/m2ndp",
        )
        self.assertEqual(
            runner.point_root(self.options.root, pgo),
            self.options.root / "ablation/g14/cira-pgo",
        )
        command = runner.command_for(pgo, self.options)
        self.assertIn("pr-offload-4thread-1us", command)
        self.assertNotIn("--timeout", command)
        self.assertIn("pgo-selected", command)
        self.assertIn("--asmc-calibration-manifest", command)
        candidate = next(
            e for e in entries if e.scale == 14 and e.system == "cira-A"
        )
        candidate_command = runner.command_for(candidate, self.options)
        self.assertIn("candidate", candidate_command)
        self.assertIn("A", candidate_command)

        self.options.resume = True
        resumed = runner.command_for(g12, self.options)
        self.assertIn("--resume", resumed)

    def test_cira_worker_completions_require_matching_issues(self):
        row = {
            "cira_issued_per_core": "40;40;40;43",
            "cira_completed_per_core": "40;40;40;43",
        }
        self.assertEqual(
            runner.cira_worker_completions(row), [40, 40, 40, 43]
        )
        row["cira_completed_per_core"] = "40;40;40;42"
        with self.assertRaisesRegex(runner.OffloadError, "differ"):
            runner.cira_worker_completions(row)

    def test_resume_rehashes_every_passed_artifact(self):
        selected = runner.select_inputs(self.options)
        identity = runner.build_identity(self.options, selected)
        state = runner.new_state(identity)
        artifact = self.root / "point.csv"
        artifact.write_text("passed\n")
        entry = runner.build_matrix()[0]
        runner.record_pass(state, entry, {str(artifact): sha(artifact)})
        runner.validate_resume(state, identity)
        artifact.write_text("changed\n")
        with self.assertRaisesRegex(runner.OffloadError, "artifact"):
            runner.validate_resume(state, identity)

    def qualification_points(self, amu="1.4", cira="1.5", m2ndp="1.6"):
        vanilla_ticks = 1_600_000
        points = {
            "g12:vanilla": {
                "scale": 12, "system": "vanilla",
                "sim_ticks": vanilla_ticks,
            },
            "g12:amu": {
                "scale": 12, "system": "amu",
                "sim_ticks": int(Decimal(vanilla_ticks) / Decimal(amu)),
            },
            "g12:cira-few-shot": {
                "scale": 12, "system": "cira-few-shot",
                "sim_ticks": int(Decimal(vanilla_ticks) / Decimal(cira)),
                "selected_candidate": "B",
            },
            "g12:m2ndp": {
                "scale": 12, "system": "m2ndp",
                "ndpsim_cycles": int(Decimal(vanilla_ticks) / Decimal(m2ndp)),
                "ndpsim_core_period_seconds": "1e-12",
            },
        }
        common = {
            "profile": "pr-offload-4thread-1us",
            "cxl_link_delay": "1us", "workers": 4, "iterations": 20,
            "all_memory_cxl": True, "verification": "pass",
            "raw_sha256": "a" * 64,
            "worker_completions": [40, 40, 40, 40],
            "pending": {"all": 0},
        }
        for point in points.values():
            point.update({key: value for key, value in common.items()
                          if key not in point})
        cira_ticks = points["g12:cira-few-shot"]["sim_ticks"]
        points["g12:cira-few-shot"]["phases"] = {
            "formation": 1, "sampling": 1, "selection": 1, "jit": 1,
            "execution": cira_ticks - 5, "drain": 1,
        }
        points["g12:cira-few-shot"]["phase_total_ns"] = cira_ticks
        m2ndp_point = points["g12:m2ndp"]
        m2ndp_point["funcsim"] = {
            "status": "pass", "compared": 1 << 12,
            "mismatched": 0, "completed_at_seq": 1,
        }
        m2ndp_point["ndpsim_started_at_seq"] = 2
        return points

    def test_qualification_gate_is_inclusive_and_checks_three_points(self):
        points = self.qualification_points()
        gate = runner.qualification_gate(points)
        self.assertEqual(gate["checked_points"], 3)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(set(gate["speedups"]), {"amu", "cira-few-shot", "m2ndp"})

    def test_qualification_accepts_measured_m2ndp_without_upper_bound(self):
        points = self.qualification_points()
        points["g12:vanilla"]["sim_ticks"] = 1_990_498_176
        points["g12:amu"]["sim_ticks"] = 1_418_052_861
        points["g12:cira-few-shot"]["sim_ticks"] = 1_392_734_871
        points["g12:cira-few-shot"]["phases"]["execution"] = 1_392_734_866
        points["g12:cira-few-shot"]["phase_total_ns"] = 1_392_734_871
        points["g12:m2ndp"]["ndpsim_cycles"] = 1_511_232
        points["g12:m2ndp"]["ndpsim_core_period_seconds"] = (
            "5.0000000000000003114e-10"
        )
        gate = runner.qualification_gate(points)
        self.assertEqual(
            gate["speedups"]["m2ndp"],
            "2.634272138228941520602758013",
        )
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["offenders"], [])
        self.assertEqual(gate["policies"]["m2ndp"], {
            "minimum": "1.4", "maximum": None,
            "correctness": "bit-exact-funcsim-before-ndpsim",
        })

    def test_qualification_keeps_bounded_and_minimum_failures(self):
        slow_m2ndp = self.qualification_points()
        slow_m2ndp["g12:m2ndp"]["ndpsim_cycles"] = 1_142_858
        cases = (
            (self.qualification_points(amu="1.600001"), "amu"),
            (self.qualification_points(cira="1.600001"), "cira-few-shot"),
            (slow_m2ndp, "m2ndp"),
        )
        for points, offender in cases:
            with self.subTest(offender=offender):
                self.assertEqual(
                    runner.qualification_gate(points)["offenders"],
                    [offender],
                )

    def test_qualification_rejects_1399_bits_and_zero_jit(self):
        offender = self.qualification_points(amu="1.399")
        self.assertEqual(runner.qualification_gate(offender)["status"], "failed")
        bit = self.qualification_points(); bit["g12:amu"]["verification"] = "fail"
        zero_jit = self.qualification_points(); zero_jit["g12:cira-few-shot"]["phases"]["jit"] = 0
        zero_jit["g12:cira-few-shot"]["phases"]["execution"] += 1
        for points in (bit, zero_jit):
            with self.assertRaises(runner.OffloadError):
                runner.qualification_gate(points)

    def test_replay_requires_rank_native_timing_and_cira_policy(self):
        primary = self.qualification_points()
        replay = copy.deepcopy(primary)
        runner.validate_replay(primary, replay)
        mutations = (
            ("g12:amu", "raw_sha256", "b" * 64),
            ("g12:m2ndp", "ndpsim_cycles", replay["g12:m2ndp"]["ndpsim_cycles"] + 1),
            ("g12:cira-few-shot", "selected_candidate", "A"),
        )
        for key, field, value in mutations:
            candidate = copy.deepcopy(replay)
            candidate[key][field] = value
            with self.subTest(key=key, field=field), self.assertRaises(
                runner.OffloadError
            ):
                runner.validate_replay(primary, candidate)

    def test_g12_gate_failure_writes_hold_before_larger_scale(self):
        launched = []
        points = self.qualification_points(amu="1.399")

        def run_point(entry):
            launched.append(entry)
            return copy.deepcopy(points[entry.key])

        with self.assertRaisesRegex(runner.OffloadError, "qualification"):
            runner.run_qualification_state_machine(
                self.options.root, run_point
            )
        self.assertFalse(any(entry.scale > 12 for entry in launched))
        hold = json.loads((
            self.options.root / "diagnostic-performance-hold.json"
        ).read_text())
        self.assertFalse(hold["official_qualification"])


if __name__ == "__main__":
    unittest.main()
