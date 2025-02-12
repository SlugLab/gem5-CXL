# Copyright (c) 2021-24 The Regents of the University of California
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
This script utilizes the X86DemoBoard to run a simple Ubunutu boot. The script
will boot the the OS to login before exiting the simulation.

A detailed terminal output can be found in `m5out/system.pc.com_1.device`.

**Warning:** The X86DemoBoard uses the Timing CPU. The boot may take
considerable time to complete execution.
`configs/example/gem5_library/x86-ubuntu-run-with-kvm.py` can be referenced as
an example of booting Ubuntu with a KVM CPU.

Usage
-----

```
scons build/X86/gem5.opt
./build/X86/gem5.opt configs/example/gem5_library/x86-ubuntu-run.py
```
"""

from m5.objects import *

from gem5.prebuilt.demo.x86_demo_board import X86DemoBoard
from gem5.resources.resource import obtain_resource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator

# Here we setup the board. The prebuilt X86DemoBoard allows for Full-System X86
# simulation.
board = X86DemoBoard()

workload = obtain_resource("x86-ubuntu-24.04-boot-with-systemd")
board.set_workload(workload)
xbar = board.get_cache_hierarchy().membus
slar0 = AddrRange(start="0x400000000", size="1024MiB")  # CXL controller
slar = AddrRange(start="0x440000000", size="512MiB")  # CXL device 1
slar2 = AddrRange(start="0x460000000", size="512MiB")  # CXL device 2

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


def exit_event_handler():
    print("First exit: kernel booted")
    yield False  # gem5 is now executing systemd startup
    print("Second exit: Started `after_boot.sh` script")
    # The after_boot.sh script is executed after the kernel and systemd have
    # booted.
    yield False  # gem5 is now executing the `after_boot.sh` script
    print("Third exit: Finished `after_boot.sh` script")
    # The after_boot.sh script will run a script if it is passed via
    # m5 readfile. This is the last exit event before the simulation exits.
    yield True


simulator = Simulator(
    board=board,
    on_exit_event={
        # Here we want override the default behavior for the first m5 exit
        # exit event.
        ExitEvent.EXIT: exit_event_handler()
    },
)

simulator.run()
