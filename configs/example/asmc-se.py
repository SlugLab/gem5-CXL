# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import argparse

import m5
from m5.objects import *


parser = argparse.ArgumentParser(description="Minimal X86 SE ASMC smoke config")
parser.add_argument("--binary", required=True)
parser.add_argument("--arguments", default="")
parser.add_argument("--mem-size", default="512MiB")
parser.add_argument("--clk", default="1GHz")
parser.add_argument("--no-asmc", action="store_true")
parser.add_argument("--asmc-spm-size", default="256KiB")
parser.add_argument("--asmc-granularity", type=int, default=8)
parser.add_argument("--asmc-max-outstanding", type=int, default=256)
parser.add_argument("--asmc-latency", default="1000ns")
parser.add_argument("--maxinsts", type=int, default=0)
parser.add_argument("--simple-memory", action="store_true")
args = parser.parse_args()

system = System()
system.clk_domain = SrcClockDomain(
    clock=args.clk, voltage_domain=VoltageDomain()
)
system.mem_mode = "timing"
system.mem_ranges = [AddrRange(args.mem_size)]
system.m5ops_base = 0xFFFF0000

system.cpu = X86TimingSimpleCPU()
if args.maxinsts:
    system.cpu.max_insts_any_thread = args.maxinsts
system.membus = SystemXBar()

system.cpu.icache_port = system.membus.cpu_side_ports
system.cpu.dcache_port = system.membus.cpu_side_ports
system.cpu.createInterruptController()
system.cpu.interrupts[0].pio = system.membus.mem_side_ports
system.cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
system.cpu.interrupts[0].int_responder = system.membus.mem_side_ports

if args.simple_memory:
    system.mem_ctrl = SimpleMemory(range=system.mem_ranges[0])
    system.mem_ctrl.port = system.membus.mem_side_ports
else:
    system.mem_ctrl = MemCtrl()
    system.mem_ctrl.dram = DDR3_1600_8x8(range=system.mem_ranges[0])
    system.mem_ctrl.port = system.membus.mem_side_ports
system.system_port = system.membus.cpu_side_ports

if not args.no_asmc:
    system.asmc = ASMC(
        spm_size=args.asmc_spm_size,
        default_granularity=args.asmc_granularity,
        max_outstanding=args.asmc_max_outstanding,
        asmc_latency=args.asmc_latency,
    )
    system.asmc.mem_side_port = system.membus.cpu_side_ports
    system.asmc.spm_side_ports = system.membus.cpu_side_ports

system.workload = SEWorkload.init_compatible(args.binary)
process = Process()
process.cmd = [args.binary] + args.arguments.split()
system.cpu.workload = process
system.cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

print("Beginning simulation")
event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {event.getCause()}")
