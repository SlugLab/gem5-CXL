# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest

from scripts import cxl_latency_spectrum as latency


class LatencyContractTest(unittest.TestCase):
    def test_labels_and_ticks_are_exact(self):
        self.assertEqual(latency.LABELS, ("200ns", "500ns", "1us", "2us"))
        self.assertEqual(latency.ticks("200ns"), 200_000)
        self.assertEqual(latency.ticks("500ns"), 500_000)
        self.assertEqual(latency.ticks("1us"), 1_000_000)
        self.assertEqual(latency.ticks("2us"), 2_000_000)

    def test_unknown_or_noncanonical_labels_fail(self):
        for value in ("1000ns", "1µs", "0ns", "3us"):
            with self.subTest(value=value):
                with self.assertRaises(latency.LatencyError):
                    latency.ticks(value)


if __name__ == "__main__":
    unittest.main()
