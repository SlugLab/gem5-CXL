# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
BUILDER_PATH = REPO / "scripts" / "build_gapbs_amu_cxlmemuring.py"
BASELINE_BUILDER_PATH = REPO / "scripts" / "build_gapbs_baseline_cxlmemuring.py"
CIRA_BUILDER_PATH = REPO / "scripts" / "build_gapbs_cira_cxlmemuring.py"
RUNNER_PATH = REPO / "scripts" / "compare_gapbs_cxl_amu_cira.py"
CONFIG_PATH = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)
ROI_STATE_PATH = (
    REPO / "configs" / "example" / "gem5_library" / "gapbs_roi_state.py"
)
MAIN_REPO = REPO.parent.parent if REPO.parent.name == ".worktrees" else REPO
CXLMEMURING = MAIN_REPO.parent / "CXLMemUring"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GapbsAmuBuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_module("gapbs_amu_builder", BUILDER_PATH)
        cls.baseline_builder = load_module(
            "gapbs_baseline_builder", BASELINE_BUILDER_PATH
        )
        cls.cira_builder = load_module("gapbs_cira_builder", CIRA_BUILDER_PATH)
        cls.runner = load_module("gapbs_amu_runner", RUNNER_PATH)
        cls.roi_state = load_module("gapbs_roi_state", ROI_STATE_PATH)

    def make_roi_state(self, **kwargs):
        return self.roi_state, self.roi_state.GapbsRoiState(**kwargs)

    def test_load_window_issues_before_waiting(self):
        header = self.builder.AMU_HEADER
        self.assertIn("class LoadWindow", header)
        issue = header.index("void issue_all()")
        wait = header.index("void wait_all()")
        self.assertLess(issue, wait)
        issue_body = header[issue:wait]
        self.assertIn("amu_aload", issue_body)
        self.assertNotIn("amu_getfin", issue_body)

    def test_load_values_uses_the_window(self):
        header = self.builder.AMU_HEADER
        wrapper = header[header.index("static inline void load_values") :]
        self.assertIn("LoadWindow window", wrapper)
        self.assertIn("window.issue_all()", wrapper)
        self.assertIn("window.wait_all()", wrapper)

    def test_window_handles_empty_and_bounded_batches(self):
        header = self.builder.AMU_HEADER
        self.assertIn("if (count_ == 0)", header)
        self.assertIn("assert(count_ < GAPBS_AMU_WINDOW_SIZE)", header)
        self.assertIn("template <typename T>\n  size_t add", header)
        self.assertIn("template <typename T>\n  T value", header)

    def test_window_invalidates_spm_before_issue(self):
        header = self.builder.AMU_HEADER
        self.assertIn("invalidate_spm_slot(spm_[i])", header)
        invalidate = header.index("invalidate_spm_slot(spm_[i])")
        issue = header.index("amu_aload(spm_[i]", invalidate)
        completion = header.index("uint64_t id = amu_getfin()", issue)
        copy_value = header.index("memcpy(values_[i], spm_[i]", completion)
        self.assertLess(invalidate, issue)
        self.assertLess(issue, completion)
        self.assertLess(completion, copy_value)
        self.assertNotIn("memset(spm_, 0", header)

    def test_window_flushes_each_source_before_issue(self):
        header = self.builder.AMU_HEADER
        self.assertIn("flush_source_lines(addrs_[i], sizes_[i])", header)
        flush = header.index("flush_source_lines(addrs_[i], sizes_[i])")
        issue = header.index("amu_aload(spm_[i]", flush)
        self.assertLess(flush, issue)
        self.assertNotIn("flushed_lines", header)
        issue_body = header[header.index("void issue_all()"):header.index("void wait_all()")]
        self.assertEqual(issue_body.count("mfence"), 1)

    def transform_source(self, benchmark, patch_function):
        source = CXLMEMURING / "bench" / "gapbs" / "src" / f"{benchmark}.cc"
        self.assertTrue(source.exists(), source)
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "src").mkdir()
            target = src_dir / "src" / source.name
            shutil.copy2(source, target)
            patch_function(src_dir)
            return target.read_text(encoding="utf-8")

    def test_bc_batches_reverse_values(self):
        transformed = self.transform_source("bc", self.builder.patch_bc)
        self.assertIn("LoadWindow value_window", transformed)
        self.assertLess(
            transformed.index("value_window.issue_all()"),
            transformed.index("value_window.wait_all()"),
        )
        self.assertNotIn("load_value(&path_counts[v])", transformed)
        self.assertNotIn("load_value(&deltas[v])", transformed)
        self.assertIn("value_window.value<CountT>", transformed)
        self.assertIn("value_window.value<ScoreT>", transformed)

    def test_sssp_batches_initial_distances(self):
        transformed = self.transform_source("sssp", self.builder.patch_sssp)
        self.assertIn("LoadWindow distance_window", transformed)
        self.assertIn(
            "distance_window.add(&dist[edges[amu_i].v])", transformed
        )
        self.assertIn("distance_window.issue_all()", transformed)
        self.assertIn("distance_window.wait_all()", transformed)
        self.assertIn("distance_window.value<WeightT>(amu_i)", transformed)
        self.assertEqual(transformed.count("load_value(&dist[wn.v])"), 1)
        retry_load = transformed.index("load_value(&dist[wn.v])")
        compare_exchange = transformed.index("compare_and_swap")
        self.assertGreater(retry_load, compare_exchange)

    def test_pr_preserves_two_stage_ordered_accumulation(self):
        transformed = self.transform_source(
            "pr", lambda src_dir: self.builder.patch_pr_like(src_dir, "pr.cc")
        )
        node_wait = transformed.index(
            "load_values(node_addrs, nodes, amu_count)"
        )
        form_score_addresses = transformed.index(
            "score_addrs[amu_i] = &outgoing_contrib[nodes[amu_i]]"
        )
        score_wait = transformed.index(
            "load_values(score_addrs, scores_batch, amu_count)"
        )
        accumulation = transformed.index(
            "incoming_total += scores_batch[amu_i]"
        )
        self.assertLess(node_wait, form_score_addresses)
        self.assertLess(form_score_addresses, score_wait)
        self.assertLess(score_wait, accumulation)

    def test_bfs_preserves_ordered_commit_and_early_exit(self):
        transformed = self.transform_source("bfs", self.builder.patch_bfs)
        bottom_loop = transformed.index(
            "for (size_t amu_i = 0; amu_i < amu_count; ++amu_i)"
        )
        parent_commit = transformed.index("parent[u] = v", bottom_loop)
        found_commit = transformed.index("found = true", parent_commit)
        self.assertLess(parent_commit, found_commit)
        top_down = transformed.index("compare_and_swap(parent[v]", found_commit)
        ordered_loop = transformed.rfind(
            "for (size_t amu_i = 0; amu_i < amu_count; ++amu_i)",
            found_commit,
            top_down,
        )
        self.assertNotEqual(ordered_loop, -1)

    def test_parse_verification_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "gem5.log"
            log.write_text("Trial Time: 1.0\nVerification: PASS\n")
            self.assertEqual(self.runner.parse_verification(log), "pass")
            log.write_text("Verification: FAIL\n")
            self.assertEqual(self.runner.parse_verification(log), "fail")
            log.write_text("Trial Time: 1.0\n")
            self.assertEqual(self.runner.parse_verification(log), "missing")

    def test_config_can_continue_after_roi_for_verification(self):
        config = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--continue-after-roi"', config)
        self.assertIn("and not args.continue_after_roi", config)
        self.assertIn("if args.fast_forward_cpu:\n            yield False", config)

    def test_second_trial_roi_switches_once_and_measures_only_trial_one(self):
        _, roi = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        self.assertEqual(roi.work_begin(), ("switch",))
        self.assertEqual(roi.work_end(), ())
        self.assertEqual(roi.work_begin(), ("reset", "record_start_tick"))
        self.assertEqual(roi.work_end(), ("dump",))
        self.assertEqual(roi.finish(), ("verify",))
        self.assertEqual(roi.switch_count, 1)
        self.assertEqual(roi.reset_count, 1)
        self.assertEqual(roi.dump_count, 1)

    def test_roi_state_rejects_missing_measured_trial_or_end(self):
        roi_state, missing_trial = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        missing_trial.work_begin()
        missing_trial.work_end()
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "missing trial 1 begin"
        ):
            missing_trial.finish()

        _, missing_end = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        missing_end.work_begin()
        missing_end.work_end()
        missing_end.work_begin()
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "missing trial 1 end"
        ):
            missing_end.finish()

    def test_roi_state_requires_every_configured_trial_to_finish(self):
        roi_state, roi = self.make_roi_state(
            iterations=2,
            measure_trial=0,
            switch_at_trial_zero=False,
        )
        roi.work_begin()
        roi.work_end()
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "missing trial 1 begin"
        ):
            roi.finish()

    def test_roi_state_rejects_third_trial_and_malformed_order(self):
        roi_state, third_trial = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        third_trial.work_begin()
        third_trial.work_end()
        third_trial.work_begin()
        third_trial.work_end()
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "unexpected trial 2 begin"
        ):
            third_trial.work_begin()

        _, end_first = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "end without begin"
        ):
            end_first.work_end()

        _, duplicate_begin = self.make_roi_state(
            iterations=2,
            measure_trial=1,
            switch_at_trial_zero=True,
        )
        duplicate_begin.work_begin()
        with self.assertRaisesRegex(
            roi_state.RoiSequenceError, "begin before trial 0 end"
        ):
            duplicate_begin.work_begin()

    def test_roi_state_rejects_measure_trial_outside_iterations(self):
        with self.assertRaisesRegex(
            ValueError, "measure_trial must be less than iterations"
        ):
            self.roi_state.GapbsRoiState(
                iterations=1,
                measure_trial=1,
                switch_at_trial_zero=False,
            )

    def test_fast_forward_exit_cause_is_always_classified(self):
        self.assertTrue(
            hasattr(self.roi_state, "classify_final_exit"),
            "missing final-exit classifier",
        )
        self.assertEqual(
            self.roi_state.classify_final_exit(
                "m5_fail instruction encountered"
            ),
            ("fail", 2),
        )
        self.assertEqual(
            self.roi_state.classify_final_exit(
                "exiting with last active thread context"
            ),
            ("pass", 0),
        )
        self.assertEqual(
            self.roi_state.classify_final_exit("simulate() limit reached"),
            ("missing", 3),
        )
        config = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "if args.fast_forward_cpu or args.continue_after_roi:", config
        )

    def test_non_gapbs_arguments_preserve_legacy_config_reuse(self):
        self.assertTrue(
            hasattr(self.roi_state, "resolve_workload_shape"),
            "missing workload-shape resolver",
        )
        arguments = [
            "--mode",
            "chase",
            "--nodes",
            "32768",
            "--accesses",
            "1024",
            "--streams",
            "1",
        ]
        shape = self.roi_state.resolve_workload_shape(
            arguments=arguments,
            configured_scale=None,
            configured_iterations=None,
            fast_forward=False,
        )
        self.assertEqual(shape, (None, 1))
        self.assertEqual(arguments[0:2], ["--mode", "chase"])

    def test_legacy_gapbs_arguments_infer_scale_and_iterations(self):
        shape = self.roi_state.resolve_workload_shape(
            arguments=["-g", "4", "-n", "2"],
            configured_scale=None,
            configured_iterations=None,
            fast_forward=False,
        )
        self.assertEqual(shape, (4, 2))

    def test_fast_forward_workload_shape_requires_matching_g20_n2(self):
        self.assertTrue(
            hasattr(self.roi_state, "resolve_workload_shape"),
            "missing workload-shape resolver",
        )
        self.assertEqual(
            self.roi_state.resolve_workload_shape(
                arguments=["-g", "20", "-n", "2", "-v"],
                configured_scale=20,
                configured_iterations=2,
                fast_forward=True,
            ),
            (20, 2),
        )
        for arguments, scale, iterations in (
            (["-g", "4", "-n", "2"], 4, 2),
            (["-g", "20", "-n", "1"], 20, 1),
            (["--mode", "chase"], 20, 2),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(
                    ValueError, "fast-forward requires matching -g 20 -n 2"
                ):
                    self.roi_state.resolve_workload_shape(
                        arguments=arguments,
                        configured_scale=scale,
                        configured_iterations=iterations,
                        fast_forward=True,
                    )

    def test_verify_dry_run_builds_atomic_to_timing_second_trial_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "baseline"
            bin_dir.mkdir()
            (bin_dir / "bc").touch()
            outdir = root / "out"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    "--baseline-bin-dir",
                    str(bin_dir),
                    "--benchmarks",
                    "bc",
                    "--scale",
                    "20",
                    "--iterations",
                    "2",
                    "--fast-forward-cpu",
                    "atomic",
                    "--cpu",
                    "timing",
                    "--measure-trial",
                    "1",
                    "--roi-work-events",
                    "--verify",
                    "--dry-run",
                    "--outdir",
                    str(outdir),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--fast-forward-cpu atomic", proc.stdout)
        self.assertIn("--cpu timing", proc.stdout)
        self.assertIn("--scale 20", proc.stdout)
        self.assertIn("--iterations 2", proc.stdout)
        self.assertIn("--measure-trial 1", proc.stdout)
        self.assertIn("--arguments -g 20 -n 2 -v", proc.stdout)
        self.assertIn("--cxl-memory", proc.stdout)
        self.assertNotIn("CIRA_GAPBS_DEVICE_OFFLOAD", proc.stdout)

    def test_runner_rejects_invalid_fast_forward_and_trial_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "baseline"
            bin_dir.mkdir()
            (bin_dir / "bc").touch()
            common = [
                sys.executable,
                str(RUNNER_PATH),
                "--baseline-bin-dir",
                str(bin_dir),
                "--benchmarks",
                "bc",
                "--dry-run",
                "--outdir",
                str(root / "out"),
            ]
            cases = (
                (
                    ["--fast-forward-cpu", "atomic"],
                    "requires --roi-work-events",
                ),
                (
                    [
                        "--fast-forward-cpu",
                        "atomic",
                        "--roi-work-events",
                        "--cpu",
                        "o3",
                    ],
                    "requires --cpu timing",
                ),
                (
                    [
                        "--fast-forward-cpu",
                        "atomic",
                        "--roi-work-events",
                        "--cpu",
                        "timing",
                    ],
                    "requires --iterations 2 and --measure-trial 1",
                ),
                (
                    [
                        "--fast-forward-cpu",
                        "atomic",
                        "--roi-work-events",
                        "--cpu",
                        "timing",
                        "--scale",
                        "4",
                        "--iterations",
                        "2",
                        "--measure-trial",
                        "1",
                    ],
                    "requires --scale 20",
                ),
                (
                    ["--iterations", "1", "--measure-trial", "1"],
                    "measure-trial must be less than iterations",
                ),
            )
            for options, expected in cases:
                with self.subTest(options=options):
                    proc = subprocess.run(
                        [*common, *options],
                        text=True,
                        capture_output=True,
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(expected, proc.stdout + proc.stderr)

    def test_parse_stats_uses_first_roi_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = Path(tmp) / "stats.txt"
            stats.write_text(
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 100\n"
                "---------- End Simulation Statistics   ----------\n"
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 250\n"
                "---------- End Simulation Statistics   ----------\n"
            )
            self.assertEqual(self.runner.parse_stats(stats)["simTicks"], 100)

    def test_roi_marker_patch_turns_verifier_failure_into_m5_fail(self):
        source = CXLMEMURING / "bench" / "gapbs" / "src" / "benchmark.h"
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "src").mkdir()
            target = src_dir / "src" / "benchmark.h"
            shutil.copy2(source, target)
            self.builder.patch_benchmark_roi_markers(src_dir)
            transformed = target.read_text(encoding="utf-8")
        self.assertIn("bool verification_passed", transformed)
        self.assertIn("m5_fail(0, 1)", transformed)

    def test_baseline_roi_patch_turns_verifier_failure_into_m5_fail(self):
        source = CXLMEMURING / "bench" / "gapbs" / "src" / "benchmark.h"
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "src").mkdir()
            target = src_dir / "src" / "benchmark.h"
            shutil.copy2(source, target)
            self.baseline_builder.patch_benchmark_roi_markers(src_dir)
            transformed = target.read_text(encoding="utf-8")
        self.assertIn("bool verification_passed", transformed)
        self.assertIn("m5_fail(0, 1)", transformed)

    def test_cira_roi_patch_turns_verifier_failure_into_m5_fail(self):
        source = CXLMEMURING / "bench" / "gapbs" / "src" / "benchmark.h"
        with tempfile.TemporaryDirectory() as tmp:
            src_dir = Path(tmp)
            (src_dir / "src").mkdir()
            target = src_dir / "src" / "benchmark.h"
            shutil.copy2(source, target)
            self.cira_builder.patch_benchmark_roi_markers(src_dir)
            transformed = target.read_text(encoding="utf-8")
        self.assertIn("bool verification_passed", transformed)
        self.assertIn("m5_fail(0, 1)", transformed)

    def test_builders_hash_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact"
            artifact.write_bytes(b"gapbs provenance\x00\xff")
            expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(self.builder.sha256_file(artifact), expected)
            self.assertEqual(
                self.baseline_builder.sha256_file(artifact), expected
            )
            self.assertEqual(self.cira_builder.sha256_file(artifact), expected)

    def test_builders_report_nested_repo_commit_and_dirty_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test User"],
                check=True,
            )
            tracked = repo / "tracked.cc"
            tracked.write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "tracked.cc"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True
            )
            expected_commit = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            for builder in (
                self.builder, self.baseline_builder, self.cira_builder
            ):
                self.assertEqual(
                    builder.git_repository_state(repo),
                    {"commit": expected_commit, "dirty": False},
                )
            tracked.write_text("dirty\n", encoding="utf-8")
            for builder in (
                self.builder, self.baseline_builder, self.cira_builder
            ):
                self.assertTrue(builder.git_repository_state(repo)["dirty"])

    def test_manifest_sources_include_required_provenance_fields(self):
        baseline_source = BASELINE_BUILDER_PATH.read_text(encoding="utf-8")
        amu_source = BUILDER_PATH.read_text(encoding="utf-8")
        cira_source = CIRA_BUILDER_PATH.read_text(encoding="utf-8")
        common_fields = (
            '"gapbs_commit"',
            '"gapbs_dirty"',
            '"benchmark_source_sha256"',
            '"binary_sha256"',
        )
        for field in common_fields:
            self.assertIn(field, baseline_source)
            self.assertIn(field, amu_source)
            self.assertIn(field, cira_source)
        self.assertIn('"profile_sha256"', amu_source)
        self.assertIn('"profile_sha256"', cira_source)

        complete_fields = (
            '"compiler_input_sha256"',
            '"builder_script_sha256"',
            '"m5_library_sha256"',
            '"gem5_include_sha256"',
            '"instrumentation_include_sha256"',
        )
        for field in complete_fields:
            for source in (baseline_source, amu_source, cira_source):
                self.assertIn(field, source)

    def test_builders_hash_complete_cc_and_header_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "a.cc").write_text("a\n", encoding="utf-8")
            (root / "nested" / "b.h").write_text("b\n", encoding="utf-8")
            (root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
            expected = {"a.cc", "nested/b.h"}
            for builder in (
                self.builder, self.baseline_builder, self.cira_builder
            ):
                self.assertEqual(
                    set(builder.sha256_tree(root, (".cc", ".h"))), expected
                )

    def test_cira_pgo_profile_resolution_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "Missing usable CIRA PGO"):
                self.cira_builder.resolve_profile(Path(tmp), "bfs", 0)

    def test_cira_override_does_not_claim_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profile_results" / "gapbs" / "bfs"
            profile_dir.mkdir(parents=True)
            (profile_dir / "bfs_twopass_profile.json").write_text(
                '{"regions": [{"optimal_prefetch_depth": 99}]}',
                encoding="utf-8",
            )
            distance, profiles, override = self.cira_builder.resolve_profile(
                Path(tmp), "bfs", 7
            )
            self.assertEqual((distance, profiles, override), (7, [], 7))
            self.assertIn(
                '"profile_mode": "override-non-pgo"',
                CIRA_BUILDER_PATH.read_text(encoding="utf-8"),
            )

    def test_cira_future_row_helper_uses_bounded_profile_distance(self):
        header = self.cira_builder.CIRA_HEADER
        self.assertIn("static inline bool future_node", header)
        self.assertIn("GAPBS_CIRA_NODE_DISTANCE", header)
        self.assertIn("candidate < static_cast<int64_t>(g.num_nodes())", header)
        self.assertNotIn("future = current", header)
        self.assertEqual(
            self.cira_builder.resolve_future_row_distance(16, 0), 16
        )
        self.assertEqual(
            self.cira_builder.resolve_future_row_distance(16, 7), 7
        )

    def test_cira_bfs_prefetches_bounded_future_rows(self):
        transformed = self.transform_source(
            "bfs", self.cira_builder.patch_bfs
        )
        self.assertEqual(
            transformed.count("GAPBS_CIRA_FUTURE_NODE(g, u, pf_u)"), 2
        )
        bottom_future = transformed.index("auto pf_neigh = g.in_neigh(pf_u)")
        bottom_prefetch = transformed.index(
            "GAPBS_CIRA_PREFETCH_IN_CSR_RECORDS_ROW(g, pf_u)",
            bottom_future,
        )
        bottom_current = transformed.index("auto neigh = g.in_neigh(u)")
        self.assertLess(bottom_future, bottom_prefetch)
        self.assertLess(bottom_prefetch, bottom_current)
        top_future = transformed.index("auto pf_neigh = g.out_neigh(pf_u)")
        top_prefetch = transformed.index(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED_ROW(g, pf_u, parent)",
            top_future,
        )
        top_current = transformed.index("auto neigh = g.out_neigh(u)")
        self.assertLess(top_future, top_prefetch)
        self.assertLess(top_prefetch, top_current)
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_RANGE(neigh.begin(), neigh.end())",
            transformed,
        )

    def test_cira_bc_prefetches_bounded_future_rows_in_both_phases(self):
        transformed = self.transform_source("bc", self.cira_builder.patch_bc)
        self.assertEqual(
            transformed.count("GAPBS_CIRA_FUTURE_NODE(g, u, pf_u)"), 2
        )
        forward = transformed.index(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED_ROW(g, pf_u, depths)"
        )
        forward_current = transformed.index("auto neigh = g.out_neigh(u)")
        self.assertLess(forward, forward_current)
        reverse = transformed.index(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED_ROW(g, pf_u, deltas)"
        )
        reverse_current = transformed.index(
            "auto neigh = g.out_neigh(u)", forward_current + 1
        )
        self.assertLess(reverse, reverse_current)
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED(g,", transformed
        )
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_RANGE(neigh.begin(), neigh.end())",
            transformed,
        )

    def test_cira_pr_prefetches_bounded_future_row(self):
        transformed = self.transform_source(
            "pr",
            lambda src_dir: self.cira_builder.patch_pr_like(src_dir, "pr.cc"),
        )
        future = transformed.index("GAPBS_CIRA_FUTURE_NODE(g, u, pf_u)")
        future_row = transformed.index("auto pf_neigh = g.in_neigh(pf_u)")
        prefetch = transformed.index(
            "GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROW("
            "g, pf_u, outgoing_contrib)"
        )
        current = transformed.index("auto neigh = g.in_neigh(u)")
        self.assertLess(future, future_row)
        self.assertLess(future_row, prefetch)
        self.assertLess(prefetch, current)
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED(g,", transformed
        )
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_RANGE(neigh.begin(), neigh.end())",
            transformed,
        )

    def test_cira_sssp_prefetches_bounded_future_row(self):
        transformed = self.transform_source(
            "sssp", self.cira_builder.patch_sssp
        )
        future = transformed.index("GAPBS_CIRA_FUTURE_NODE(g, u, pf_u)")
        future_row = transformed.index("auto pf_neigh = g.out_neigh(pf_u)")
        prefetch = transformed.index(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED_FIELD_ROW("
            "g, pf_u, v, dist)"
        )
        current = transformed.index("auto neigh = g.out_neigh(u)")
        self.assertLess(future, future_row)
        self.assertLess(future_row, prefetch)
        self.assertLess(prefetch, current)
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_OUT_CSR_INDEXED_FIELD(g,", transformed
        )
        self.assertNotIn(
            "GAPBS_CIRA_PREFETCH_RANGE(neigh.begin(), neigh.end())",
            transformed,
        )

    def test_cira_manifest_records_future_row_policy(self):
        source = CIRA_BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn('"future_row_policy": "node-id-ahead-v1"', source)
        self.assertIn('"future_row_distances": future_row_distances', source)

    def test_verify_mode_makes_invalid_summary_nonzero(self):
        runner_source = RUNNER_PATH.read_text(encoding="utf-8")
        self.assertIn('row["status"] != "ok"', runner_source)
        self.assertIn('row["verification"] != "pass"', runner_source)
        self.assertIn("raise SystemExit(1)", runner_source)

    def test_config_reports_verification_from_exit_event(self):
        config = CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("simulator.get_last_exit_event_cause()", config)
        self.assertIn('print("Verification: PASS")', config)
        self.assertIn('print("Verification: FAIL")', config)


if __name__ == "__main__":
    unittest.main()
