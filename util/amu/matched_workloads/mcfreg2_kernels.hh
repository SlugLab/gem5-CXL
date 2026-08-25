/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_KERNELS_HH__
#define __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_KERNELS_HH__

#include "canonical_trace.hh"
#include "mcfreg2_state.hh"

#include <cstdint>

namespace mcfreg2
{

class KernelTraceSink
{
  public:
    virtual ~KernelTraceSink() = default;
    virtual void emit(
        uint16_t phase, matched_trace::Opcode opcode, uint64_t workItem,
        uint64_t address, uint64_t operand0, uint64_t operand1,
        uint64_t result) = 0;
};

PricingDerivedOut replayPricing(
    const PricingLiveIn &liveIn, KernelTraceSink &trace);

} // namespace mcfreg2

#endif // __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_KERNELS_HH__
