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
    mem_side_ports = VectorRequestPort(
        "Per-core timing prefetch ports toward private L2s"
    )
    csr_mem_side_port = RequestPort(
        "Device-side timing port for CIRA CSR index reads"
    )
    demand_probe_targets = VectorParam.SimObject(
        [], "Private L2s used for target-local CIRA usefulness attribution"
    )

    cache_line_size = Param.Unsigned(
        Parent.cache_line_size, "Cacheline size used for CIRA installs"
    )
    max_outstanding = Param.Unsigned(
        256, "Maximum outstanding CIRA cacheline install operations"
    )
    max_send_queue = Param.Unsigned(
        1024, "Maximum queued CIRA memory packets waiting for the cache port"
    )
    max_csr_walk_queue = Param.Unsigned(
        4096, "Maximum total queued CIRA CSR descriptors"
    )
    max_csr_index_reads = Param.Unsigned(
        1024, "Maximum queued plus in-flight CIRA CSR index reads"
    )
    csr_lines_per_turn = Param.Unsigned(
        64, "Maximum unique line candidates expanded per scheduling turn"
    )
    max_completed_lines = Param.Unsigned(
        65536, "Maximum completed usefulness records retained per core"
    )
    issue_latency = Param.Latency(
        "1ns", "Delay from m5op issue to first CIRA memory packet send"
    )
    completion_latency = Param.Latency(
        "0ns", "Fixed CIRA completion delay after memory responses return"
    )
    pr_descriptor_entries = Param.Unsigned(
        16, "Maximum accepted PageRank descriptors per issuing core"
    )
    pr_csr_read_entries = Param.Unsigned(
        256, "Maximum queued and in-flight PageRank CSR reads"
    )
    pr_coherent_entries = Param.Unsigned(
        256, "Maximum queued and in-flight PageRank coherent packets per core"
    )
    pr_fp_add_cycles = Param.Cycles(1, "Cycles per ordered PageRank float add")
    pr_fp_mul_cycles = Param.Cycles(1, "Cycles per PageRank float multiply")
    pr_fp_div_cycles = Param.Cycles(4, "Cycles per PageRank float divide")
    pr_reconfiguration_latency = Param.Latency(
        "100ns", "Charged latency for a CIRA PageRank JIT reconfiguration"
    )
    enabled = Param.Bool(True, "Enable CIRA timing prefetch requests")
    timing_csr_traversal = Param.Bool(
        True, "Use bounded device-side timing reads for CSR indices"
    )
