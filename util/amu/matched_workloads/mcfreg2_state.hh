/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_STATE_HH__
#define __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_STATE_HH__

#include "mcfreg2_format.h"

#include <cstdint>
#include <set>
#include <string>
#include <variant>
#include <vector>

namespace mcfreg2
{

enum class RecordRole : uint8_t
{
    LiveIn = MCFREG2_ROLE_LIVE_IN,
    ObservedResult = MCFREG2_ROLE_OBSERVED_RESULT,
};

enum class RecordKind : uint8_t
{
    CallBegin = MCFREG2_CALL_BEGIN,
    PricingScanLiveIn = MCFREG2_PRICING_SCAN_LIVE_IN,
    PricingCandidateObserved = MCFREG2_PRICING_CANDIDATE_OBSERVED,
    BasketLiveIn = MCFREG2_BASKET_LIVE_IN,
    BasketLiveOutObserved = MCFREG2_BASKET_LIVE_OUT_OBSERVED,
    PricingEndObserved = MCFREG2_PRICING_END_OBSERVED,
    PriceOutStateLiveIn = MCFREG2_PRICE_OUT_STATE_LIVE_IN,
    PriceOutCandidateObserved = MCFREG2_PRICE_OUT_CANDIDATE_OBSERVED,
    PriceOutDecisionObserved = MCFREG2_PRICE_OUT_DECISION_OBSERVED,
    ArcFinalObserved = MCFREG2_ARC_FINAL_OBSERVED,
    RemapObserved = MCFREG2_REMAP_OBSERVED,
    AdjacencyFinalObserved = MCFREG2_ADJACENCY_FINAL_OBSERVED,
    PriceOutEndObserved = MCFREG2_PRICE_OUT_END_OBSERVED,
    CallEnd = MCFREG2_CALL_END,
};

struct BasketState
{
    uint64_t slot = 0;
    McfStableRef arc{};
    int64_t cost = 0;
    int64_t absCost = 0;
};

struct PricingScanLiveIn
{
    uint64_t scanPosition = 0;
    McfStableRef arc{};
    McfStableRef tail{};
    McfStableRef head{};
    int64_t cost = 0;
    int64_t ident = 0;
    int64_t tailPotential = 0;
    int64_t headPotential = 0;
};

struct PricingCandidate
{
    uint64_t scanPosition = 0;
    int64_t reducedCost = 0;
    bool candidate = false;
    int64_t basketSlot = -1;
};

struct PricingLiveIn
{
    uint64_t ordinal = 0;
    uint64_t m = 0;
    uint64_t nrGroup = 0;
    uint64_t groupPos = 0;
    bool initialize = false;
    std::vector<BasketState> basket;
    std::vector<PricingScanLiveIn> scans;
};

struct PricingDerivedOut
{
    uint64_t ordinal = 0;
    std::vector<PricingCandidate> candidates;
    std::vector<BasketState> basket;
    McfStableRef selectedArc{};
    int64_t selectedReducedCost = 0;
    uint64_t arcsPriced = 0;
    uint64_t nrGroup = 0;
    uint64_t groupPos = 0;
    bool initialize = false;
};

struct ObjectState
{
    McfStableRef reference{};
    std::vector<int64_t> words;
    std::vector<McfStableRef> links;
};

struct PriceOutCandidate
{
    uint64_t candidate = 0;
    McfStableRef tail{};
    McfStableRef head{};
    int64_t cost = 0;
    int64_t reducedCost = 0;
};

enum class PriceOutDecisionKind : uint8_t
{
    NoChange = 0,
    Insert = 1,
    Replace = 2,
};

struct PriceOutDecision
{
    uint64_t candidate = 0;
    PriceOutDecisionKind decision = PriceOutDecisionKind::NoChange;
    McfStableRef reference{};
};

struct PriceOutLiveIn
{
    uint64_t ordinal = 0;
    std::vector<int64_t> networkWords;
    std::vector<ObjectState> objects;
    uint32_t arenaGeneration = 0;
    uint64_t arenaCapacity = 0;
    std::vector<McfStableRef> heap;
};

struct PriceOutDerivedOut
{
    uint64_t ordinal = 0;
    std::vector<int64_t> networkWords;
    std::vector<ObjectState> objects;
    uint32_t arenaGeneration = 0;
    uint64_t arenaCapacity = 0;
    std::vector<McfStableRef> heap;
    std::vector<PriceOutCandidate> candidates;
    std::vector<PriceOutDecision> decisions;
};

using CanonicalCallState = std::variant<
    PricingLiveIn, PricingDerivedOut, PriceOutLiveIn, PriceOutDerivedOut>;

std::vector<uint8_t> encodeCallState(const CanonicalCallState &state);
std::string digestCallState(const CanonicalCallState &state);

class CallFrameReader
{
  public:
    void begin(uint64_t call, uint64_t order, const std::string &phase);
    void accept(RecordKind kind, RecordRole role);
    void end(uint64_t call, uint64_t order, const std::string &phase);

    bool active() const { return isActive; }
    const std::set<RecordKind> &liveInKinds() const { return liveIns; }
    const std::set<RecordKind> &observedKinds() const { return observed; }

  private:
    bool isActive = false;
    uint64_t activeCall = 0;
    uint64_t activeOrder = 0;
    std::string activePhase;
    std::set<RecordKind> liveIns;
    std::set<RecordKind> observed;
};

} // namespace mcfreg2

#endif // __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_STATE_HH__
