# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ASMC_PY = (REPO / "src/mem/ASMC.py").read_text(encoding="utf-8")
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")
CONFIG = (
    REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
).read_text(encoding="utf-8")
BUILDER = (
    REPO / "scripts/build_gapbs_amu_cxlmemuring.py"
).read_text(encoding="utf-8")
MATCHED = (
    REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py"
).read_text(encoding="utf-8")
M5OPS = (
    REPO / "include/gem5/asm/generic/m5ops.h"
).read_text(encoding="utf-8")
PSEUDO = (REPO / "src/sim/pseudo_inst.cc").read_text(encoding="utf-8")
AMU_HEADER = (REPO / "util/amu/amu.h").read_text(encoding="utf-8")
PR_MATH = REPO / "src/mem/pr_row_math.hh"
PR_SMOKE = (
    REPO / "tests/test-progs/amu-smoke/amu_pr_rows_smoke.c"
).read_text(encoding="utf-8")


class AsmcPaperModelTest(unittest.TestCase):
    def test_amu_pr_descriptor_has_real_model_dispatch(self):
        self.assertIn("M5OP_AMU_PR_ROWS       0x65", M5OPS)
        self.assertIn("asmc->issuePrRows(tc, desc.addr)", PSEUDO)
        self.assertIn(
            "uint64_t issuePrRows(ThreadContext *tc, Addr desc_addr);",
            HEADER,
        )
        self.assertIn("m5_amu_pr_rows(const void *desc)", (
            REPO / "include/gem5/m5ops.h"
        ).read_text(encoding="utf-8"))
        self.assertIn("amu_pr_rows", AMU_HEADER)

    def test_amu_pr_stats_fail_closed_on_unfinished_work(self):
        for name in (
            "issuedPrDescriptors",
            "completedPrDescriptors",
            "prRows",
            "prReadPackets",
            "prWritePackets",
            "prComputeTicks",
            "prQueueStallTicks",
        ):
            self.assertIn(name, SOURCE)

    def test_amu_pr_has_bounded_calibratable_resources_and_f32_math(self):
        for token in (
            'pr_descriptor_entries = Param.Unsigned(',
            'pr_read_entries = Param.Unsigned(',
            'pr_fp_add_cycles = Param.Cycles(',
            'pr_fp_mul_cycles = Param.Cycles(',
            'pr_fp_div_cycles = Param.Cycles(',
        ):
            self.assertIn(token, ASMC_PY)
        self.assertTrue(PR_MATH.is_file())
        math = PR_MATH.read_text(encoding="utf-8")
        for helper, operation in (
            ("prF32Div", "left / right"),
            ("prF32Add", "left + right"),
            ("prF32Mul", "left * right"),
        ):
            self.assertIn(helper, math)
            self.assertIn(operation, math)
        self.assertGreaterEqual(math.count("volatile float value"), 3)

    def test_amu_pr_reschedules_each_row_and_reduces_in_csr_order(self):
        self.assertIn(
            "return prOutstanding.count(id) != 0;", SOURCE
        )
        self.assertIn(
            "for (float contribution : state.contributions)", SOURCE
        )
        self.assertIn("sum = prF32Add(sum, contribution);", SOURCE)
        self.assertIn(
            "pendingPrReads + reservedPrReadSlots + packets > "
            "prReadEntries",
            SOURCE,
        )
        self.assertIn("reservedInitialPackets", HEADER + SOURCE)
        self.assertIn("reservedPrReadSlots", HEADER + SOURCE)
        self.assertIn(
            "reservedFarSendSlots += initialPacketCount", SOURCE
        )
        self.assertIn(
            "reservedPrReadSlots += initialPacketCount", SOURCE
        )
        self.assertIn("state.neighbors[sender_state->prIndex]", SOURCE)
        completion = SOURCE[
            SOURCE.index("ASMC::completePrDescriptor"):
            SOURCE.index("ASMC::configFor", SOURCE.index(
                "ASMC::completePrDescriptor"
            ))
        ]
        self.assertIn("completionWaiters.count(tc) != 0", completion)
        self.assertIn("!hasOutstanding", completion)
        self.assertLess(
            completion.index("!hasOutstanding"),
            completion.index("completionWaiters.erase(tc)"),
        )
        self.assertIn("PR_ROW_CONTRIB", PR_SMOKE)
        self.assertIn("PR_ROW_PULL", PR_SMOKE)
        self.assertIn("bits(next_scores[row]) != bits(expected)", PR_SMOKE)

    @staticmethod
    def calibration_loader():
        tree = ast.parse(CONFIG)
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Import) and all(
                alias.name in {"hashlib", "json"} for alias in node.names
            ):
                selected.append(node)
            elif isinstance(node, ast.ImportFrom) and node.module == "pathlib":
                selected.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id in {"AMU_PDF_SHA256", "CIRA_CSV_SHA256"}
                for target in node.targets
            ):
                selected.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in {"sha256_file", "load_amu_calibration"}
            ):
                selected.append(node)
        namespace = {}
        exec(compile(ast.Module(selected, []), "<amu-config-loader>", "exec"), namespace)
        return namespace

    def test_paper_resource_parameters_exist(self):
        for token in (
            'pending_queue_entries = Param.Unsigned(32',
            'id_batch_entries = Param.Unsigned(32',
            'metadata_latency = Param.Cycles(10',
            'id_refill_latency = Param.Cycles(0',
            'completion_publish_latency = Param.Cycles(0',
        ):
            self.assertIn(token, ASMC_PY)

    def test_internal_pending_queue_does_not_replace_amart_limit(self):
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext") :
            SOURCE.index("ASMC::startInitialAccess")
        ]
        self.assertIn(
            "outstandingPerThread[tc] >= config.maxOutstanding", issue
        )
        self.assertIn("metadataPending >= pendingQueueEntries", issue)
        self.assertIn("startInitialAccess", HEADER + SOURCE)
        self.assertIn("metadataPending--", SOURCE)

    def test_runtime_registers_are_isolated_per_thread_context(self):
        """Mixed 8-byte NodeID and 4-byte score loads must not race."""
        self.assertIn("struct ThreadConfig", HEADER)
        self.assertIn(
            "std::unordered_map<ThreadContext *, ThreadConfig> threadConfigs",
            HEADER,
        )
        self.assertIn(
            "std::unordered_map<ThreadContext *, uint64_t> outstandingPerThread",
            HEADER,
        )
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext") :
            SOURCE.index("ASMC::startInitialAccess")
        ]
        self.assertIn("ThreadConfig &config = configFor(tc)", issue)
        self.assertIn("state->size = config.granularity", issue)
        self.assertNotIn("state->size = granularity", issue)
        cfg_write = SOURCE[
            SOURCE.index("ASMC::cfgWrite") : SOURCE.index("ASMC::cfgRead")
        ]
        self.assertIn("ThreadConfig &config = configFor(tc)", cfg_write)
        self.assertIn("config.granularity = value ? value : 1", cfg_write)
        cfg_read = SOURCE[
            SOURCE.index("ASMC::cfgRead") :
            SOURCE.index("ASMC::deleteQueuedPacket", SOURCE.index("ASMC::cfgRead"))
        ]
        self.assertIn("outstandingPerThread.find(tc)", cfg_read)
        self.assertNotIn("return outstanding.size()", cfg_read)

    def test_id_batches_refill_only_at_batch_boundary(self):
        self.assertIn("idsRemaining", HEADER)
        self.assertIn("if (idsRemaining == 0)", SOURCE)
        self.assertIn("idsRemaining = idBatchEntries", SOURCE)
        self.assertIn("++stats.idBatchRefills", SOURCE)
        self.assertIn("--idsRemaining", SOURCE)

    def test_completion_and_polling_stats_are_recorded(self):
        for token in (
            "outstandingIntegral",
            "maxObservedOutstanding",
            "pendingQueueFull",
            "idBatchRefills",
            "metadataAccesses",
            "emptyGetfinPolls",
            "successfulGetfin",
            "consumerWaitTicks",
            "avgOutstanding",
        ):
            self.assertIn(token, HEADER + SOURCE)

    def test_occupancy_is_closed_at_dump_and_reset_boundaries(self):
        self.assertIn("void preDumpStats() override", HEADER)
        self.assertIn("owner.updateOccupancyIntegral()", SOURCE)
        self.assertIn("void resetStats() override", HEADER)
        reset = SOURCE[
            SOURCE.index("ASMC::resetStats") : SOURCE.index("ASMC::getPort")
        ]
        self.assertIn("lastOccupancyTick = curTick()", reset)

    def test_getfin_measures_real_poll_wait_without_extra_fake_delay(self):
        getfin = SOURCE[
            SOURCE.index("ASMC::getFinished") : SOURCE.index("ASMC::cfgWrite")
        ]
        self.assertIn("++stats.emptyGetfinPolls", getfin)
        self.assertIn("++stats.successfulGetfin", getfin)
        self.assertIn("pollWaitStart", getfin)
        self.assertIn("outstandingPerThread.find(tc)", getfin)
        self.assertNotIn("!outstanding.empty()", getfin)
        self.assertNotIn("getfinLatency", getfin)

    def test_reset_clears_new_resource_state(self):
        reset = SOURCE[SOURCE.index("ASMC::reset") :]
        for token in (
            "metadataPending = 0",
            "completionPending = 0",
            "idsRemaining = 0",
            "pollWaitStart.clear()",
        ):
            self.assertIn(token, reset)

    def test_paper_profile_is_manifest_bound_and_visible_in_config_ini(self):
        for token in (
            '"--asmc-profile"',
            '"paper-calibrated"',
            '"--asmc-calibration-manifest"',
            'AMU_PDF_SHA256',
            'CIRA_CSV_SHA256',
            'validation"]["status"] != "PASS"',
            'calibration_profile=args.asmc_profile',
            'calibration_manifest_sha256=amu_manifest_sha256',
        ):
            self.assertIn(token, CONFIG)
        self.assertIn('calibration_profile = Param.String("legacy"', ASMC_PY)
        self.assertIn('calibration_manifest_sha256 = Param.String(""', ASMC_PY)

    def test_paper_profile_requires_64k_and_wires_fitted_cycles(self):
        for token in (
            'args.asmc_spm_size != "64KiB"',
            'pending_queue_entries=amu_profile[',
            'id_batch_entries=amu_profile[',
            'metadata_latency=amu_profile["metadata_cycles"]',
            'id_refill_latency=amu_profile["id_refill_cycles"]',
            'completion_publish_latency=amu_profile["completion_cycles"]',
        ):
            self.assertIn(token, CONFIG)

    def test_manifest_loader_rejects_unapproved_sources_and_failed_fit(self):
        loader = self.calibration_loader()
        profile = {
            "spm_bytes": 64 * 1024,
            "pending_entries_per_state_machine": 32,
            "id_batch_entries": 32,
            "metadata_cycles": 4,
            "id_refill_cycles": 6,
            "completion_cycles": 2,
        }
        manifest = {
            "schema": 1,
            "sources": {
                "amu_pdf": {"sha256": loader["AMU_PDF_SHA256"]},
                "cira_csv": {"sha256": loader["CIRA_CSV_SHA256"]},
            },
            "amu": {
                "validation": {"status": "PASS"},
                "formal_profile_selection": {
                    "source": "bounded_table4_fit",
                    "fit_parameters_applied": True,
                },
                "fit": {
                    "parameters": {
                        "metadata_cycles": 4,
                        "id_refill_cycles": 6,
                        "completion_cycles": 2,
                    }
                },
                "formal_profile": profile,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded, digest = loader["load_amu_calibration"](path)
            self.assertEqual(loaded, profile)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

            for mutation, message in (
                (("sources", "amu_pdf", "sha256"), "PDF hash"),
                (("amu", "validation", "status"), "validation"),
            ):
                candidate = json.loads(json.dumps(manifest))
                owner = candidate
                for key in mutation[:-1]:
                    owner = owner[key]
                owner[mutation[-1]] = "FAIL"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    loader["load_amu_calibration"](path)

            candidate = json.loads(json.dumps(manifest))
            candidate["amu"]["formal_profile"]["metadata_cycles"] = 8
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs from fitted"):
                loader["load_amu_calibration"](path)

    def test_manifest_loader_accepts_only_proven_infeasible_defaults(self):
        loader = self.calibration_loader()
        manifest = {
            "schema": 1,
            "sources": {
                "amu_pdf": {"sha256": loader["AMU_PDF_SHA256"]},
                "cira_csv": {"sha256": loader["CIRA_CSV_SHA256"]},
            },
            "amu": {
                "validation": {
                    "status": "PASS",
                    "proxy_feasibility_status": (
                        "INFEASIBLE_NONNEGATIVE_COSTS"
                    ),
                },
                "formal_profile_selection": {
                    "source": "asmc_architecture_defaults",
                    "fit_parameters_applied": False,
                },
                "fit": {
                    "parameters": {
                        "metadata_cycles": 0,
                        "id_refill_cycles": 0,
                        "completion_cycles": 0,
                    }
                },
                "formal_profile": {
                    "spm_bytes": 64 * 1024,
                    "pending_entries_per_state_machine": 32,
                    "id_batch_entries": 32,
                    "metadata_cycles": 10,
                    "id_refill_cycles": 0,
                    "completion_cycles": 0,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            profile, _ = loader["load_amu_calibration"](path)
            self.assertEqual(profile["metadata_cycles"], 10)

            for mutation, message in (
                (("amu", "validation", "proxy_feasibility_status"), "infeasible"),
                (("amu", "formal_profile", "metadata_cycles"), "architecture default"),
                (("amu", "formal_profile_selection", "fit_parameters_applied"), "must not apply"),
            ):
                candidate = json.loads(json.dumps(manifest))
                owner = candidate
                for key in mutation[:-1]:
                    owner = owner[key]
                owner[mutation[-1]] = "FEASIBLE" if "status" in mutation[-1] else 8
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    loader["load_amu_calibration"](path)

    def test_async_window_submit_never_waits_and_consume_is_ordered(self):
        self.assertIn("class AsyncWindow", BUILDER)
        submit = BUILDER[
            BUILDER.index("void submit(") : BUILDER.index("T consume_next()")
        ]
        self.assertIn("amu_aload", submit)
        self.assertNotIn("amu_getfin", submit)
        self.assertNotIn("consume_next", submit)
        consume = BUILDER[
            BUILDER.index("T consume_next()") : BUILDER.index("private:", BUILDER.index("T consume_next()"))
        ]
        self.assertIn("amu_getfin", consume)
        self.assertIn("head_", consume)

    def test_matched_pr_uses_ordered_line_batches(self):
        loop = MATCHED[
            MATCHED.index("_AMU_PULL_LOOP") : MATCHED.index("_CIRA_PULL_LOOP")
        ]
        self.assertIn("LineBatch<gapbs_amu::Gem5LineBackend> initial_batch", loop)
        self.assertIn("LineBatch<gapbs_amu::Gem5LineBackend> current_batch", loop)
        self.assertLess(loop.index("current_batch.issue_all()"),
                        loop.index("current_batch.wait_all()"))
        self.assertIn(
            "incoming_total = incoming_total + current_batch.value<ScoreT>",
            loop,
        )
        self.assertNotIn("AsyncWindow", loop)
        self.assertNotIn("load_values", loop)
        self.assertNotIn("load_value", loop)


if __name__ == "__main__":
    unittest.main()
