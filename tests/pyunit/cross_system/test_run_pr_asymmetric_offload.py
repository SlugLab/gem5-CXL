# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
