# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
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
        price_out_rows = [
            json.loads(line)
            for line in (capture_root / "price_out.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        price_out_calls = {
            row["call"] for row in price_out_rows if "call" in row
        }
        self.assertGreater(len(price_out_calls), 0)
        self.assertEqual(
            capture_run["price_out_calls"], len(price_out_calls)
        )
        for price_out_call in price_out_calls:
            price_out_frame = [
                row for row in price_out_rows
                if row.get("call") == price_out_call
            ]
            self.assertEqual(price_out_frame[0]["kind"], "BEGIN")
            self.assertEqual(price_out_frame[-1]["kind"], "END")
            candidates = [
                row for row in price_out_frame
                if row["kind"] == "CANDIDATE"
            ]
            decisions = [
                row for row in price_out_frame
                if row["kind"] == "DECISION"
            ]
            self.assertEqual(len(candidates), len(decisions))
            for candidate, decision in zip(candidates, decisions):
                self.assertEqual(
                    candidate["candidate"], decision["candidate"]
                )
                self.assertEqual(
                    candidate["reduced_cost"],
                    candidate["arc_cost"]
                    - candidate["tail_potential"]
                    + candidate["head_potential"],
                )

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

    def test_price_out_events_cover_decisions_and_remap_first(self):
        self.require_module()
        if shutil.which("cc") is None:
            self.skipTest("C compiler is unavailable")
        frozen = self.freeze_approved_source("price-out-frozen")
        prepared = generator.prepare_native_source(
            frozen=frozen,
            capture_enabled=True,
        )
        source_dir = Path(prepared["source_dir"])
        probe_source = self.root / "price_out_probe.c"
        probe_source.write_text(
            r'''#include "mcf_capture.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern long resize_prob(network_t *net);
extern void insert_new_arc(
    arc_t *new_arcs, long newpos, node_t *tail, node_t *head,
    cost_t cost, cost_t reduced_cost);
extern void replace_weaker_arc(
    network_t *net, arc_t *new_arcs, node_t *tail, node_t *head,
    cost_t cost, cost_t reduced_cost);

int main(int argc, char **argv)
{
    network_t net;
    char output[4096];
    FILE *stream;
    if (argc != 2)
        return 10;
    memset(&net, 0, sizeof(net));
    net.n = 1;
    net.m = 1;
    net.max_m = 4;
    net.max_new_m = 4;
    net.max_residual_new_m = 2;
    net.nodes = calloc(2, sizeof(node_t));
    net.arcs = calloc(4, sizeof(arc_t));
    net.dummy_arcs = calloc(1, sizeof(arc_t));
    if (!net.nodes || !net.arcs || !net.dummy_arcs)
        return 11;
    net.arcs[0].tail = &net.nodes[0];
    net.arcs[0].head = &net.nodes[1];
    net.stop_nodes = net.nodes + 2;
    net.stop_arcs = net.arcs + 1;
    net.nodes[1].pred = &net.nodes[0];
    net.nodes[0].potential = 25;
    if (mcf_capture_configure("fixture.in", argv[1], 1) ||
        mcf_capture_roi_begin(&net) ||
        mcf_capture_price_out_begin(&net))
        return 12;
    if (mcf_capture_price_out_candidate(
            &net.nodes[0], &net.nodes[1], 30, 5) ||
        mcf_capture_price_out_decision(
            0, NULL, &net.nodes[0], &net.nodes[1]))
        return 13;
    net.nodes[0].potential = 34;
    if (mcf_capture_price_out_candidate(
            &net.nodes[0], &net.nodes[1], 30, -4))
        return 14;
    insert_new_arc(
        &net.arcs[1], 0, &net.nodes[0], &net.nodes[1], 30, -4);
    if (mcf_capture_price_out_decision(
            1, &net.arcs[1], &net.nodes[0], &net.nodes[1]))
        return 15;
    net.arcs[2].flow = -10;
    net.arcs[3].flow = -10;
    net.nodes[0].potential = 0;
    net.nodes[1].potential = 36;
    if (mcf_capture_price_out_candidate(
            &net.nodes[1], &net.nodes[0], 30, -6))
        return 16;
    replace_weaker_arc(
        &net, &net.arcs[1], &net.nodes[1], &net.nodes[0], 30, -6);
    if (mcf_capture_price_out_decision(
            2, &net.arcs[1], &net.nodes[1], &net.nodes[0]))
        return 17;
    if (resize_prob(&net))
        return 18;
    net.nodes[1].potential = 38;
    if (mcf_capture_price_out_candidate(
            &net.nodes[1], &net.nodes[0], 30, -8))
        return 19;
    replace_weaker_arc(
        &net, &net.arcs[1], &net.nodes[1], &net.nodes[0], 30, -8);
    if (mcf_capture_price_out_decision(
            2, &net.arcs[1], &net.nodes[1], &net.nodes[0]))
        return 20;
    net.m = 2;
    net.stop_arcs = net.arcs + net.m;
    if (mcf_capture_price_out_end(&net, 1) ||
        mcf_capture_roi_end(&net))
        return 21;
    if (snprintf(output, sizeof(output), "%s/mcf.out", argv[1]) < 0)
        return 22;
    stream = fopen(output, "w");
    if (!stream || fputs("fixture\n", stream) == EOF || fclose(stream))
        return 23;
    if (mcf_capture_finish(output))
        return 24;
    free(net.arcs);
    free(net.nodes);
    free(net.dummy_arcs);
    return 0;
}
''',
            encoding="ascii",
        )
        probe = self.root / "price-out-probe"
        subprocess.run(
            [
                shutil.which("cc"),
                "-std=gnu11",
                "-Werror=implicit-function-declaration",
                "-I",
                str(source_dir),
                str(probe_source),
                str(source_dir / "implicit.c"),
                str(source_dir / "mcfutil.c"),
                str(source_dir / "mcf_capture.c"),
                "-o",
                str(probe),
            ],
            check=True,
        )
        output_root = self.root / "price-out-run"
        output_root.mkdir()
        subprocess.run([str(probe), str(output_root)], check=True)
        rows = [
            json.loads(line)
            for line in (output_root / "price_out.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(rows[0]["kind"], "BEGIN")
        self.assertEqual(rows[-1]["kind"], "END")
        self.assertEqual(
            [row["decision"] for row in rows if row["kind"] == "DECISION"],
            ["NO_CHANGE", "INSERT", "REPLACE", "REPLACE"],
        )
        for index, row in enumerate(rows):
            if row.get("kind") != "DECISION" or row["decision"] == "NO_CHANGE":
                continue
            candidate = row["candidate"]
            prior_candidate = max(
                prior for prior in range(index)
                if rows[prior].get("kind") == "CANDIDATE"
                and rows[prior]["candidate"] == candidate
            )
            self.assertTrue(
                any(
                    event.get("kind") == "ARC_STATE"
                    and event["candidate"] == candidate
                    for event in rows[prior_candidate + 1:index]
                )
            )
        remap = next(
            index for index, row in enumerate(rows)
            if row["kind"] == "ARENA_REMAP"
        )
        first_new_generation = next(
            index for index, row in enumerate(rows)
            if row.get("reference", {}).get("generation") == 1
        )
        self.assertLess(remap, first_new_generation)
        self.assertEqual(
            len([row for row in rows if row["kind"] == "ADJACENCY"]),
            2,
        )
        self.assertEqual(rows[-1]["new_arcs"], 1)
        self.assertEqual(rows[-1]["live_out_m"], 2)

    def test_capture_packages_are_deterministic_and_publish_atomically(self):
        self.require_module()
        self.assertTrue(
            hasattr(generator, "generate_candidate"),
            "generate_candidate is missing",
        )
        if shutil.which("cc") is None:
            self.skipTest("C compiler is unavailable")
        tiny_input = self.root / "deterministic.in"
        tiny_input.write_text(
            "2 2\n0 10\n2 12\n1 2 5\n2 1 7\n",
            encoding="ascii",
        )
        authority_frozen = self.freeze_approved_source("det-authority")
        authority_prepared = generator.prepare_native_source(
            frozen=authority_frozen,
            capture_enabled=False,
        )
        authority_binary = generator.build_native(
            prepared=authority_prepared,
            output=self.root / "det-authority-bin",
            compiler=shutil.which("cc"),
        )
        authority_root = self.root / "det-authority-run"
        generator.run_native(
            binary=authority_binary,
            input_path=tiny_input,
            output_root=authority_root,
        )

        capture_frozen = self.freeze_approved_source("det-capture")
        capture_prepared = generator.prepare_native_source(
            frozen=capture_frozen,
            capture_enabled=True,
        )
        capture_binary = generator.build_native(
            prepared=capture_prepared,
            output=self.root / "det-capture-bin",
            compiler=shutil.which("cc"),
        )
        primary_root = self.root / "det-primary-run"
        replay_root = self.root / "det-replay-run"
        generator.run_native(
            binary=capture_binary,
            input_path=tiny_input,
            output_root=primary_root,
        )
        generator.run_native(
            binary=capture_binary,
            input_path=tiny_input,
            output_root=replay_root,
        )
        identity = {
            "source_commit": capture_prepared["source_commit"],
            "source_tree_sha256": capture_prepared["source_tree_sha256"],
            "input_sha256": sha256(tiny_input),
            "common_patch_sha256": capture_prepared[
                "common_patch_sha256"
            ],
            "capture_patch_sha256": capture_prepared[
                "capture_patch_sha256"
            ],
            "compiler_sha256": sha256(Path(shutil.which("cc")).resolve()),
        }
        evidence_root = self.root / "accepted"
        accepted = generator.generate_candidate(
            authority_root=authority_root,
            capture_primary_root=primary_root,
            capture_replay_root=replay_root,
            identity=identity,
            evidence_root=evidence_root,
        )
        accepted_root = Path(accepted["accepted_root"])
        self.assertEqual(accepted_root.name, accepted["package_sha256"])
        self.assertEqual(
            (accepted_root / "mcf.reg2").read_bytes(),
            Path(accepted["replay_package"]).read_bytes(),
        )
        self.assertEqual(
            sha256(accepted_root / "mcf.reg2"),
            accepted["package_sha256"],
        )
        parsed_package = generator.mcfreg2.read_package(
            accepted_root / "mcf.reg2"
        )
        directory = {
            entry.section_type: entry for entry in parsed_package.directory
        }
        network_entry = directory[
            generator.mcfreg2.SECTION_TYPES["NETWORK"]
        ]
        nodes_entry = directory[generator.mcfreg2.SECTION_TYPES["NODES"]]
        arcs_entry = directory[generator.mcfreg2.SECTION_TYPES["ARCS"]]
        events_entry = directory[
            generator.mcfreg2.SECTION_TYPES["EVENTS"]
        ]
        self.assertEqual(
            (network_entry.element_count, network_entry.element_size),
            (generator.STATE_NETWORK_WORDS, 8),
        )
        self.assertEqual(
            (nodes_entry.element_count, nodes_entry.element_size),
            (parsed_package.header.nodes, generator.STATE_NODE_BYTES),
        )
        self.assertEqual(
            (arcs_entry.element_count, arcs_entry.element_size),
            (
                parsed_package.header.active_arcs
                + parsed_package.header.dummy_arcs,
                generator.STATE_ARC_BYTES,
            ),
        )
        self.assertEqual(
            events_entry.element_count, parsed_package.header.event_count
        )
        self.assertEqual(events_entry.element_size, 0)
        self.assertEqual(
            json.loads(
                (accepted_root / "validation.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "capture_replay_complete",
        )
        accepted_again = generator.generate_candidate(
            authority_root=authority_root,
            capture_primary_root=primary_root,
            capture_replay_root=replay_root,
            identity=identity,
            evidence_root=evidence_root,
        )
        self.assertEqual(accepted_again["accepted_root"], str(accepted_root))
        self.assertEqual(
            sha256(accepted_root / "mcf.reg2"),
            accepted["package_sha256"],
        )

        fault_replay = self.root / "fault-replay"
        shutil.copytree(replay_root, fault_replay)
        pricing = fault_replay / "pricing.jsonl"
        rows = [
            json.loads(line)
            for line in pricing.read_text(encoding="utf-8").splitlines()
        ]
        scan = next(row for row in rows if row["kind"] == "SCAN")
        scan["reduced_cost"] += 1
        pricing.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n" for row in rows
            ),
            encoding="utf-8",
        )
        failed_root = self.root / "failed-evidence"
        with self.assertRaisesRegex(
            generator.GenerationError, "capture_determinism"
        ):
            generator.generate_candidate(
                authority_root=authority_root,
                capture_primary_root=primary_root,
                capture_replay_root=fault_replay,
                identity=identity,
                evidence_root=failed_root,
            )
        failure = json.loads(
            (failed_root / "failed-input.json").read_text(encoding="utf-8")
        )
        self.assertEqual(failure["status"], "failed_input")
        self.assertEqual(
            failure["first_failed_gate"], "capture_determinism"
        )
        self.assertNotIn("accepted_package", failure)
        self.assertEqual(
            list(path for path in failed_root.iterdir()
                 if path.is_dir()),
            [],
        )

        short_root = self.root / "short-write-evidence"
        with mock.patch.object(
            generator.mcfreg2,
            "write_package",
            side_effect=OSError("short write"),
        ):
            with self.assertRaisesRegex(
                generator.GenerationError, "capture_determinism"
            ):
                generator.generate_candidate(
                    authority_root=authority_root,
                    capture_primary_root=primary_root,
                    capture_replay_root=replay_root,
                    identity=identity,
                    evidence_root=short_root,
                )
        self.assertEqual(
            json.loads(
                (short_root / "failed-input.json").read_text(
                    encoding="utf-8"
                )
            )["first_failed_gate"],
            "capture_determinism",
        )
        self.assertFalse(any(path.is_dir() for path in short_root.iterdir()))

    def test_publication_preflight_rejects_insufficient_disk(self):
        self.require_module()
        identity = {
            "source_commit": "1" * 40,
            "source_tree_sha256": "2" * 64,
            "input_sha256": "3" * 64,
            "common_patch_sha256": "4" * 64,
            "capture_patch_sha256": "5" * 64,
            "compiler_sha256": "6" * 64,
        }
        evidence_root = self.root / "no-space"
        usage = mock.Mock(free=0)
        with mock.patch.object(
            generator.shutil, "disk_usage", return_value=usage
        ):
            with self.assertRaisesRegex(
                generator.GenerationError, "insufficient disk"
            ):
                generator.generate_candidate(
                    authority_root=self.root / "authority",
                    capture_primary_root=self.root / "primary",
                    capture_replay_root=self.root / "replay",
                    identity=identity,
                    evidence_root=evidence_root,
                )
        failure = json.loads(
            (evidence_root / "failed-input.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(failure["first_failed_gate"], "preflight")
        self.assertFalse(any(path.is_dir() for path in evidence_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
