# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import generate_mcfreg2_state as generator
except ImportError:
    generator = None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GenerateMCFREG2Test(unittest.TestCase):
    APPROVED_SOURCE = Path("/home/victoryang00/CXLMemUring")
    APPROVED_COMMIT = "2b30de22399402d8c44bd74b8ebf743b6a6a55e9"
    APPROVED_INPUT_SHA256 = (
        "aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849"
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def require_module(self):
        self.assertIsNotNone(
            generator, "scripts.generate_mcfreg2_state is missing"
        )

    def make_source_repo(self):
        source = self.root / "source"
        mcf = source / "bench/mcf"
        (mcf / "data/ref/input").mkdir(parents=True)
        (mcf / "mcf.c").write_text("int mcf(void) { return 0; }\n")
        (mcf / "defines.h").write_text("typedef long cost_t;\n")
        input_path = mcf / "data/ref/input/inp.in"
        input_path.write_text("2 1\n", encoding="ascii")
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "add", "bench/mcf"], cwd=source, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"],
            cwd=source,
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        return source.resolve(), commit, input_path.resolve()

    def freeze(self, source, commit, input_path, name="frozen"):
        self.require_module()
        return generator.freeze_source(
            source_root=source,
            expected_commit=commit,
            source_subdir="bench/mcf",
            input_path=input_path,
            expected_input_sha256=sha256(input_path),
            destination=self.root / name,
        )

    def test_freezer_ignores_unrelated_dirt_and_hashes_tracked_mcf(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        (source / "unrelated.log").write_text("dirty", encoding="utf-8")

        frozen = self.freeze(source, commit, input_path)

        self.assertEqual(frozen["source_commit"], commit)
        self.assertEqual(frozen["tracked_file_count"], 3)
        self.assertRegex(frozen["source_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(frozen["input_sha256"], sha256(input_path))
        self.assertEqual(frozen["input_bytes"], input_path.stat().st_size)
        copied_root = Path(frozen["copied_source_root"])
        self.assertTrue((copied_root / "bench/mcf/mcf.c").is_file())
        self.assertFalse((copied_root / "unrelated.log").exists())

    def test_freezer_rejects_dirty_mcf_path(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        (source / "bench/mcf/mcf.c").write_text("changed\n")
        with self.assertRaisesRegex(generator.GenerationError, "source.*dirty"):
            self.freeze(source, commit, input_path)

    def test_freezer_rejects_commit_and_input_hash_drift(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        with self.assertRaisesRegex(generator.GenerationError, "commit"):
            generator.freeze_source(
                source_root=source,
                expected_commit="0" * 40,
                source_subdir="bench/mcf",
                input_path=input_path,
                expected_input_sha256=sha256(input_path),
                destination=self.root / "wrong-commit",
            )
        with self.assertRaisesRegex(generator.GenerationError, "input SHA-256"):
            generator.freeze_source(
                source_root=source,
                expected_commit=commit,
                source_subdir="bench/mcf",
                input_path=input_path,
                expected_input_sha256="0" * 64,
                destination=self.root / "wrong-input",
            )

    def test_freezer_requires_fresh_destination(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        destination = self.root / "occupied"
        destination.mkdir()
        with self.assertRaisesRegex(generator.GenerationError, "destination"):
            generator.freeze_source(
                source_root=source,
                expected_commit=commit,
                source_subdir="bench/mcf",
                input_path=input_path,
                expected_input_sha256=sha256(input_path),
                destination=destination,
            )

    def freeze_approved_source(self, name="approved-frozen"):
        if not self.APPROVED_SOURCE.is_dir():
            self.skipTest("approved CXLMemUring source is unavailable")
        input_path = (
            self.APPROVED_SOURCE / "bench/mcf/data/ref/input/inp.in"
        ).resolve()
        return generator.freeze_source(
            source_root=self.APPROVED_SOURCE,
            expected_commit=self.APPROVED_COMMIT,
            source_subdir="bench/mcf",
            input_path=input_path,
            expected_input_sha256=self.APPROVED_INPUT_SHA256,
            destination=self.root / name,
        )

    def test_common_patch_builds_and_runs_explicit_input(self):
        self.require_module()
        self.assertTrue(
            hasattr(generator, "prepare_native_source"),
            "prepare_native_source is missing",
        )
        if shutil.which("cc") is None:
            self.skipTest("C compiler is unavailable")
        frozen = self.freeze_approved_source()
        prepared = generator.prepare_native_source(
            frozen=frozen,
            capture_enabled=False,
        )
        binary = generator.build_native(
            prepared=prepared,
            output=self.root / "mcf-authority",
            compiler=shutil.which("cc"),
        )
        tiny_input = self.root / "tiny.in"
        tiny_input.write_text("1 0\n0 10\n", encoding="ascii")
        output_root = self.root / "authority-run"
        run = generator.run_native(
            binary=binary,
            input_path=tiny_input,
            output_root=output_root,
        )

        self.assertEqual(run["roi_begin"], "after_primal_start_artificial")
        self.assertEqual(run["roi_end"], "after_global_opt")
        self.assertEqual(run["roi_begin_count"], 1)
        self.assertEqual(run["roi_end_count"], 1)
        self.assertEqual(run["capture_enabled"], False)
        self.assertEqual(run["input"], str(tiny_input.resolve()))
        self.assertGreater(run["peak_allocated_bytes"], 0)
        self.assertTrue((output_root / "initial.state").is_file())
        self.assertTrue((output_root / "final.state").is_file())
        self.assertTrue((output_root / "mcf.out").is_file())
        self.assertEqual(
            run["mcf_output_bytes"], (output_root / "mcf.out").stat().st_size
        )

    def test_common_patch_is_content_bound_and_source_copy_only(self):
        self.require_module()
        self.assertTrue(
            hasattr(generator, "prepare_native_source"),
            "prepare_native_source is missing",
        )
        frozen = self.freeze_approved_source()
        original = self.APPROVED_SOURCE / "bench/mcf/mcf.c"
        original_sha256 = sha256(original)

        prepared = generator.prepare_native_source(
            frozen=frozen,
            capture_enabled=False,
        )

        self.assertRegex(prepared["common_patch_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            sha256(Path(prepared["source_dir"]) / "mcf.c"), original_sha256
        )
        self.assertEqual(sha256(original), original_sha256)
        self.assertTrue(
            (Path(prepared["source_dir"]) / "mcf_capture.c").is_file()
        )

    def test_common_harness_fails_closed_on_invalid_sizes_and_links(self):
        self.require_module()
        if shutil.which("cc") is None:
            self.skipTest("C compiler is unavailable")
        frozen = self.freeze_approved_source()
        prepared = generator.prepare_native_source(
            frozen=frozen,
            capture_enabled=False,
        )
        source_dir = Path(prepared["source_dir"])
        probe_source = self.root / "capture_fail_closed.c"
        probe_source.write_text(
            r'''#include "mcf_capture.h"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    network_t net;
    if (argc != 3 || mcf_capture_configure("fixture.in", argv[2], 0))
        return 10;
    if (strcmp(argv[1], "overflow") == 0)
        return mcf_capture_allocation(
            "nodes", UINT64_MAX, 2, 0) == 0 ? 11 : 0;
    memset(&net, 0, sizeof(net));
    if (strcmp(argv[1], "capacity") == 0) {
        net.n = -1;
        return mcf_capture_roi_begin(&net) == 0 ? 12 : 0;
    }
    if (strcmp(argv[1], "malformed") != 0)
        return 13;
    net.n = 1;
    net.m = 1;
    net.max_m = 1;
    net.nodes = calloc(2, sizeof(node_t));
    net.arcs = calloc(1, sizeof(arc_t));
    net.dummy_arcs = calloc(1, sizeof(arc_t));
    if (!net.nodes || !net.arcs || !net.dummy_arcs)
        return 14;
    net.nodes[0].child = (node_t *)((unsigned char *)net.nodes + 1);
    return mcf_capture_roi_begin(&net) == 0 ? 15 : 0;
}
''',
            encoding="ascii",
        )
        probe = self.root / "capture-fail-closed"
        subprocess.run(
            [
                shutil.which("cc"),
                "-std=gnu11",
                "-I",
                str(source_dir),
                str(probe_source),
                str(source_dir / "mcf_capture.c"),
                "-o",
                str(probe),
            ],
            check=True,
        )
        for mode in ("overflow", "capacity", "malformed"):
            with self.subTest(mode=mode):
                output_root = self.root / f"probe-{mode}"
                output_root.mkdir()
                completed = subprocess.run(
                    [str(probe), mode, str(output_root)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )

    def test_pricing_capture_preserves_native_order_and_basket_state(self):
        self.require_module()
        if shutil.which("cc") is None:
            self.skipTest("C compiler is unavailable")
        tiny_input = self.root / "pricing.in"
        tiny_input.write_text(
            "2 2\n0 10\n2 12\n1 2 5\n2 1 7\n",
            encoding="ascii",
        )

        capture_frozen = self.freeze_approved_source("capture-frozen")
        capture_prepared = generator.prepare_native_source(
            frozen=capture_frozen,
            capture_enabled=True,
        )
        self.assertRegex(
            capture_prepared["capture_patch_sha256"], r"^[0-9a-f]{64}$"
        )
        capture_binary = generator.build_native(
            prepared=capture_prepared,
            output=self.root / "mcf-capture",
            compiler=shutil.which("cc"),
        )
        capture_root = self.root / "capture-run"
        capture_run = generator.run_native(
            binary=capture_binary,
            input_path=tiny_input,
            output_root=capture_root,
        )

        journal_path = capture_root / "pricing.jsonl"
        self.assertTrue(journal_path.is_file())
        rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        calls = {}
        for row in rows:
            calls.setdefault(row["call"], []).append(row)
        self.assertGreater(len(calls), 1)
        self.assertEqual(capture_run["pricing_calls"], len(calls))

        previous_live_out = None
        for call_id, frame in sorted(calls.items()):
            self.assertEqual(frame[0]["kind"], "BEGIN")
            self.assertEqual(frame[-1]["kind"], "END")
            if call_id == 0:
                self.assertTrue(frame[0]["initialize"])
            scans = [row for row in frame if row["kind"] == "SCAN"]
            live_in = [
                row for row in frame
                if row["kind"] == "BASKET" and row["phase"] == "live_in"
            ]
            live_out = [
                row for row in frame
                if row["kind"] == "BASKET" and row["phase"] == "live_out"
            ]
            self.assertEqual(frame[-1]["arcs_priced"], len(scans))
            expected_arc_ids = []
            group_pos = frame[0]["group_pos"]
            while True:
                expected_arc_ids.extend(
                    range(
                        group_pos,
                        frame[0]["m"],
                        frame[0]["nr_group"],
                    )
                )
                group_pos = (group_pos + 1) % frame[0]["nr_group"]
                if group_pos == frame[-1]["group_pos"]:
                    break
            self.assertEqual(
                [scan["arc_id"] for scan in scans], expected_arc_ids
            )
            for scan in scans:
                self.assertEqual(
                    scan["reduced_cost"],
                    scan["arc_cost"]
                    - scan["tail_potential"]
                    + scan["head_potential"],
                )
                expected_candidate = (
                    scan["reduced_cost"] < 0 and scan["ident"] == 1
                ) or (
                    scan["reduced_cost"] > 0 and scan["ident"] == 2
                )
                self.assertEqual(scan["candidate"], expected_candidate)
                self.assertEqual(
                    scan["group_pos"],
                    scan["arc_id"] % frame[0]["nr_group"],
                )
                if scan["basket_slot"] >= 0:
                    self.assertTrue(scan["candidate"])
                    self.assertGreater(scan["basket_slot"], 0)
            self.assertEqual(
                [row["slot"] for row in live_in],
                list(range(1, len(live_in) + 1)),
            )
            self.assertEqual(
                [row["slot"] for row in live_out],
                list(range(1, len(live_out) + 1)),
            )
            self.assertEqual(
                [row["abs_cost"] for row in live_out],
                sorted(
                    (row["abs_cost"] for row in live_out), reverse=True
                ),
            )
            if live_out:
                self.assertEqual(
                    frame[-1]["selected_arc_id"], live_out[0]["arc_id"]
                )
                self.assertEqual(
                    frame[-1]["reduced_cost"], live_out[0]["cost"]
                )
            else:
                self.assertEqual(frame[-1]["selected_arc_id"], -1)
                self.assertEqual(frame[-1]["reduced_cost"], 0)
            if previous_live_out is not None:
                retained_candidates = {
                    row["arc_id"] for row in previous_live_out[1:]
                }
                self.assertTrue(
                    {row["arc_id"] for row in live_in}.issubset(
                        retained_candidates
                    )
                )
            previous_live_out = live_out

        authority_frozen = self.freeze_approved_source("authority-frozen")
        authority_prepared = generator.prepare_native_source(
            frozen=authority_frozen,
            capture_enabled=False,
        )
        authority_binary = generator.build_native(
            prepared=authority_prepared,
            output=self.root / "mcf-authority-pricing",
            compiler=shutil.which("cc"),
        )
        authority_root = self.root / "authority-pricing-run"
        generator.run_native(
            binary=authority_binary,
            input_path=tiny_input,
            output_root=authority_root,
        )
        self.assertEqual(
            (capture_root / "final.state").read_bytes(),
            (authority_root / "final.state").read_bytes(),
        )
        self.assertEqual(
            (capture_root / "mcf.out").read_bytes(),
            (authority_root / "mcf.out").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
