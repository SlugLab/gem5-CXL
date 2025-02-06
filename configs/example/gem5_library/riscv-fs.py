# Copyright (c) 2021 The Regents of the University of California
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
This example runs a simple linux boot. It uses the 'riscv-disk-img' resource.
It is built with the sources in `src/riscv-fs` in [gem5 resources](
https://github.com/gem5/gem5-resources).

Characteristics
---------------

* Runs exclusively on the RISC-V ISA with the classic caches
* Assumes that the kernel is compiled into the bootloader
* Automatically generates the DTB file
* Will boot but requires a user to login using `m5term` (username: `root`,
  password: `root`)
"""

from m5.objects import *
from m5.params import *

from gem5.components.boards.riscv_board import RiscvBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_walk_cache_hierarchy import (
    PrivateL1PrivateL2WalkCacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR3_1600
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

# Run a check to ensure the right version of gem5 is being used.
requires(isa_required=ISA.RISCV)

# Setup the cache hierarchy.
# For classic, PrivateL1PrivateL2 and NoCache have been tested.
# For Ruby, MESI_Two_Level and MI_example have been tested.
cache_hierarchy = PrivateL1PrivateL2WalkCacheHierarchy(
    l1d_size="32KiB", l1i_size="32KiB", l2_size="512KiB"
)

# Setup the system memory with its own range (0x80000000 - 0xBFFFFFFF)
memory = SingleChannelDDR3_1600()
# memory.range = AddrRange(start="0x80000000", size="1GB")

# Create non-overlapping memory ranges for CXL devices
# Start CXL ranges at 0x400000000 to avoid overlap with system memory
slar0 = AddrRange(start="0x400000000", size="1024MiB")  # CXL controller
slar = AddrRange(start="0x440000000", size="512MiB")  # CXL device 1
slar2 = AddrRange(start="0x460000000", size="512MiB")  # CXL device 2

# Setup a single core Processor.
processor = SimpleProcessor(
    cpu_type=CPUTypes.TIMING, isa=ISA.RISCV, num_cores=1
)

# Setup the board.
board = RiscvBoard(
    clk_freq="1GHz",
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)
# Set the Full board workload.
board.set_kernel_disk_workload(
    kernel=obtain_resource(
        "riscv-bootloader-vmlinux-5.10", resource_version="1.0.0"
    ),
    disk_image=obtain_resource("riscv-disk-img", resource_version="1.0.0"),
)

# Get the memory bus from the board using the proper method
xbar = board.get_cache_hierarchy().membus

# Create CXL components
board.cxl_controller = CXLController(
    width=16,
    frontend_latency=2,
    forward_latency=3,
    response_latency=3,
)
board.cxl_device = CXLDevice(
    width=16,
    frontend_latency=2,
    forward_latency=2,
    response_latency=4,
)
board.cxl_controller.seriallink = SerialLink(
    ranges=slar0,
    req_size=10,
    resp_size=10,
    num_lanes=16,
    link_speed=31,
    delay="100ns",
)
board.cxl_device.seriallink = SerialLink(
    ranges=slar,
    req_size=10,
    resp_size=10,
    num_lanes=16,
    link_speed=31,
    delay="100ns",
)
board.pciexbar = CXLXBar(
    width=16,
    frontend_latency=2,
    forward_latency=1,
    response_latency=2,
)
board.pciexbar2 = CXLXBar(
    width=16,
    frontend_latency=2,
    forward_latency=1,
    response_latency=2,
)
board.cxl_device2 = CXLDevice(
    width=16,
    frontend_latency=2,
    forward_latency=2,
    response_latency=4,
)
board.cxl_device2.seriallink = SerialLink(
    ranges=slar2,
    req_size=10,
    resp_size=10,
    num_lanes=16,
    link_speed=31,
    delay="100ns",
)
board.pciexbar2.seriallink = SerialLink(
    ranges=slar2,
    req_size=10,
    resp_size=10,
    num_lanes=16,
    link_speed=31,
    delay="100ns",
)

board.cxl_controller.monitor = CommMonitor()

# Connect the components
xbar.mem_side_ports = board.cxl_controller.cpu_side_ports
sl = board.cxl_controller.seriallink
board.cxl_controller.mem_side_ports = (
    board.cxl_controller.monitor.cpu_side_port
)
board.cxl_controller.monitor.mem_side_port = sl.cpu_side_port
sl.mem_side_port = board.pciexbar.cpu_side_ports

# cxl board 1
sl2 = board.cxl_device.seriallink
board.pciexbar.mem_side_ports = sl2.cpu_side_port
sl2.mem_side_port = board.cxl_device.cpu_side_ports

# cxl board 2
sl3 = board.pciexbar2.seriallink
sl4 = board.cxl_device2.seriallink
board.pciexbar.mem_side_ports = sl3.cpu_side_port
sl3.mem_side_port = board.pciexbar2.cpu_side_ports
board.pciexbar2.mem_side_ports = sl4.cpu_side_port
sl4.mem_side_port = board.cxl_device2.cpu_side_ports

# Memory controller setup with adjusted ranges
board.mem_ctrl = MemCtrl()
mc = board.mem_ctrl
mc.dram = DDR3_1600_8x8()
mc.dram.range = AddrRange(start="0x440000000", size="512MiB")  # Match slar
mc.port = board.cxl_device.mem_side_ports

board.mem_ctrl2 = MemCtrl()
mc2 = board.mem_ctrl2
mc2.dram = DDR3_1600_8x8()
mc2.dram.range = AddrRange(start="0x460000000", size="512MiB")  # Match slar2
board.cxl_device2.mem_side_ports = mc2.port


simulator = Simulator(board=board)
print("Beginning simulation!")
# Note: This simulation will never stop. You can access the terminal upon boot
# using m5term (`./util/term`): `./m5term localhost <port>`. Note the `<port>`
# value is obtained from the gem5 terminal stdout. Look out for
# "system.platform.terminal: Listening for connections on port <port>".
simulator.run()
