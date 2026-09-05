# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
CIRA_PY = REPO / "src" / "mem" / "CIRA.py"
CIRA_HH = REPO / "src" / "mem" / "cira.hh"
CIRA_CC = REPO / "src" / "mem" / "cira.cc"
M5OPS = REPO / "include" / "gem5" / "asm" / "generic" / "m5ops.h"
PSEUDO = REPO / "src" / "sim" / "pseudo_inst.cc"
CIRA_HEADER = REPO / "util" / "cira" / "cira.h"
TRANSLATING_PORT_PROXY_CC = REPO / "src" / "mem" / "translating_port_proxy.cc"
PROCESS_CC = REPO / "src" / "sim" / "process.cc"
SYSCALL_EMUL_HH = REPO / "src" / "sim" / "syscall_emul.hh"
TIMING_SIMPLE_CC = REPO / "src" / "cpu" / "simple" / "timing.cc"
ATOMIC_SIMPLE_CC = REPO / "src" / "cpu" / "simple" / "atomic.cc"
GAPBS_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)
CIRA_MULTICORE_WORKLOAD = (
    REPO / "tests" / "gem5" / "cira" / "cira_multicore_prefetch.cc"
)
CIRA_MULTICORE_CONFIG = (
    REPO / "tests" / "gem5" / "cira" / "run_cira_multicore.py"
)
CIRA_PR_WORKLOAD = (
    REPO / "tests" / "gem5" / "cira" / "cira_pr_rows_smoke.cc"
)


