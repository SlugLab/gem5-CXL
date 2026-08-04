# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the deterministic two-core CIRA routing/coalescing workload."""

import argparse
import runpy
import sys
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--binary", required=True, type=Path)
args = parser.parse_args()

repo = Path(__file__).resolve().parents[3]
config_dir = repo / "configs" / "example" / "gem5_library"
config = config_dir / "x86-gapbs-amu-se.py"
sys.path.insert(0, str(config_dir))
sys.argv = [
    str(config),
    "--binary",
    str(args.binary.resolve()),
    "--arguments",
    "",
    "--scale",
    "1",
    "--iterations",
    "1",
    "--cores",
    "2",
    "--cpu",
    "timing",
    "--mem-size",
    "4GiB",
    "--disable-hw-prefetchers",
    "--no-asmc",
    "--cira",
    "--cira-to-l2",
    "--cira-max-outstanding",
    "256",
    "--cira-max-send-queue",
    "1024",
    "--cira-max-csr-walk-queue",
    "64",
    "--cira-csr-lines-per-turn",
    "8",
    "--cira-max-completed-lines",
    "4096",
    "--cira-issue-latency",
    "1ns",
    "--require-m5-verification-exit",
]
runpy.run_path(str(config), run_name="__main__")
