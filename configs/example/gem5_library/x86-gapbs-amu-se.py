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
from m5.objects import ASMC, CIRA, NULL, SerialLink

from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import (
    PrivateL1PrivateL2CacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires


requires(isa_required=ISA.X86)


class TunablePrivateL1PrivateL2CacheHierarchy(PrivateL1PrivateL2CacheHierarchy):
    def __init__(
        self,
        *args,
        disable_hw_prefetchers=False,
        l1_mshrs=None,
        l1_tgts_per_mshr=None,
        l2_mshrs=None,
        l2_tgts_per_mshr=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._disable_hw_prefetchers = disable_hw_prefetchers
        self._l1_mshrs = l1_mshrs
        self._l1_tgts_per_mshr = l1_tgts_per_mshr
        self._l2_mshrs = l2_mshrs
        self._l2_tgts_per_mshr = l2_tgts_per_mshr

    def incorporate_cache(self, board):
        super().incorporate_cache(board)

        for idx in range(board.get_processor().get_num_cores()):
            l1i = getattr(self, f"l1i-cache-{idx}")
            l1d = getattr(self, f"l1d-cache-{idx}")
            l2 = getattr(self, f"l2-cache-{idx}")
            for cache in (l1i, l1d):
                if self._disable_hw_prefetchers:
                    cache.prefetcher = NULL
                if self._l1_mshrs is not None:
                    cache.mshrs = self._l1_mshrs
                if self._l1_tgts_per_mshr is not None:
                    cache.tgts_per_mshr = self._l1_tgts_per_mshr
            if self._disable_hw_prefetchers:
                l2.prefetcher = NULL
            if self._l2_mshrs is not None:
                l2.mshrs = self._l2_mshrs
            if self._l2_tgts_per_mshr is not None:
                l2.tgts_per_mshr = self._l2_tgts_per_mshr


class CXLSimpleBoard(SimpleBoard):
    def __init__(
        self,
        *args,
        cxl_memory=False,
        cxl_args=None,
        cira_to_l2=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_cxl_memory", cxl_memory)
        object.__setattr__(self, "_cxl_args", cxl_args or {})
        object.__setattr__(self, "_cxl_mem_ports", None)
        object.__setattr__(self, "_cira_to_l2", cira_to_l2)

    def get_mem_ports(self):
        if not self._cxl_memory:
            return super().get_mem_ports()

        if self._cxl_mem_ports is None:
            cxl_mem_ports = []
            for idx, (addr_range, port) in enumerate(super().get_mem_ports()):
                link = SerialLink(ranges=[addr_range], **self._cxl_args)
                link.mem_side_port = port
                setattr(self, f"cxl_mem_link{idx}", link)
                cxl_mem_ports.append((addr_range, link.cpu_side_port))
            object.__setattr__(self, "_cxl_mem_ports", cxl_mem_ports)

        return self._cxl_mem_ports

    def _connect_things(self):
        super()._connect_things()
        cira = getattr(self, "cira", None)
        if cira is None:
            return

        if self._cira_to_l2 and hasattr(self.cache_hierarchy, "l2buses"):
            cira.demand_probe_target = getattr(
                self.cache_hierarchy, "l2-cache-0"
            )
            cira.mem_side_port = self.cache_hierarchy.l2buses[0].cpu_side_ports
        else:
            cira.mem_side_port = self.cache_hierarchy.get_cpu_side_port()

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
parser.add_argument(
    "--cpu", choices=["atomic", "timing", "o3", "minor"], default="timing"
)
parser.add_argument("--mem-size", default="4GiB")
parser.add_argument("--clk", default="3GHz")
parser.add_argument("--l1d-size", default="32KiB")
parser.add_argument("--l1i-size", default="32KiB")
parser.add_argument("--l2-size", default="256KiB")
parser.add_argument("--disable-hw-prefetchers", action="store_true")
parser.add_argument("--l1-mshrs", type=int)
parser.add_argument("--l1-tgts-per-mshr", type=int)
parser.add_argument("--l2-mshrs", type=int)
parser.add_argument("--l2-tgts-per-mshr", type=int)
parser.add_argument(
    "--env",
    action="append",
    default=[],
    help="Extra workload environment entry, e.g. KEY=VALUE.",
)
parser.add_argument("--no-asmc", action="store_true")
parser.add_argument("--asmc-spm-size", default="256KiB")
parser.add_argument("--asmc-granularity", type=int, default=8)
parser.add_argument("--asmc-max-outstanding", type=int, default=256)
parser.add_argument("--asmc-max-send-queue", type=int, default=512)
parser.add_argument("--asmc-issue-latency", default="1ns")
parser.add_argument("--asmc-completion-latency", default="0ns")
parser.add_argument("--asmc-latency", default="0ns")
parser.add_argument(
    "--cxl-memory",
    action="store_true",
    help="Route normal cache/memory traffic through a CXL SerialLink.",
)
parser.add_argument("--cxl-link-delay", default="1us")
parser.add_argument("--cxl-link-speed", type=int, default=32)
parser.add_argument("--cxl-link-lanes", type=int, default=16)
parser.add_argument("--cxl-link-req-size", type=int, default=256)
parser.add_argument("--cxl-link-resp-size", type=int, default=256)
parser.add_argument("--cira", action="store_true", help="Enable CIRA model.")
parser.add_argument(
    "--cira-to-l2",
    action="store_true",
    help="Send CIRA cacheline installs through the first private L2.",
)
parser.add_argument("--cira-max-outstanding", type=int, default=256)
parser.add_argument("--cira-max-send-queue", type=int, default=1024)
parser.add_argument("--cira-issue-latency", default="1ns")
parser.add_argument("--cira-completion-latency", default="0ns")
parser.add_argument(
    "--roi-work-events",
    action="store_true",
    help="Reset stats at m5_work_begin and stop after m5_work_end.",
)
parser.add_argument("--continue-after-roi", action="store_true")

args = parser.parse_args()

binary = Path(args.binary).resolve()
if not binary.exists():
    raise FileNotFoundError(binary)

cpu_type = {
    "atomic": CPUTypes.ATOMIC,
    "timing": CPUTypes.TIMING,
    "o3": CPUTypes.O3,
    "minor": CPUTypes.MINOR,
}[args.cpu]

cache_hierarchy = TunablePrivateL1PrivateL2CacheHierarchy(
    l1d_size=args.l1d_size,
    l1i_size=args.l1i_size,
    l2_size=args.l2_size,
    disable_hw_prefetchers=args.disable_hw_prefetchers,
    l1_mshrs=args.l1_mshrs,
    l1_tgts_per_mshr=args.l1_tgts_per_mshr,
    l2_mshrs=args.l2_mshrs,
    l2_tgts_per_mshr=args.l2_tgts_per_mshr,
)
memory = SingleChannelDDR4_2400(size=args.mem_size)
processor = SimpleProcessor(cpu_type=cpu_type, isa=ISA.X86, num_cores=args.cores)

board = CXLSimpleBoard(
    clk_freq=args.clk,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
    cxl_memory=args.cxl_memory,
    cxl_args={
        "delay": args.cxl_link_delay,
        "link_speed": args.cxl_link_speed,
        "num_lanes": args.cxl_link_lanes,
        "req_size": args.cxl_link_req_size,
        "resp_size": args.cxl_link_resp_size,
    },
    cira_to_l2=args.cira_to_l2,
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
    if args.cxl_memory:
        board.asmc.mem_side_port = cache_hierarchy.get_cpu_side_port()
    else:
        board.cxl_link = SerialLink(
            delay=args.cxl_link_delay,
            link_speed=args.cxl_link_speed,
            num_lanes=args.cxl_link_lanes,
            req_size=args.cxl_link_req_size,
            resp_size=args.cxl_link_resp_size,
        )
        board.asmc.mem_side_port = board.cxl_link.cpu_side_port
        board.cxl_link.mem_side_port = cache_hierarchy.get_cpu_side_port()

if args.cira:
    board.cira = CIRA(
        max_outstanding=args.cira_max_outstanding,
        max_send_queue=args.cira_max_send_queue,
        issue_latency=args.cira_issue_latency,
        completion_latency=args.cira_completion_latency,
    )

board.set_se_binary_workload(
    BinaryResource(local_path=str(binary)),
    arguments=args.arguments.split(),
    env_list=[f"OMP_NUM_THREADS={args.cores}", *args.env],
)

start_tick = None


def handle_workbegin():
    print("Resetting stats at the start of ROI!")
    m5.stats.reset()
    global start_tick
    start_tick = m5.curTick()
    yield False


def handle_workend():
    print("Dump stats at the end of the ROI!")
    m5.stats.dump()
    yield not args.continue_after_roi


if args.roi_work_events:
    simulator = Simulator(
        board=board,
        on_exit_event={
            ExitEvent.WORKBEGIN: handle_workbegin(),
            ExitEvent.WORKEND: handle_workend(),
        },
    )
else:
    simulator = Simulator(board=board)

start_wall = time.time()
print(f"Running {binary} {' '.join(args.arguments.split())}")
simulator.run()
if args.continue_after_roi:
    exit_cause = simulator.get_last_exit_event_cause()
    if exit_cause == "m5_fail instruction encountered":
        print("Verification: FAIL")
        raise SystemExit(2)
    if exit_cause != "exiting with last active thread context":
        print(f"Verification: MISSING ({exit_cause})")
        raise SystemExit(3)
    print("Verification: PASS")
if not args.roi_work_events:
    m5.stats.dump()

print("Done with the simulation")
print(f"Simulated ticks: {simulator.get_current_tick()}")
if start_tick is not None:
    print(f"Simulated ROI ticks: {m5.curTick() - start_tick}")
print(f"Wallclock seconds: {time.time() - start_wall:.2f}")
