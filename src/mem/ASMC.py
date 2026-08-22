# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

from m5.objects.ClockedObject import ClockedObject
from m5.params import *
from m5.proxy import *


class ASMC(ClockedObject):
    type = "ASMC"
    cxx_header = "mem/asmc.hh"
    cxx_class = "gem5::ASMC"

    system = Param.System(Parent.any, "System this ASMC belongs to")
    mem_side_port = RequestPort("Timing request port toward cache/memory")

    spm_size = Param.MemorySize("256KiB", "Modeled ASMC SPM capacity")
    cache_line_size = Param.Unsigned(
        Parent.cache_line_size, "Maximum packet chunk size"
    )
    default_granularity = Param.Unsigned(
        8, "Initial bytes moved by each AMU operation"
    )
    max_outstanding = Param.Unsigned(
        512, "Initial maximum outstanding AMU operations (increased from 256)"
    )
    max_send_queue = Param.Unsigned(
        1024, "Maximum queued memory packets waiting for the cache port (increased from 512)"
    )
    issue_latency = Param.Latency(
        "1ns", "Delay from m5op issue to first ASMC memory packet send"
    )
    completion_latency = Param.Latency(
        "1ns", "Fast ASMC completion delay (direct memory access)"
    )
    asmc_latency = Param.Latency(
        "1ns", "Minimal AMU latency for direct memory operations"
    )
