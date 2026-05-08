# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""
Run a local GAPBS binary in X86 syscall-emulation mode.

This config is intended for comparing a normal GAPBS binary against a binary
rebuilt with scripts/build_gapbs_amu_cxlmemuring.py. It avoids the stock
full-system GAPBS resource's KVM requirement.
"""

import argparse
import time
from pathlib import Path

import m5
from m5.objects import ASMC

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import (
    PrivateL1PrivateL2CacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires


requires(isa_required=ISA.X86)

parser = argparse.ArgumentParser(
    description="Run local X86 GAPBS binaries, including AMU-instrumented ones."
)
parser.add_argument("--binary", required=True, help="Path to GAPBS binary.")
parser.add_argument(
    "--arguments",
    default="-g 10 -n 1",
    help="Arguments passed to the GAPBS binary.",
)
parser.add_argument("--cores", type=int, default=1)
parser.add_argument("--cpu", choices=["atomic", "timing", "o3"], default="timing")
parser.add_argument("--mem-size", default="4GiB")
parser.add_argument("--clk", default="3GHz")
parser.add_argument("--l1d-size", default="32KiB")
parser.add_argument("--l1i-size", default="32KiB")
parser.add_argument("--l2-size", default="256KiB")
parser.add_argument("--no-asmc", action="store_true")
parser.add_argument("--asmc-spm-size", default="256KiB")
parser.add_argument("--asmc-granularity", type=int, default=8)
parser.add_argument("--asmc-max-outstanding", type=int, default=256)
parser.add_argument("--asmc-max-send-queue", type=int, default=512)
parser.add_argument("--asmc-issue-latency", default="1ns")
parser.add_argument("--asmc-completion-latency", default="0ns")
parser.add_argument("--asmc-latency", default="1000ns")

args = parser.parse_args()

binary = Path(args.binary).resolve()
if not binary.exists():
    raise FileNotFoundError(binary)

cpu_type = {
    "atomic": CPUTypes.ATOMIC,
    "timing": CPUTypes.TIMING,
    "o3": CPUTypes.O3,
}[args.cpu]

cache_hierarchy = PrivateL1PrivateL2CacheHierarchy(
    l1d_size=args.l1d_size,
    l1i_size=args.l1i_size,
    l2_size=args.l2_size,
)
memory = SingleChannelDDR4_2400(size=args.mem_size)
processor = SimpleProcessor(cpu_type=cpu_type, isa=ISA.X86, num_cores=args.cores)

board = SimpleBoard(
    clk_freq=args.clk,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)
board.m5ops_base = 0xFFFF0000

if not args.no_asmc:
    board.asmc = ASMC(
        spm_size=args.asmc_spm_size,
        default_granularity=args.asmc_granularity,
        max_outstanding=args.asmc_max_outstanding,
        max_send_queue=args.asmc_max_send_queue,
        issue_latency=args.asmc_issue_latency,
        completion_latency=args.asmc_completion_latency,
        asmc_latency=args.asmc_latency,
    )
    board.asmc.mem_side_port = cache_hierarchy.get_cpu_side_port()

board.set_se_binary_workload(
    BinaryResource(local_path=str(binary)),
    arguments=args.arguments.split(),
    env_list=[f"OMP_NUM_THREADS={args.cores}"],
)

simulator = Simulator(board=board)

start_wall = time.time()
print(f"Running {binary} {' '.join(args.arguments.split())}")
simulator.run()
m5.stats.dump()

print("Done with the simulation")
print(f"Simulated ticks: {simulator.get_current_tick()}")
print(f"Wallclock seconds: {time.time() - start_wall:.2f}")
