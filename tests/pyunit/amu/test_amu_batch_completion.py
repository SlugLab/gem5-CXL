# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
OPS = (REPO / "include/gem5/asm/generic/m5ops.h").read_text()
API = (REPO / "include/gem5/m5ops.h").read_text()
PSEUDO_HH = (REPO / "src/sim/pseudo_inst.hh").read_text()
PSEUDO_CC = (REPO / "src/sim/pseudo_inst.cc").read_text()
ASMC_HH = (REPO / "src/mem/asmc.hh").read_text()
ASMC_CC = (REPO / "src/mem/asmc.cc").read_text()
X86_BASIC = (REPO / "src/arch/x86/isa/formats/basic.isa").read_text()
X86_DECODER = (
    REPO / "src/arch/x86/isa/decoder/two_byte_opcodes.isa"
).read_text()
X86_INCLUDES = (REPO / "src/arch/x86/isa/includes.isa").read_text()
PROFILE = (REPO / "util/amu/amu_paper_profile.cc").read_text()
SMOKE = (
    REPO / "tests/test-progs/amu-smoke/amu_batch_smoke.c"
).read_text()


class AmuBatchCompletionTest(unittest.TestCase):
    def test_batch_opcode_is_unique_and_exported_by_libm5(self):
        self.assertIn("M5OP_AMU_GETFIN_BATCH", OPS)
        values = re.findall(r"^#define\s+M5OP_[A-Z0-9_]+\s+(0x[0-9a-fA-F]+)",
                            OPS, re.MULTILINE)
        self.assertEqual(len(values), len(set(values)))
        self.assertIn("M5OP(m5_amu_getfin_batch, M5OP_AMU_GETFIN_BATCH)", OPS)
        self.assertIn("M5OP_AMU_WAITFIN", OPS)
        self.assertIn("M5OP(m5_amu_waitfin, M5OP_AMU_WAITFIN)", OPS)
        self.assertIn("uint64_t m5_amu_getfin_batch", API)
        self.assertIn("void m5_amu_waitfin", API)

    def test_pseudo_inst_drains_fifo_into_register_tokens(self):
        self.assertIn("amuGetfinBatch", PSEUDO_HH)
        self.assertIn("case M5OP_AMU_GETFIN_BATCH", PSEUDO_HH)
        body = PSEUDO_CC[
            PSEUDO_CC.index("amuGetfinBatch"):
            PSEUDO_CC.index("amuCfgwr")
        ]
        self.assertIn("asmc->getFinished(tc)", body)
        self.assertIn("constexpr unsigned tokenBits = 15", body)
        self.assertIn("constexpr unsigned batchSize = 4", body)
        self.assertIn("packed |= (id & tokenMask)", body)
        self.assertIn("return packed | count", body)
        self.assertNotIn("writeBlob", body)

    def test_profile_consumes_completion_batches_but_checks_every_id(self):
        self.assertIn("constexpr size_t kCompletionBatch", PROFILE)
        self.assertIn("profileGetfinBatch", PROFILE)
        helper = PROFILE[
            PROFILE.index("profileGetfinBatch"):
            PROFILE.index("class PersistentScheduler")
        ]
        self.assertIn("m5_amu_getfin_batch()", helper)
        scheduler = PROFILE[
            PROFILE.index("class PersistentScheduler"):
            PROFILE.index("void\nprimeSpm")
        ]
        self.assertIn("waitCompletionBatch", scheduler)
        self.assertIn("waitCompletionOwners", scheduler)
        self.assertIn("findOwnerToken(token)", scheduler)
        self.assertIn("ownerWords[owner_index]", scheduler)
        self.assertIn("(entry.id & kCompletionTokenMask) != token", scheduler)
        self.assertIn("entry.phase != expected", scheduler)
        self.assertNotIn("profileGetfin()", scheduler)

    def test_profile_owner_lookup_is_direct_and_packed(self):
        scheduler = PROFILE[
            PROFILE.index("class PersistentScheduler"):
            PROFILE.index("void\nprimeSpm")
        ]
        lookup = scheduler[
            scheduler.index("size_t findOwnerToken"):
            scheduler.index("void registerId")
        ]
        self.assertIn(
            "constexpr size_t kOwnerEntries = 1 << kCompletionTokenBits",
            PROFILE,
        )
        self.assertIn("ownerWords[token] & kOwnerLive", lookup)
        self.assertNotIn("for (", lookup)
        self.assertIn("duplicate live token", scheduler)
        self.assertNotIn("overflowOwners", scheduler)
        self.assertNotIn("overflowCounts", scheduler)

    def test_completion_slot_buffer_is_not_needlessly_zeroed(self):
        self.assertEqual(
            PROFILE.count(
                "std::array<size_t, kCompletionBatch> completedSlots;"
            ),
            2,
        )
        self.assertEqual(
            PROFILE.count(
                "std::array<uint32_t, kCompletionBatch> completedOwners;"
            ),
            2,
        )
        self.assertNotIn(
            "std::array<size_t, kCompletionBatch> completedSlots{}", PROFILE
        )
        self.assertNotIn(
            "std::array<uint32_t, kCompletionBatch> completedOwners{}", PROFILE
        )

    def test_o3_smoke_checks_every_packed_token_and_spm_value(self):
        self.assertIn("m5_amu_getfin_batch", SMOKE)
        self.assertIn("CompletionTokenBits = 15", SMOKE)
        self.assertIn("const uint64_t token", SMOKE)
        self.assertIn("seen[owner]", SMOKE)
        self.assertIn("spm[index].value != source[index].value", SMOKE)

    def test_empty_batch_quiesces_until_asmc_publishes_a_completion(self):
        self.assertIn("quiesceUntilCompletion", ASMC_HH)
        self.assertIn("completionWaiters", ASMC_HH)
        self.assertIn("tc->quiesce()", ASMC_CC)
        self.assertIn("tc->activate()", ASMC_CC)
        body = PSEUDO_CC[
            PSEUDO_CC.index("amuGetfinBatch"):
            PSEUDO_CC.index("amuCfgwr")
        ]
        self.assertIn("asmc->quiesceUntilCompletion(tc)", body)
        self.assertNotIn("quiesceUntilCompletion", body[:body.index("amuWaitfin")])
        self.assertIn("profileWaitfin", PROFILE)
        self.assertIn("m5_amu_waitfin", SMOKE)
        self.assertIn("AmuWaitOperate::gem5Op", X86_DECODER)
        self.assertIn("def format AmuWaitOperate", X86_BASIC)
        self.assertIn("machInst.immediate == M5OP_AMU_WAITFIN", X86_BASIC)
        self.assertIn("flags[IsQuiesce] = true", X86_BASIC)
        self.assertIn("<gem5/asm/generic/m5ops.h>", X86_INCLUDES)
        reset = PSEUDO_CC[
            PSEUDO_CC.index("case 3:", PSEUDO_CC.index("amuCfgwr")):
            PSEUDO_CC.index("default:", PSEUDO_CC.index("amuCfgwr"))
        ]
        self.assertIn("amuBatchWaiters.erase(tc)", reset)
        self.assertIn("tc->activate()", reset)

    def test_waitfin_wakes_on_full_batch_or_final_tail(self):
        complete = ASMC_CC[
            ASMC_CC.index("ASMC::completeRequest"):
            ASMC_CC.index("ASMC::updateOccupancyIntegral")
        ]
        self.assertIn("constexpr size_t completionWakeBatch = 4", complete)
        self.assertIn("queue.size() >= completionWakeBatch", complete)
        self.assertIn("!has_outstanding", complete)
        self.assertLess(complete.index("outstanding.erase(it)"),
                        complete.index("completionWaiters.erase(tc)"))
        fallback = PSEUDO_CC[
            PSEUDO_CC.index("completeAmuRequest"):
            PSEUDO_CC.index("issueAmuRequest")
        ]
        self.assertIn("state.finished.size() >= 4", fallback)
        self.assertIn("state.outstanding.empty()", fallback)


if __name__ == "__main__":
    unittest.main()
