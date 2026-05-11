# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

from m5.objects.ClockedObject import ClockedObject
from m5.params import *
from m5.proxy import *


class CIRA(ClockedObject):
    type = "CIRA"
    cxx_header = "mem/cira.hh"
    cxx_class = "gem5::CIRA"

    system = Param.System(Parent.any, "System this CIRA model belongs to")
    mem_side_port = RequestPort("Timing prefetch port toward cache/memory")

    cache_line_size = Param.Unsigned(
        Parent.cache_line_size, "Cacheline size used for CIRA installs"
    )
    max_outstanding = Param.Unsigned(
        256, "Maximum outstanding CIRA cacheline install operations"
    )
    max_send_queue = Param.Unsigned(
        1024, "Maximum queued CIRA memory packets waiting for the cache port"
    )
    issue_latency = Param.Latency(
        "1ns", "Delay from m5op issue to first CIRA memory packet send"
    )
    completion_latency = Param.Latency(
        "0ns", "Fixed CIRA completion delay after memory responses return"
    )
    enabled = Param.Bool(True, "Enable CIRA timing prefetch requests")
