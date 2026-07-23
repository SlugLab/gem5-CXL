# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
CIRA_PY = REPO / "src" / "mem" / "CIRA.py"
CIRA_HH = REPO / "src" / "mem" / "cira.hh"
CIRA_CC = REPO / "src" / "mem" / "cira.cc"
GAPBS_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)


class CiraUsefulnessTrackerContractTest(unittest.TestCase):
    def test_transition_machine(self):
        compiler = shutil.which("g++") or shutil.which("c++")
        self.assertIsNotNone(compiler, "a C++ compiler is required")

        program = textwrap.dedent(
            r"""
            #include <cassert>
            #include <cstdint>

            #include "mem/cira_usefulness_tracker.hh"

            using gem5::CiraLineUsefulnessTracker;

            int
            main()
            {
                using Attribution =
                    CiraLineUsefulnessTracker::DemandAttribution;

                CiraLineUsefulnessTracker tracker(64);

                // Addresses are attributed at cacheline granularity.
                tracker.issue(0x1003);
                tracker.fill(0x103f, true);
                assert(tracker.demand(0x1010) == Attribution::Useful);
                assert(tracker.demand(0x1018) == Attribution::None);

                // A demand before fill is late and suppresses the later fill.
                tracker.issue(0x2000);
                assert(tracker.demand(0x203f) == Attribution::Late);
                assert(tracker.demand(0x2008) == Attribution::None);
                tracker.fill(0x2010, true);
                assert(tracker.demand(0x2020) == Attribution::None);

                // Duplicate issues collapse to one physical-fill token.
                tracker.issue(0x3000);
                tracker.issue(0x3030);
                assert(tracker.outstandingRefs(0x3001) == 2);
                tracker.fill(0x3008, true);
                assert(tracker.outstandingRefs(0x3038) == 0);
                assert(tracker.demand(0x3018) == Attribution::Useful);
                assert(tracker.demand(0x3028) == Attribution::None);

                // One late demand consumes all duplicate outstanding refs.
                tracker.issue(0x4000);
                tracker.issue(0x4038);
                assert(tracker.demand(0x4010) == Attribution::Late);
                assert(tracker.outstandingRefs(0x4020) == 0);
                tracker.fill(0x4000, true);
                assert(tracker.demand(0x4030) == Attribution::None);

                // A fill owned by another requestor resolves but cannot credit
                // an outstanding CIRA issue.
                tracker.issue(0x5000);
                tracker.fill(0x5030, false);
                assert(tracker.demand(0x5010) == Attribution::None);

                // A CIRA access that hits a resident line retires only its
                // corresponding issue and never creates a completed token.
                tracker.issue(0x6000);
                tracker.issue(0x6030);
                tracker.prefetchHit(0x6010);
                assert(tracker.outstandingRefs(0x6020) == 1);
                tracker.prefetchHit(0x6038);
                assert(tracker.outstandingRefs(0x6008) == 0);
                assert(tracker.demand(0x6000) == Attribution::None);

                // A late demand may install the line before the delayed CIRA
                // access reaches L2. Its later Hit resolves the suppressed
                // generation and must not poison a future issue/fill.
                tracker.issue(0x6800);
                assert(tracker.demand(0x6810) == Attribution::Late);
                tracker.prefetchHit(0x6820);
                tracker.issue(0x6830);
                tracker.fill(0x6808, true);
                assert(tracker.demand(0x6818) == Attribution::Useful);

                // If completed and outstanding state coexist, the demand is
                // useful only. It also clears stale same-line outstanding
                // refs so a later demand cannot be misclassified as late.
                tracker.issue(0x7000);
                tracker.fill(0x7000, true);
                tracker.issue(0x7010);
                assert(tracker.demand(0x7020) == Attribution::Useful);
                assert(tracker.outstandingRefs(0x7030) == 0);
                assert(tracker.demand(0x7008) == Attribution::None);

                tracker.issue(0x8000);
                tracker.clear();
                assert(tracker.outstandingRefs(0x8000) == 0);
                assert(tracker.demand(0x8000) == Attribution::None);
                return 0;
            }
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cira_usefulness_contract.cc"
            binary = Path(tmp) / "cira_usefulness_contract"
            source.write_text(program, encoding="utf-8")
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(REPO / "src"),
                    str(source),
                    "-o",
                    str(binary),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [str(binary)], capture_output=True, text=True
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )

    def test_cira_probe_and_stats_integration_contract(self):
        cira_py = CIRA_PY.read_text(encoding="utf-8")
        cira_hh = CIRA_HH.read_text(encoding="utf-8")
        cira_cc = CIRA_CC.read_text(encoding="utf-8")
        config = GAPBS_CONFIG.read_text(encoding="utf-8")

        self.assertIn("demand_probe_target", cira_py)
        self.assertIn("usefulPrefetches", cira_hh)
        self.assertIn("latePrefetches", cira_hh)
        self.assertIn('"Hit"', cira_cc)
        self.assertIn('"Miss"', cira_cc)
        self.assertIn('"Fill"', cira_cc)
        self.assertIn("resetStats()", cira_hh)
        self.assertIn("lineTracker.clear()", cira_cc)
        self.assertIn(
            "taskId() != context_switch_task_id::Prefetcher", cira_cc
        )
        self.assertIn("getRequestorName", cira_cc)
        self.assertIn('".data"', cira_cc)
        self.assertIn("cira.demand_probe_target", config)
        self.assertIn("cira.mem_side_port", config)

        recv_response = cira_cc[
            cira_cc.index("CIRA::recvTimingResp"):
            cira_cc.index("CIRA::recvReqRetry")
        ]
        self.assertNotIn("lineTracker.fill", recv_response)


if __name__ == "__main__":
    unittest.main()
