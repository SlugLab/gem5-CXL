# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import gzip
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

try:
    from scripts import freeze_cross_system_inputs as freezer
    from scripts import generate_mcfreg2_state as generator
except ImportError:
    freezer = None
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

    def test_fast_event_json_is_canonical_byte_exact(self):
        self.require_module()
        rows = (
            {
                "kind": "SCAN",
                "call": 17,
                "order": 19,
                "eligible": True,
                "selected": None,
                "cost": -42,
            },
            {"kind": "LABEL", "value": "non-ascii-\u00e9"},
        )
        for row in rows:
            self.assertEqual(
                generator._canonical_event_json(row),
                generator._canonical_json(row),
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

    def test_copied_file_is_checked_against_recorded_source_hash(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        original_copy2 = shutil.copy2

        def mutate_input_then_copy(source_path, target_path, *args, **kwargs):
            if Path(source_path).resolve() == input_path:
                input_path.write_text(
                    "2 1\nchanged after hash\n", encoding="ascii"
                )
            return original_copy2(source_path, target_path, *args, **kwargs)

        with mock.patch.object(
            generator.shutil, "copy2", side_effect=mutate_input_then_copy
        ):
            with self.assertRaisesRegex(
                generator.GenerationError, "frozen copy"
            ):
                self.freeze(source, commit, input_path)

    def test_all_native_runs_use_the_frozen_input_after_source_drift(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        frozen = self.freeze(source, commit, input_path)
        input_path.write_text("changed after freeze\n", encoding="ascii")

        with mock.patch.object(generator, "run_native", return_value={}) as run:
            result = generator.run_frozen_native_matrix(
                frozen=frozen,
                authority_binary=self.root / "authority-mcf",
                capture_binary=self.root / "capture-mcf",
                work_root=self.root / "runs",
            )

        copied_input = Path(frozen["copied_input"])
        self.assertEqual(len(run.call_args_list), 3)
        self.assertEqual(
            {call.kwargs["input_path"] for call in run.call_args_list},
            {copied_input},
        )
        self.assertEqual(result["input"], str(copied_input))

    def test_evidence_identity_is_derived_from_consistent_build_records(self):
        self.require_module()
        hashes = {name: format(index, "064x") for index, name in enumerate((
            "source_tree_sha256",
            "input_sha256",
            "common_patch_sha256",
            "capture_patch_sha256",
            "capture_runtime_sha256",
            "wire_abi_sha256",
            "compiler_sha256",
            "authority_command_sha256",
            "capture_command_sha256",
            "authority_binary_sha256",
            "capture_binary_sha256",
            "generator_sha256",
            "python_reader_sha256",
            "cpp_reader_sha256",
            "cpp_kernel_sha256",
        ), start=1)}
        source = {
            "source_commit": "a" * 40,
            "source_tree_sha256": hashes["source_tree_sha256"],
            "input_sha256": hashes["input_sha256"],
        }
        shared = {
            "schema": 2,
            "source_tree_sha256": hashes["source_tree_sha256"],
            "common_patch_sha256": hashes["common_patch_sha256"],
            "capture_runtime_sha256": hashes["capture_runtime_sha256"],
            "wire_abi_sha256": hashes["wire_abi_sha256"],
            "compiler": {
                "sha256": hashes["compiler_sha256"],
                "version": "cc fixture 1.0",
                "target": "x86_64-fixture-linux-gnu",
            },
            "generator_sha256": hashes["generator_sha256"],
            "python_reader_sha256": hashes["python_reader_sha256"],
            "cpp_reader_sha256": hashes["cpp_reader_sha256"],
            "cpp_kernel_sha256": hashes["cpp_kernel_sha256"],
        }
        authority = {
            **shared,
            "capture_enabled": False,
            "capture_patch_sha256": None,
            "canonical_command": ["<COMPILER>", "-O2", "<OUTPUT>"],
            "binary_sha256": hashes["authority_binary_sha256"],
        }
        capture = {
            **shared,
            "capture_enabled": True,
            "capture_patch_sha256": hashes["capture_patch_sha256"],
            "canonical_command": [
                "<COMPILER>", "-O2", "-DMCF_CAPTURE_EVENTS=1", "<OUTPUT>"
            ],
            "binary_sha256": hashes["capture_binary_sha256"],
        }
        authority["command_sha256"] = generator._canonical_value_sha256(
            authority["canonical_command"]
        )
        capture["command_sha256"] = generator._canonical_value_sha256(
            capture["canonical_command"]
        )

        identity = generator.derive_evidence_identity(
            source, authority, capture
        )

        self.assertEqual(set(identity), generator.EVIDENCE_IDENTITY_FIELDS)
        self.assertEqual(
            identity["authority_binary_sha256"],
            hashes["authority_binary_sha256"],
        )
        inconsistent = dict(capture, wire_abi_sha256="f" * 64)
        with self.assertRaisesRegex(
            generator.GenerationError, "wire_abi_sha256"
        ):
            generator.derive_evidence_identity(
                source, authority, inconsistent
            )

    def test_preflight_reports_bound_source_lp64_and_capacity(self):
        self.require_module()
        source, commit, input_path = self.make_source_repo()
        result = generator.preflight(
            source_root=source,
            source_commit=commit,
            source_subdir="bench/mcf",
            input_path=input_path,
            input_sha256=sha256(input_path),
            output_root=self.root / "preflight-output",
            compiler=shutil.which("cc"),
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source_commit"], commit)
        self.assertEqual(result["tracked_file_count"], 3)
        self.assertEqual(result["input_sha256"], sha256(input_path))
        self.assertTrue(result["lp64"])
        self.assertGreater(result["available_disk_bytes"], 0)

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
                "-lz",
                "-lcrypto",
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

        journal_path = capture_root / "pricing.jsonl.gz"
        self.assertTrue(journal_path.is_file())
        with gzip.open(journal_path, "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        calls = {}
        for row in rows:
            calls.setdefault(row["call"], []).append(row)
        self.assertGreater(len(calls), 1)
        self.assertEqual(capture_run["pricing_calls"], len(calls))
        with gzip.open(
            capture_root / "price_out.jsonl.gz", "rt", encoding="utf-8"
        ) as stream:
            price_out_rows = [json.loads(line) for line in stream]
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
            self.assertEqual(price_out_frame[0]["kind"], "CALL_BEGIN")
            self.assertEqual(price_out_frame[-1]["kind"], "CALL_END")
            self.assertEqual(
                price_out_frame[1]["kind"], "PRICE_OUT_STATE_LIVE_IN"
            )
            self.assertEqual(
                price_out_frame[-2]["kind"], "PRICE_OUT_END_OBSERVED"
            )
            candidates = [
                row for row in price_out_frame
                if row["kind"] == "PRICE_OUT_CANDIDATE_OBSERVED"
            ]
            decisions = [
                row for row in price_out_frame
                if row["kind"] == "PRICE_OUT_DECISION_OBSERVED"
            ]
            self.assertEqual(len(candidates), len(decisions))
            for candidate, decision in zip(candidates, decisions):
                self.assertEqual(
                    candidate["candidate"], decision["candidate"]
                )
                self.assertIn(decision["decision"], {
                    "NO_CHANGE", "INSERT", "REPLACE"
                })

        previous_live_out = None
        for call_id, frame in sorted(calls.items()):
            self.assertEqual(frame[0]["kind"], "CALL_BEGIN")
            self.assertEqual(frame[-1]["kind"], "CALL_END")
            ending = next(
                row for row in frame
                if row["kind"] == "PRICING_END_OBSERVED"
            )
            if call_id == 0:
                self.assertTrue(frame[0]["initialize"])
            scans = [
                row for row in frame
                if row["kind"] == "PRICING_SCAN_LIVE_IN"
            ]
            candidates = [
                row for row in frame
                if row["kind"] == "PRICING_CANDIDATE_OBSERVED"
            ]
            live_in = [
                row for row in frame
                if row["kind"] == "BASKET_LIVE_IN"
            ]
            live_out = [
                row for row in frame
                if row["kind"] == "BASKET_LIVE_OUT_OBSERVED"
            ]
            self.assertEqual(ending["arcs_priced"], len(scans))
            self.assertEqual(len(scans), len(candidates))
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
                if group_pos == ending["group_pos"]:
                    break
            self.assertEqual(
                [scan["arc"]["index"] for scan in scans], expected_arc_ids
            )
            for scan, candidate in zip(scans, candidates):
                self.assertEqual(
                    scan["scan_position"], candidate["scan_position"]
                )
                self.assertEqual(
                    candidate["reduced_cost"],
                    scan["cost"]
                    - scan["tail_potential"]
                    + scan["head_potential"],
                )
                expected_candidate = (
                    candidate["reduced_cost"] < 0 and scan["ident"] == 1
                ) or (
                    candidate["reduced_cost"] > 0 and scan["ident"] == 2
                )
                self.assertEqual(candidate["candidate"], expected_candidate)
                self.assertEqual(
                    scan["group_pos"], scan["arc"]["index"]
                    % frame[0]["nr_group"],
                )
                if candidate["basket_slot"] >= 0:
                    self.assertTrue(candidate["candidate"])
                    self.assertGreater(candidate["basket_slot"], 0)
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
                    ending["selected_arc"], live_out[0]["arc"]
                )
                self.assertEqual(
                    ending["selected_reduced_cost"], live_out[0]["cost"]
                )
            else:
                self.assertEqual(ending["selected_arc"]["kind"], "null")
                self.assertEqual(ending["selected_reduced_cost"], 0)
            if previous_live_out is not None:
                retained_candidates = {
                    row["arc"]["index"] for row in previous_live_out[1:]
                }
                self.assertTrue(
                    {row["arc"]["index"] for row in live_in}.issubset(
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

    def _capture_price_out_probe(self):
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
    net.arcs[1].flow = 0;
    net.arcs[1].ident = AT_LOWER;
    net.arcs[1].nextout = &net.arcs[0];
    net.arcs[1].nextin = &net.arcs[0];
    net.nodes[1].firstout = &net.arcs[1];
    net.nodes[0].firstin = &net.arcs[1];
    net.m = 2;
    net.m_impl += 1;
    net.max_residual_new_m -= 1;
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
                "-lz",
                "-lcrypto",
            ],
            check=True,
        )
        output_root = self.root / "price-out-run"
        output_root.mkdir()
        subprocess.run([str(probe), str(output_root)], check=True)
        with gzip.open(
            output_root / "price_out.jsonl.gz", "rt", encoding="utf-8"
        ) as stream:
            rows = [json.loads(line) for line in stream]
        return rows

    def test_arc_final_is_recorded_after_flow_ident_and_links(self):
        rows = self._capture_price_out_probe()
        self.assertEqual(rows[0]["kind"], "CALL_BEGIN")
        self.assertEqual(rows[-1]["kind"], "CALL_END")
        self.assertEqual(
            [
                row["decision"] for row in rows
                if row["kind"] == "PRICE_OUT_DECISION_OBSERVED"
            ],
            ["NO_CHANGE", "INSERT", "REPLACE", "REPLACE"],
        )
        final = next(
            row for row in rows
            if row["kind"] == "ARC_FINAL_OBSERVED"
            and row["reference"]["index"] == 1
        )
        self.assertEqual(final["flow"], 0)
        self.assertEqual(final["ident"], 1)
        self.assertEqual(final["nextout"]["kind"], "arc")
        self.assertEqual(final["nextin"]["kind"], "arc")
        adjacency = [
            row for row in rows
            if row["kind"] == "ADJACENCY_FINAL_OBSERVED"
        ]
        self.assertEqual(len(adjacency), 2)
        ending = next(
            row for row in rows
            if row["kind"] == "PRICE_OUT_END_OBSERVED"
        )
        live_in = next(
            row for row in rows
            if row["kind"] == "PRICE_OUT_STATE_LIVE_IN"
        )
        self.assertEqual(ending["network_words"][3], 2)
        self.assertEqual(ending["network_words"][5], 1)
        self.assertEqual(
            ending["network_words"][6],
            live_in["network_words"][6] + live_in["network_words"][7] - 1,
        )
        self.assertEqual(ending["network_words"][22], 2)

    def test_remap_contains_every_live_arc_reference(self):
        rows = self._capture_price_out_probe()
        live_in = next(
            row for row in rows
            if row["kind"] == "PRICE_OUT_STATE_LIVE_IN"
        )
        remaps = [row for row in rows if row["kind"] == "REMAP_OBSERVED"]
        self.assertEqual(len(remaps), live_in["network_words"][3])
        self.assertEqual(
            [row["old_reference"]["index"] for row in remaps],
            list(range(live_in["network_words"][3])),
        )
        self.assertTrue(all(
            row["new_reference"]["generation"]
            == row["old_reference"]["generation"] + 1
            for row in remaps
        ))

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
        source_value = {
            "schema": 1,
            "source_commit": capture_prepared["source_commit"],
            "source_tree_sha256": capture_prepared["source_tree_sha256"],
            "input_sha256": sha256(tiny_input),
        }
        identity = generator.derive_evidence_identity(
            source_value, authority_binary, capture_binary
        )
        source_record = self.root / "source-record.json"
        source_record.write_bytes(generator._canonical_json(source_value))
        evidence_root = self.root / "accepted"
        accepted = generator.generate_candidate(
            authority_root=authority_root,
            capture_primary_root=primary_root,
            capture_replay_root=replay_root,
            identity=identity,
            evidence_root=evidence_root,
            source_record=source_record,
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
        streamed_names = (
            "BASKET",
            "CALL_INDEX",
            "EVENTS",
            "DELTAS",
            "BOUNDARIES",
        )
        events_entry = directory[generator.mcfreg2.SECTION_TYPES["EVENTS"]]
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
        for name in streamed_names:
            entry = directory[generator.mcfreg2.SECTION_TYPES[name]]
            self.assertEqual(entry.schema, 3)
            self.assertEqual(parsed_package.section(name)[:2], b"\x1f\x8b")
        frames = generator.mcfreg2.validate_semantic_roles(parsed_package)
        self.assertEqual(len(frames), parsed_package.header.pricing_calls
                         + parsed_package.header.price_out_calls)
        delta_rows = [
            json.loads(line)
            for line in gzip.decompress(
                parsed_package.section("DELTAS")
            ).splitlines()
        ]
        allocations = [
            row for row in delta_rows if row["kind"] == "ALLOC"
        ]
        primary_run = json.loads(
            (primary_root / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            len(allocations), primary_run["allocation_events"]
        )
        self.assertTrue(all(
            row["requested_bytes"]
            == row["elements"] * row["element_bytes"]
            for row in allocations
        ))
        validation = json.loads(
            (accepted_root / "validation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(validation["schema"], 3)
        self.assertEqual(
            validation["status"], "capture_contract_validated"
        )
        self.assertFalse(validation["independent_replay_complete"])
        self.assertEqual(validation["identity"], identity)
        self.assertEqual(validation["package_sha256"], accepted["package_sha256"])
        self.assertEqual(
            validation["primary_package_sha256"],
            validation["replay_package_sha256"],
        )
        self.assertEqual(
            validation["authority_final_state_sha256"],
            validation["capture_primary_final_state_sha256"],
        )
        self.assertEqual(
            validation["authority_mcf_output_sha256"],
            validation["capture_replay_mcf_output_sha256"],
        )
        self.assertEqual(
            validation["peak_allocated_bytes"],
            json.loads(
                (primary_root / "run.json").read_text(encoding="utf-8")
            )["peak_allocated_bytes"],
        )
        self.assertTrue((
            accepted_root / "capture-validation/mcfreg2-capture.json"
        ).is_file())
        self.assertTrue((accepted_root / "authority/run.json").is_file())
        self.assertTrue((accepted_root / "capture-primary/run.json").is_file())
        self.assertTrue((accepted_root / "capture-replay/run.json").is_file())
        candidate = json.loads(
            (accepted_root / "candidate-record.json").read_text(
                encoding="utf-8"
            )
        )
        with mock.patch.dict(
            freezer.MINIMUM_ALLOCATED_BYTES, {"mcf": 1}
        ):
            with self.assertRaisesRegex(
                freezer.InputError, "validation schema must be 2"
            ):
                freezer.validate_mcf_record(candidate["record"])
            with self.assertRaises(generator.GenerationError):
                generator.verify_accepted(evidence_root)
        accepted_again = generator.generate_candidate(
            authority_root=authority_root,
            capture_primary_root=primary_root,
            capture_replay_root=replay_root,
            identity=identity,
            evidence_root=evidence_root,
            source_record=source_record,
        )
        self.assertEqual(accepted_again["accepted_root"], str(accepted_root))
        self.assertEqual(
            sha256(accepted_root / "mcf.reg2"),
            accepted["package_sha256"],
        )

        fault_replay = self.root / "fault-replay"
        shutil.copytree(replay_root, fault_replay)
        pricing = fault_replay / "pricing.jsonl.gz"
        with gzip.open(pricing, "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        scan = next(
            row for row in rows
            if row["kind"] == "PRICING_CANDIDATE_OBSERVED"
        )
        scan["reduced_cost"] += 1
        with pricing.open("wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, mtime=0
            ) as stream:
                stream.write("".join(
                    json.dumps(row, sort_keys=True) + "\n" for row in rows
                ).encode("utf-8"))
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
            name: "6" * 64 for name in generator.EVIDENCE_IDENTITY_FIELDS
        }
        identity["source_commit"] = "1" * 40
        identity["compiler_version"] = "cc fixture 1.0"
        identity["compiler_target"] = "x86_64-fixture-linux-gnu"
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
