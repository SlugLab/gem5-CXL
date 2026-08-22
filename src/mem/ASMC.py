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
    spm_side_ports = VectorRequestPort(
        "Per-core coherent private-L2 SPM ports"
    )

    calibration_profile = Param.String("legacy", "Bound AMU calibration profile")
    calibration_manifest_sha256 = Param.String("", "Calibration manifest SHA-256")

    spm_size = Param.MemorySize("256KiB", "Modeled ASMC SPM capacity")
    cache_line_size = Param.Unsigned(
        Parent.cache_line_size, "Maximum packet chunk size"
    )
    default_granularity = Param.Unsigned(
        8, "Initial bytes moved by each AMU operation"
    )
    max_outstanding = Param.Unsigned(
        256, "Initial maximum outstanding AMU operations"
    )
    max_send_queue = Param.Unsigned(
        512, "Maximum queued memory packets waiting for the cache port"
    )
    spm_send_queue_size = Param.Unsigned(
        512, "Packets queued per SPM port"
    )
    pending_queue_entries = Param.Unsigned(32, "Entries per internal service stage")
    id_batch_entries = Param.Unsigned(32, "AMART IDs obtained per metadata refill")
    metadata_latency = Param.Cycles(10, "Cycles for one metadata service")
    id_refill_latency = Param.Cycles(0, "Additional cycles at an ID batch boundary")
    completion_publish_latency = Param.Cycles(0, "Cycles to publish a completion")
    pr_descriptor_entries = Param.Unsigned(
        16, "Maximum accepted PageRank row descriptors"
    )
    pr_read_entries = Param.Unsigned(
        256, "Maximum PageRank payload read packets in flight"
    )
    pr_fp_add_cycles = Param.Cycles(1, "PageRank float32 add latency")
    pr_fp_mul_cycles = Param.Cycles(1, "PageRank float32 multiply latency")
    pr_fp_div_cycles = Param.Cycles(4, "PageRank float32 divide latency")
    issue_latency = Param.Latency(
        "1ns", "Delay from m5op issue to first ASMC memory packet send"
    )
    completion_latency = Param.Latency(
        "0ns", "Fixed ASMC completion delay after memory responses return"
    )
    asmc_latency = Param.Latency(
        "1000ns", "Configurable AMU compatibility completion delay"
    )
