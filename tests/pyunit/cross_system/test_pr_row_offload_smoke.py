# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
HARNESS = REPO / "tests/gem5/pr_offload/run_pr_row_offload.py"
WORKLOAD = REPO / "tests/gem5/pr_offload/pr_row_offload_smoke.cc"


def load_harness():
    spec = importlib.util.spec_from_file_location("pr_row_smoke", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrRowOffloadSmokeTest(unittest.TestCase):
    def test_six_row_fixture_and_failure_injections_are_frozen(self):
        source = WORKLOAD.read_text(encoding="utf-8")
        for token in (
            "constexpr int NumNodes = 6",
            "constexpr int NumIterations = 3",
            "#define PR_INJECT_BIT",
            "#define PR_INJECT_UNFINISHED",
            "PR_ROW_ITER_BITS mode=%s iteration=%d words=",
            "PR_ROW_VERIFY mode=%s status=PASS",
            "-ffp-contract=off",
            "-fno-fast-math",
        ):
            self.assertIn(token, source + HARNESS.read_text(encoding="utf-8"))

    def test_marker_parser_matches_all_words_and_rejects_one_bit(self):
        harness = load_harness()
        rows = {
            mode: [tuple(range(iteration, iteration + 6)) for iteration in range(3)]
            for mode in ("vanilla", "amu", "cira")
        }
        harness.validate_word_rows(rows)
        rows["cira"][2] = (*rows["cira"][2][:-1], rows["cira"][2][-1] ^ 1)
        with self.assertRaisesRegex(harness.SmokeError, "bit-exact"):
            harness.validate_word_rows(rows)

    def test_live_vanilla_amu_cira_and_fail_closed_cases(self):
        harness = load_harness()
        gem5 = REPO / "build/X86/gem5.opt"
        m5_library = REPO / "util/m5/build/x86/out/libm5.a"
        if not gem5.is_file() or not m5_library.is_file():
            self.skipTest("gem5 or libm5 is not built")
        proof = harness.run_smoke(gem5=gem5, m5_library=m5_library)
        self.assertEqual(proof["status"], "PASS")
        self.assertEqual(proof["delay_ticks"], 1_000_000)
        self.assertEqual(set(proof["modes"]), {"vanilla", "amu", "cira"})
        self.assertEqual(proof["modes"]["amu"]["read_packets"], 18)
        self.assertEqual(proof["modes"]["cira"]["read_packets"], 63)
        self.assertEqual(
            set(proof["failed_injections"]),
            {"changed-bit", "queue-capacity", "unfinished-write"},
        )


if __name__ == "__main__":
    unittest.main()
