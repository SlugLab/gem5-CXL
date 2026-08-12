# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import random
import unittest
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SOURCE = (REPO / "util/amu/amu_paper_profile.cc").read_text(encoding="utf-8")
AMU_API = (REPO / "util/amu/amu.h").read_text(encoding="utf-8")
ASMC_SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")


@dataclass
class HostSlot:
    op: int
    request_id: int
    phase: str


class HostCompletionTable:
    """Host-only executable model of the C++ ID owner/phase checks."""

    def __init__(self):
        self.slots = {}
        self.owners = {}

    def issue(self, slot, op, request_id, phase):
        self.slots[slot] = HostSlot(op, request_id, phase)
        self.owners[request_id] = (slot, phase)

    def complete(self, request_id, *, reported_slot=None, reported_phase=None):
        if request_id not in self.owners:
            raise RuntimeError("unknown or stale completion")
        slot, expected = self.owners.pop(request_id)
        state = self.slots[slot]
        if reported_slot is not None and reported_slot != slot:
            raise RuntimeError("wrong owner")
        if reported_phase is not None and reported_phase != expected:
            raise RuntimeError("wrong phase")
        if state.request_id != request_id or state.phase != expected:
            raise RuntimeError("slot ownership mismatch")
        return slot, state.op


def run_host_pipeline(order):
    table = [0x9E3779B97F4A7C15 ^ index for index in range(64)]
    reference = list(table)
    for op in range(64):
        index = (op * 40503) & 63
        reference[index] ^= 0xD1B54A32D192ED03 ^ index

    completions = HostCompletionTable()
    staged = {}
    for op in range(64):
        completions.issue(op, op, op + 1, "LoadPending")
    for request_id in order:
        slot, op = completions.complete(request_id)
        index = (op * 40503) & 63
        staged[op] = (index, table[index] ^ 0xD1B54A32D192ED03 ^ index)
        completions.issue(slot, op, request_id + 1000, "StorePending")
    store_ids = [request_id + 1000 for request_id in order]
    random.Random(7).shuffle(store_ids)
    for request_id in store_ids:
        _, op = completions.complete(request_id)
        index, value = staged[op]
        table[index] = value
    return table, reference