class CiraUsefulnessTrackerContractTest(unittest.TestCase):
    def test_cira_pr_descriptor_uses_native_dispatch(self):
        self.assertIn(
            "M5OP_CIRA_PR_ROWS      0x66",
            M5OPS.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "cira->issuePrRows(tc, desc.addr)",
            PSEUDO.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "cira_pr_rows(const struct pr_row_offload_desc *desc)",
            CIRA_HEADER.read_text(encoding="utf-8"),
        )

    def test_cira_pr_descriptor_uses_device_csr_and_coherent_value_ports(self):
        source = CIRA_CC.read_text(encoding="utf-8")
        self.assertIn("issuePrRows(ThreadContext *tc, Addr desc_addr)", source)
        self.assertIn("PrPacketRole::CsrRead", source)
        self.assertIn("PrPacketRole::CoherentRead", source)
        self.assertIn("PrPacketRole::CoherentWrite", source)
        self.assertIn("completedPrDescriptorsPerCore", source)
        self.assertIn("csrMemoryPort->sendTimingReq", source)
        self.assertIn("memSidePorts[targetCore]->sendTimingReq", source)

    def test_cira_jit_reconfiguration_is_a_real_completion(self):
        header = CIRA_HEADER.read_text(encoding="utf-8")
        self.assertIn("CIRA_CFG_PR_RECONFIGURE", header)
        self.assertIn("CIRA_CFG_PR_ROW_WINDOW", header)
        self.assertIn("CIRA_CFG_PR_LEAD_BLOCKS", header)
        source = CIRA_CC.read_text(encoding="utf-8")
        self.assertIn("prReconfigurationLatency", source)
        self.assertIn("completedPrReconfigurations", source)

    def test_cira_pr_policy_has_hardware_ranked_formation_charge(self):
        cira_py = CIRA_PY.read_text(encoding="utf-8")
        cira_hh = CIRA_HH.read_text(encoding="utf-8")
        source = CIRA_CC.read_text(encoding="utf-8")
        for token in (
            "pr_policy_base_cycles = Param.Cycles(",
            "pr_policy_a_cost_ppm = Param.Unsigned(",
            "pr_policy_b_cost_ppm = Param.Unsigned(",
            "pr_policy_c_cost_ppm = Param.Unsigned(",
        ):
            self.assertIn(token, cira_py)
        self.assertIn("policyReadyTick", cira_hh)
        self.assertIn("prPolicyCostPpm(desc.row_window, desc.lead_blocks)", source)
        self.assertIn("state->policyReadyTick", source)
        self.assertIn("const Tick policyReadyTick = state->policyReadyTick", source)
        self.assertIn("schedulePr(targetCore, policyReadyTick)", source)

    def test_cira_pr_descriptor_uses_policy_row_window_for_execution(self):
        source = CIRA_CC.read_text(encoding="utf-8")
        header = CIRA_HH.read_text(encoding="utf-8")
        self.assertIn("struct PrRowState", header)
        self.assertIn("std::map<uint64_t, PrRowState> rows", header)
        self.assertIn("uint64_t prRow", header)
        self.assertIn("state.desc.row_window", source)
        self.assertIn("processPrRow(state, it->second)", source)
        self.assertNotIn(
            "schedulePr(targetCore, clockEdge(Cycles(1)))", source
        )
        self.assertIn("scheduleAllPr(curTick())", source)
        self.assertIn("struct PrLineReadState", header)
        self.assertIn("cachedCsrReadLines", header)
        self.assertIn("pendingCoherentReadLines", header)
        self.assertIn("struct PrLineWriteState", header)
        self.assertIn("pendingWriteLines", header)
        self.assertIn("std::vector<PrReadWaiter> waiters", header)
        self.assertIn("copyPrReadFragment", source)
        self.assertIn("discardCleanBlock", source)
        cache_header = (REPO / "src/mem/cache/base.hh").read_text(
            encoding="utf-8"
        )
        self.assertIn("bool discardCleanBlock", cache_header)
        self.assertIn("CacheBlk::DirtyBit", cache_header)

    def test_cira_serializes_partial_result_writes_per_cache_line(self):
        source = CIRA_CC.read_text(encoding="utf-8")
        header = CIRA_HH.read_text(encoding="utf-8")
        self.assertIn("std::set<Addr> pendingPrWriteLines", header)
        self.assertIn(
            "if (pendingPrWriteLines.count(writeLine))", source
        )
        self.assertIn("pendingPrWriteLines.insert(writeLine)", source)
        self.assertIn("pendingPrWriteLines.erase(writeLine)", source)
        self.assertIn("pendingPrWriteLines.clear()", source)

    def test_csr_timing_walk_has_a_bounded_device_side_read_path(self):
        cira_py = CIRA_PY.read_text(encoding="utf-8")
        cira_hh = CIRA_HH.read_text(encoding="utf-8")
        cira_cc = CIRA_CC.read_text(encoding="utf-8")
        process_start = cira_cc.index("CIRA::processCsrWalk()")
        process_end = cira_cc.index(
            "CIRA::validatePrDescriptor(", process_start
        )
        process = cira_cc[process_start:process_end]

        self.assertNotIn("readIndex(", process)
        self.assertNotIn("readGuest(", process)
        self.assertIn("timing_csr_traversal = Param.Bool(", cira_py)
        self.assertIn("max_csr_index_reads = Param.Unsigned(", cira_py)
        self.assertIn("csr_mem_side_port = RequestPort(", cira_py)
        self.assertIn("enum class PacketRole", cira_hh)
        self.assertIn("PrefetchLine", cira_hh)
        self.assertIn("CsrIndexRead", cira_hh)
        self.assertIn("struct PendingCsrIndexRead", cira_hh)
        self.assertIn("const bool timingCsrTraversal", cira_hh)
        self.assertIn("const uint64_t maxCsrIndexReads", cira_hh)
        for stat in (
            "csrIndexReadPackets",
            "csrIndexReadBytes",
            "completedCsrIndexReads",
            "rejectedCsrIndexQueueFull",
            "timingCsrTraversalEnabled",
        ):
            self.assertIn(stat, cira_hh)

    def test_csr_timing_port_is_device_side_of_cxl_not_in_host_l2(self):
        config = GAPBS_CONFIG.read_text(encoding="utf-8")
        self.assertIn("NoncoherentXBar", config)
        self.assertIn(
            "link.mem_side_port = device_xbar.cpu_side_ports", config
        )
        self.assertIn("device_xbar.mem_side_ports = port", config)
        self.assertIn(
            "cira.csr_mem_side_port = "
            "self._cxl_device_xbars[0].cpu_side_ports",
            config,
        )
        connect_body = config[
            config.index("def _connect_things"):config.index("parser =")
        ]
        self.assertNotIn(
            "cira.csr_mem_side_port = l2bus", connect_body
        )
        self.assertNotIn(
            "cira.csr_mem_side_port = "
            "self.cache_hierarchy.get_cpu_side_port()",
            connect_body,
        )

    def test_cira_filters_lines_already_present_or_pending_in_target_l2(self):
        source = CIRA_CC.read_text(encoding="utf-8")
        issue = source[
            source.index("CIRA::issuePrefetch(") : source.index(
                "CIRA::issueIndexedPrefetch("
            )
        ]
        self.assertIn("targetCaches.at(targetCore)->inCache", issue)
        self.assertIn("targetCaches.at(targetCore)->inMissQueue", issue)

    def test_se_proxy_reads_coherent_dirty_guest_memory(self):
        source = TRANSLATING_PORT_PROXY_CC.read_text(encoding="utf-8")
        constructor = source[
            source.index("TranslatingPortProxy::TranslatingPortProxy(") :
            source.index("bool\nTranslatingPortProxy::tryOnBlob")
        ]
        self.assertIn(
            "makeFunctionalAccess(tc, bypass_caches)",
            constructor,
        )
        self.assertIn("tc->sendFunctional(&coherent_pkt)", source)
        self.assertIn("getPhysMem().functionalAccess(pkt)", source)
        self.assertIn("if (bypassCaches)", source)
        self.assertIn("if (pkt->isWrite())", source)
        self.assertNotIn("getPhysMem().functionalAccess", constructor)
        process = PROCESS_CC.read_text(encoding="utf-8")
        self.assertIn(
            "tc, SETranslatingPortProxy::Always, 0, true",
            process,
        )

    def test_clone3_does_not_eagerly_read_pointer_arguments(self):
        source = SYSCALL_EMUL_HH.read_text(encoding="utf-8")
        clone3 = source[
            source.index("clone3Func(") : source.index(
                "cloneFunc(", source.index("clone3Func(")
            )
        ]
        self.assertIn("VPtr<> ptidPtr", clone3)
        self.assertIn("VPtr<> ctidPtr", clone3)
        self.assertIn("VPtr<> tlsPtr", clone3)
        self.assertNotIn("VPtr<uint64_t>", clone3)

    def test_timing_se_stores_update_the_syscall_shadow(self):
        source = TIMING_SIMPLE_CC.read_text(encoding="utf-8")
        handle_write = source[
            source.index("TimingSimpleCPU::handleWritePacket()") :
            source.index("TimingSimpleCPU::writeMem(")
        ]
        self.assertIn("SE syscall shadow", handle_write)
        self.assertIn("getPhysMem().functionalAccess(&shadow_pkt)", handle_write)
        self.assertIn("!FullSystem", handle_write)
        self.assertIn("!req->isSwap()", handle_write)

    def test_no_data_writes_accept_address_check_requests_only(self):
        for path in (ATOMIC_SIMPLE_CC, TIMING_SIMPLE_CC):
            source = path.read_text(encoding="utf-8")
            write_mem = source[source.index("::writeMem("):]
            null_data = write_mem[
                write_mem.index("if (data == NULL)"):
                write_mem.index("}", write_mem.index("if (data == NULL)"))
            ]
            self.assertIn("Request::STORE_NO_DATA", null_data)
            self.assertIn("Request::NO_ACCESS", null_data)

    def test_transition_machine(self):
        compiler = shutil.which("g++") or shutil.which("c++")
        self.assertIsNotNone(compiler, "a C++ compiler is required")

        program = textwrap.dedent(
            r"""
            #include <cassert>
            #include <cstdint>

            #include "mem/cira_usefulness_tracker.hh"

            using gem5::CiraLineUsefulnessTracker;

            int
            main()
            {
                using Attribution =
                    CiraLineUsefulnessTracker::DemandAttribution;

                CiraLineUsefulnessTracker tracker(64);

                // Addresses are attributed at cacheline granularity.
                tracker.issue(0x1003);
                tracker.fill(0x103f, true);
                assert(tracker.demand(0x1010, true) == Attribution::Useful);
                assert(tracker.demand(0x1018, true) == Attribution::None);

                // A demand before fill is late and suppresses the later fill.
                tracker.issue(0x2000);
                assert(tracker.demand(0x203f, false) == Attribution::Late);
                assert(tracker.demand(0x2008, false) == Attribution::None);
                tracker.fill(0x2010, true);
                assert(tracker.demand(0x2020, true) == Attribution::None);

                // Duplicate issues collapse to one physical-fill token.
                tracker.issue(0x3000);
                tracker.issue(0x3030);
                assert(tracker.outstandingRefs(0x3001) == 2);
                tracker.fill(0x3008, true);
                assert(tracker.outstandingRefs(0x3038) == 0);
                assert(tracker.demand(0x3018, true) == Attribution::Useful);
                assert(tracker.demand(0x3028, true) == Attribution::None);

                // One late demand consumes all duplicate outstanding refs.
                tracker.issue(0x4000);
                tracker.issue(0x4038);
                assert(tracker.demand(0x4010, false) == Attribution::Late);
                assert(tracker.outstandingRefs(0x4020) == 0);
                tracker.fill(0x4000, true);
                assert(tracker.demand(0x4030, true) == Attribution::None);

                // A fill owned by another requestor resolves but cannot credit
                // an outstanding CIRA issue.
                tracker.issue(0x5000);
                tracker.fill(0x5030, false);
                assert(tracker.demand(0x5010, true) == Attribution::None);

                // A CIRA access that hits a resident line retires only its
                // corresponding issue and never creates a completed token.
                tracker.issue(0x6000);
                tracker.issue(0x6030);
                tracker.prefetchHit(0x6010);
                assert(tracker.outstandingRefs(0x6020) == 1);
                tracker.prefetchHit(0x6038);
                assert(tracker.outstandingRefs(0x6008) == 0);
                assert(tracker.demand(0x6000, true) == Attribution::None);

                // A late demand may install the line before the delayed CIRA
                // access reaches L2. Its later Hit resolves the suppressed
                // generation and must not poison a future issue/fill.
                tracker.issue(0x6800);
                assert(tracker.demand(0x6810, false) == Attribution::Late);
                tracker.prefetchHit(0x6820);
                tracker.issue(0x6830);
                tracker.fill(0x6808, true);
                assert(tracker.demand(0x6818, true) == Attribution::Useful);

                // If completed and outstanding state coexist, the demand is
                // useful only. It also clears stale same-line outstanding
                // refs so a later demand cannot be misclassified as late.
                tracker.issue(0x7000);
                tracker.fill(0x7000, true);
                tracker.issue(0x7010);
                assert(tracker.demand(0x7020, true) == Attribution::Useful);
                assert(tracker.outstandingRefs(0x7030) == 0);
                assert(tracker.demand(0x7008, true) == Attribution::None);

                // A completed token is stale when the CPU misses: the line
                // filled by CIRA is no longer serving this demand.
                tracker.issue(0x7800);
                tracker.fill(0x7800, true);
                assert(tracker.demand(0x7810, false) == Attribution::None);
                assert(tracker.demand(0x7820, true) == Attribution::None);

                // An outstanding issue is unnecessary rather than late when
                // the CPU already hits the resident line.
                tracker.issue(0x7c00);
                assert(tracker.demand(0x7c10, true) == Attribution::None);
                assert(tracker.outstandingRefs(0x7c20) == 0);
                tracker.fill(0x7c30, true);
                assert(tracker.demand(0x7c08, true) == Attribution::None);

                // On a CPU miss, a stale completed token cannot hide a newer
                // outstanding CIRA generation: count one Late and suppress
                // the later CIRA fill.
                tracker.issue(0x8000);
                tracker.fill(0x8000, true);
                tracker.issue(0x8010);
                assert(tracker.demand(0x8020, false) == Attribution::Late);
                assert(tracker.outstandingRefs(0x8030) == 0);
                tracker.fill(0x8008, true);
                assert(tracker.demand(0x8018, true) == Attribution::None);

                tracker.issue(0x8800);
                tracker.clear();
                assert(tracker.outstandingRefs(0x8800) == 0);
                assert(tracker.demand(0x8800, false) == Attribution::None);

                // Coalescing accepts one request per tracked cacheline. A
                // bounded completed history retires the oldest undemanded
                // line without confusing newer generations of that line.
                CiraLineUsefulnessTracker bounded(64, 2);
                assert(!bounded.tracked(0x9000));
                assert(bounded.issueIfAbsent(0x9000));
                assert(!bounded.issueIfAbsent(0x903f));
                bounded.fill(0x9008, true);
                assert(bounded.tracked(0x9010));

                assert(bounded.issueIfAbsent(0x9100));
                bounded.fill(0x9100, true);
                assert(bounded.issueIfAbsent(0x9200));
                bounded.fill(0x9200, true);
                assert(!bounded.tracked(0x9000));
                assert(bounded.tracked(0x9100));
                assert(bounded.tracked(0x9200));

                assert(bounded.demand(0x9110, true) ==
                       Attribution::Useful);
                assert(!bounded.tracked(0x9100));
                assert(bounded.issueIfAbsent(0x9130));
                return 0;
            }
            """
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cira_usefulness_contract.cc"
            binary = Path(tmp) / "cira_usefulness_contract"
            source.write_text(program, encoding="utf-8")
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(REPO / "src"),
                    str(source),
                    "-o",
                    str(binary),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [str(binary)], capture_output=True, text=True
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )

    def test_cira_probe_and_stats_integration_contract(self):
        cira_py = CIRA_PY.read_text(encoding="utf-8")
        cira_hh = CIRA_HH.read_text(encoding="utf-8")
        cira_cc = CIRA_CC.read_text(encoding="utf-8")
        config = GAPBS_CONFIG.read_text(encoding="utf-8")

        self.assertIn("mem_side_ports = VectorRequestPort(", cira_py)
        self.assertIn(
            "demand_probe_targets = VectorParam.SimObject(", cira_py
        )
        self.assertNotIn("demand_probe_target = Param.SimObject(", cira_py)
        self.assertIn("max_csr_walk_queue = Param.Unsigned(", cira_py)
        self.assertIn("csr_lines_per_turn = Param.Unsigned(", cira_py)
        self.assertIn("max_completed_lines = Param.Unsigned(", cira_py)
        self.assertIn("PortID targetCore", cira_hh)
        self.assertIn(
            "std::vector<std::unique_ptr<MemoryPort>> memSidePorts", cira_hh
        )
        self.assertIn(
            "std::vector<CiraLineUsefulnessTracker> lineTrackers", cira_hh
        )
        self.assertIn(
            "std::vector<std::deque<CsrWalkState>> csrWalkQueues", cira_hh
        )
        self.assertIn("const uint64_t maxCsrWalkQueue", cira_hh)
        self.assertIn("const uint64_t csrLinesPerTurn", cira_hh)
        self.assertIn("PortID nextCsrCore", cira_hh)
        self.assertIn("queuedCsrWalks() const", cira_hh)
        self.assertIn("droppedCsrDescriptors", cira_hh)
        self.assertIn("csrQueueHighWatermark", cira_hh)
        self.assertIn("issuedCsrPrefetchesPerCore", cira_hh)
        self.assertIn("resolveTargetCore(ThreadContext *tc)", cira_hh)
        self.assertIn(
            "p.port_mem_side_ports_connection_count", cira_cc
        )
        self.assertIn("p.demand_probe_targets", cira_cc)
        self.assertIn(
            "lineTrackers.at(targetCore).issueIfAbsent", cira_cc
        )
        capacity_check = cira_cc.index(
            "queuedCsrWalks() >= maxCsrWalkQueue"
        )
        record_span_validation = cira_cc.index(
            "csr invalid record span descriptor"
        )
        accepted_descriptor = cira_cc.index(
            "++stats.issuedCsrPrefetches", capacity_check
        )
        queue_insert = cira_cc.index(
            "csrWalkQueues[targetCore].push_back(walk)"
        )
        self.assertLess(record_span_validation, capacity_check)
        self.assertLess(capacity_check, accepted_descriptor)
        self.assertLess(capacity_check, queue_insert)
        self.assertIn("++stats.droppedCsrDescriptors", cira_cc)
        self.assertIn("stats.csrQueueHighWatermark", cira_cc)
        self.assertIn("const PortID startCore = nextCsrCore", cira_cc)
        self.assertIn("candidatesThisTurn < csrLinesPerTurn", cira_cc)
        self.assertIn("nextCsrCore = (targetCore + 1)", cira_cc)
        self.assertIn("usefulPrefetches", cira_hh)
        self.assertIn("latePrefetches", cira_hh)
        self.assertIn('"Hit"', cira_cc)
        self.assertIn('"Miss"', cira_cc)
        self.assertIn('"Fill"', cira_cc)
        self.assertIn(
            "event == CacheProbeEvent::Hit", cira_cc[
                cira_cc.index("lineTracker.demand") - 160:
                cira_cc.index("lineTracker.demand") + 160
            ]
        )
        self.assertIn("resetStats()", cira_hh)
        self.assertIn("tracker.clear()", cira_cc)
        self.assertIn(
            "taskId() != context_switch_task_id::Prefetcher", cira_cc
        )
        self.assertIn("getRequestorName", cira_cc)
        self.assertIn('".data"', cira_cc)
        self.assertIn(
            "for idx, l2bus in enumerate(self.cache_hierarchy.l2buses):",
            config,
        )
        self.assertIn(
            "cira.mem_side_ports = l2bus.cpu_side_ports", config
        )
        self.assertIn("cira.demand_probe_targets = [", config)
        connect_body = config[
            config.index("def _connect_things"):config.index("parser =")
        ]
        self.assertNotIn('"l2-cache-0"', connect_body)

        recv_response = cira_cc[
            cira_cc.index("CIRA::recvTimingResp"):
            cira_cc.index("CIRA::recvReqRetry")
        ]
        self.assertNotIn("lineTracker.fill", recv_response)

    def test_live_four_core_timing_csr_and_dirty_value_coherence(self):
        self.assertTrue(CIRA_MULTICORE_WORKLOAD.is_file())
        self.assertTrue(CIRA_MULTICORE_CONFIG.is_file())

        gem5 = REPO / "build" / "X86" / "gem5.opt"
        if not gem5.is_file():
            self.skipTest(f"{gem5} is not built")

        m5_library = (
            REPO / "util" / "m5" / "build" / "x86" / "out" / "libm5.a"
        )
        if not m5_library.is_file():
            subprocess.run(
                [
                    "scons",
                    "-C",
                    str(REPO / "util" / "m5"),
                    "build/x86/out/libm5.a",
                ],
                cwd=REPO,
                check=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            binary = tmp_path / "cira_multicore_prefetch"
            outdir = tmp_path / "m5out"
            compile_result = subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-static",
                    "-no-pie",
                    "-I",
                    str(REPO / "include"),
                    "-I",
                    str(REPO / "util" / "cira"),
                    str(CIRA_MULTICORE_WORKLOAD),
                    str(m5_library),
                    "-o",
                    str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [
                    str(gem5),
                    "--outdir",
                    str(outdir),
                    str(CIRA_MULTICORE_CONFIG),
                    "--binary",
                    str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )

            stats = {}
            for line in (outdir / "stats.txt").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    stats[fields[0]] = fields[1]

            issued = [
                int(stats[f"board.cira.issuedPrefetchesPerCore::{core}"])
                for core in range(4)
            ]
            completed = [
                int(stats[f"board.cira.completedPrefetchesPerCore::{core}"])
                for core in range(4)
            ]
            self.assertTrue(all(value > 0 for value in issued), issued)
            self.assertEqual(completed, issued)
            self.assertEqual(
                int(stats["board.cira.csrIndexReadPackets"]),
                4 * 256,
            )
            self.assertEqual(
                int(stats["board.cira.csrIndexReadBytes"]),
                4 * 256 * 4,
            )
            self.assertEqual(
                int(stats["board.cira.completedCsrIndexReads"]),
                4 * 256,
            )
            self.assertEqual(
                int(stats["board.cira.rejectedCsrIndexQueueFull"]), 0
            )
            self.assertEqual(
                int(stats["board.cira.timingCsrTraversalEnabled"]), 1
            )
            self.assertEqual(
                int(stats["board.cira.droppedCsrDescriptors"]), 0
            )

    def test_live_four_core_pr_rows_are_bit_exact_and_drained(self):
        gem5 = REPO / "build" / "X86" / "gem5.opt"
        if not gem5.is_file():
            self.skipTest(f"{gem5} is not built")
        self.assertTrue(CIRA_PR_WORKLOAD.is_file())
        m5_library = (
            REPO / "util" / "m5" / "build" / "x86" / "out" / "libm5.a"
        )
        self.assertTrue(m5_library.is_file())

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            binary = tmp_path / "cira_pr_rows_smoke"
            outdir = tmp_path / "m5out"
            compile_result = subprocess.run(
                [
                    "g++", "-std=c++17", "-O2", "-Wall", "-Wextra",
                    "-static", "-no-pie", "-ffp-contract=off",
                    "-fno-fast-math", "-I", str(REPO / "include"),
                    "-I", str(REPO / "util" / "cira"),
                    str(CIRA_PR_WORKLOAD), str(m5_library),
                    "-o", str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            run_result = subprocess.run(
                [
                    str(gem5), "--outdir", str(outdir),
                    str(CIRA_MULTICORE_CONFIG), "--binary", str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )
            self.assertIn("Verification: PASS", run_result.stdout)

            stats = {}
            for line in (outdir / "stats.txt").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    stats[fields[0]] = fields[1]
            self.assertEqual(int(stats["board.cira.issuedPrDescriptors"]), 8)
            self.assertEqual(
                int(stats["board.cira.completedPrDescriptors"]), 8
            )
            self.assertEqual(int(stats["board.cira.rejectedPrDescriptors"]), 0)
            self.assertEqual(int(stats["board.cira.prRows"]), 16)
            self.assertGreater(int(stats["board.cira.prCsrReads"]), 0)
            self.assertGreater(int(stats["board.cira.prCoherentReads"]), 0)
            self.assertEqual(int(stats["board.cira.prCoherentWrites"]), 16)
            self.assertEqual(
                int(stats["board.cira.issuedPrReconfigurations"]), 4
            )
            self.assertEqual(
                int(stats["board.cira.completedPrReconfigurations"]), 4
            )
            self.assertEqual(int(stats["board.cira.prOutstandingWork"]), 0)
            for core in range(4):
                self.assertEqual(
                    int(stats[
                        f"board.cira.issuedPrDescriptorsPerCore::{core}"
                    ]),
                    2,
                )
                self.assertEqual(
                    int(stats[
                        f"board.cira.completedPrDescriptorsPerCore::{core}"
                    ]),
                    2,
                )


if __name__ == "__main__":
    unittest.main()