class AmuPaperProfileSchedulerTest(unittest.TestCase):
    def test_multiline_window_is_bounded_by_route_packet_reservations(self):
        cache_line_bytes = 64
        far_queue_packets = 512
        spm_queue_packets = 512
        stream_bytes = 512
        maximum_far_packets = (
            stream_bytes + 2 * cache_line_bytes - 2
        ) // cache_line_bytes
        aligned_spm_packets = (
            stream_bytes + cache_line_bytes - 1
        ) // cache_line_bytes
        safe_slots = min(
            far_queue_packets // maximum_far_packets,
            spm_queue_packets // (2 * aligned_spm_packets),
        )
        self.assertEqual(maximum_far_packets, 9)
        self.assertEqual(aligned_spm_packets, 8)
        self.assertEqual(safe_slots, 32)

        for token in (
            "AMU_CFG_FAR_SEND_QUEUE_PACKETS = 7",
            "AMU_CFG_SPM_SEND_QUEUE_PACKETS = 8",
            "AMU_CFG_CACHE_LINE_BYTES = 9",
        ):
            self.assertIn(token, AMU_API)
        cfg_read = ASMC_SOURCE[
            ASMC_SOURCE.index("ASMC::cfgRead"):
            ASMC_SOURCE.index("ASMC::deleteQueuedPacket")
        ]
        self.assertIn("return maxSendQueue", cfg_read)
        self.assertIn("return spmSendQueueSize", cfg_read)
        self.assertIn("return cacheLineSize", cfg_read)

        self.assertIn("queueSafeSlotCount", SOURCE)
        scheduler = SOURCE[
            SOURCE.index("PersistentScheduler(size_t granularity)"):
            SOURCE.index("size_t capacity() const")
        ]
        self.assertIn("queueSafeSlots", scheduler)
        self.assertNotIn("queueSafeSlotCount", scheduler)
        queue_window = SOURCE[
            SOURCE.index("queueSafeSlotCount"):
            SOURCE.index("void\nflushRange")
        ]
        self.assertIn("AMU_CFG_FAR_SEND_QUEUE_PACKETS", queue_window)
        self.assertIn("AMU_CFG_SPM_SEND_QUEUE_PACKETS", queue_window)
        self.assertIn("AMU_CFG_CACHE_LINE_BYTES", queue_window)
        self.assertIn("cache_line_bytes != kCacheLineBytes", queue_window)
        self.assertIn("2 * spm_packets", queue_window)

        prepare = SOURCE[
            SOURCE.index("prepareAndPrime"):
            SOURCE.index("runGupsBaseline")
        ]
        self.assertIn(
            "queueSafeSlots = queueSafeSlotCount(workloadGranularity(options))",
            prepare,
        )
        main = SOURCE[SOURCE.index("main(int argc") :]
        self.assertLess(main.index("prepareAndPrime"), main.index("m5_work_begin"))

    def test_source_uses_one_persistent_aligned_arena_and_fixed_slots(self):
        self.assertIn("constexpr size_t kSpmBytes = 64 * 1024", SOURCE)
        self.assertIn("constexpr size_t kWindowSlots = 256", SOURCE)
        self.assertIn("alignas(64) std::array<uint8_t, kSpmBytes>", SOURCE)
        self.assertIn("std::array<Slot, kWindowSlots>", SOURCE)

    def test_id_dispatch_is_constant_time_and_phase_checked(self):
        self.assertIn("enum class SlotPhase", SOURCE)
        self.assertIn("LoadPending", SOURCE)
        self.assertIn("ReadyToStore", SOURCE)
        self.assertIn("StorePending", SOURCE)
        self.assertIn("struct IdOwner", SOURCE)
        self.assertIn("constexpr size_t kOwnerSets = 128", SOURCE)
        self.assertIn("constexpr size_t kOwnerWays = 4", SOURCE)
        self.assertIn("constexpr size_t kOwnerEntries", SOURCE)
        self.assertIn("std::array<IdOwner, kOwnerEntries> idOwners", SOURCE)
        self.assertIn("findOwner", SOURCE)
        self.assertIn("eraseOwner", SOURCE)
        self.assertIn("insertOwner", SOURCE)
        self.assertNotIn("std::vector<IdOwner> idOwners", SOURCE)
        self.assertNotIn("std::unordered_map<uint64_t, IdOwner>", SOURCE)
        self.assertNotIn("idOwners.find", SOURCE)
        self.assertNotIn("idOwners.erase", SOURCE)
        self.assertNotIn("overflowOwners", SOURCE)
        self.assertNotIn("overflowCounts", SOURCE)
        self.assertNotIn("std::find", SOURCE)
        self.assertIn("expected != entry.phase", SOURCE)

    def test_slots_refill_before_waiting_for_another_completion(self):
        completion = SOURCE[
            SOURCE.index("runGupsAmu"):
            SOURCE.index("runHashJoinAmu")
        ]
        self.assertIn("refillGupsSlot", completion)
        refill = completion.index("refillGupsSlot")
        next_wait = completion.find("waitCompletion", refill)
        self.assertGreater(next_wait, refill)

    def test_store_transition_uses_non_speculative_inline_m5op_ordering(self):
        issue_store = SOURCE[
            SOURCE.index("void issueStore"):
            SOURCE.index("size_t waitCompletion")
        ]
        self.assertIn("profileAstore", issue_store)
        self.assertNotIn("_mm_sfence()", issue_store)
        self.assertNotIn("_mm_mfence()", issue_store)
        profile_store = SOURCE[
            SOURCE.index("profileAstore"):
            SOURCE.index("profileGetfin")
        ]
        self.assertIn('asm volatile(".byte 0x0f, 0x04', profile_store)
        self.assertIn(': "memory")', profile_store)

    def test_roi_m5ops_are_inlined_without_call_return_wrappers(self):
        for helper in ("profileAload", "profileAstore", "profileGetfin"):
            self.assertIn(helper, SOURCE)
        self.assertIn('asm volatile(".byte 0x0f, 0x04', SOURCE)
        scheduler = SOURCE[
            SOURCE.index("class PersistentScheduler"):
            SOURCE.index("void\nprimeSpm")
        ]
        self.assertIn("profileAload", scheduler)
        self.assertIn("profileAstore", scheduler)
        self.assertIn("profileGetfin", scheduler)
        self.assertNotIn("amu_getfin()", scheduler)

    def test_priming_precedes_roi_and_checksum_follows_roi(self):
        main = SOURCE[SOURCE.index("main(int argc") :]
        self.assertIn("prepareAndPrime", main)
        self.assertLess(main.index("prepareAndPrime"), main.index("m5_work_begin"))
        self.assertLess(main.index("m5_work_end"), main.index("checksum("))
        before_roi = main[: main.index("m5_work_begin")]
        self.assertIn("primeSpm", SOURCE)
        self.assertIn("prepareAndPrime", before_roi)
        self.assertIn("AMU_CFG_OUTSTANDING", SOURCE)

    def test_far_working_sets_are_flushed_before_spm_priming_and_roi(self):
        prepare = SOURCE[
            SOURCE.index("prepareAndPrime"):
            SOURCE.index("runGupsBaseline")
        ]
        self.assertIn("flushFarWorkingSet", prepare)
        self.assertLess(
            prepare.index("flushFarWorkingSet"), prepare.index("primeSpm")
        )
        flush = SOURCE[
            SOURCE.index("flushFarWorkingSet"):
            SOURCE.index("prepareAndPrime")
        ]
        for member in (
            "state.gupsTable", "state.hashNodes", "state.streamA",
            "state.streamB", "state.streamC",
        ):
            self.assertIn(member, flush)

    def test_checksum_uses_a_tagged_register_transport(self):
        main = SOURCE[SOURCE.index("uint64_t digest = checksum") :]
        self.assertIn("m5_sum", main)
        self.assertIn("kChecksumMagic", main)
        self.assertIn("workloadTag(options)", main)
        self.assertLess(main.index("m5_sum"), main.index("m5_exit"))

    def test_reverse_and_shuffled_completions_match_scalar_image(self):
        forward, reference = run_host_pipeline(list(range(1, 65)))
        reverse, reverse_reference = run_host_pipeline(list(range(64, 0, -1)))
        shuffled_ids = list(range(1, 65))
        random.Random(11).shuffle(shuffled_ids)
        shuffled, shuffled_reference = run_host_pipeline(shuffled_ids)
        self.assertEqual(forward, reference)
        self.assertEqual(reverse, reverse_reference)
        self.assertEqual(shuffled, shuffled_reference)
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, shuffled)

    def test_host_model_rejects_wrong_owner_phase_and_stale_id(self):
        for mutation in ("owner", "phase", "stale"):
            with self.subTest(mutation=mutation):
                completions = HostCompletionTable()
                completions.issue(3, 9, 41, "LoadPending")
                with self.assertRaises(RuntimeError):
                    if mutation == "owner":
                        completions.complete(41, reported_slot=4)
                    elif mutation == "phase":
                        completions.complete(41, reported_phase="StorePending")
                    else:
                        completions.complete(99)


if __name__ == "__main__":
    unittest.main()
