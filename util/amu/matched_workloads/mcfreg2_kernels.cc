/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcfreg2_kernels.hh"

#include "mcfreg2.hh"

#include <algorithm>
#include <cstring>
#include <limits>

namespace mcfreg2
{
namespace
{

constexpr uint64_t BasketLimit = 50;
constexpr uint64_t GroupTarget = 300;
constexpr uint64_t ArcAddress = UINT64_C(0x800000000);
constexpr uint64_t PotentialAddress = UINT64_C(0x900000000);

uint64_t
bits(int64_t value)
{
    uint64_t result;
    static_assert(sizeof(result) == sizeof(value));
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

int64_t
signedBits(uint64_t value)
{
    int64_t result;
    static_assert(sizeof(result) == sizeof(value));
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

int64_t
wrappedAdd(int64_t left, int64_t right)
{
    return signedBits(bits(left) + bits(right));
}

int64_t
wrappedSubtract(int64_t left, int64_t right)
{
    return signedBits(bits(left) - bits(right));
}

int64_t
wrappedAbsolute(int64_t value)
{
    return value >= 0 ? value : signedBits(UINT64_C(0) - bits(value));
}

void
requireReference(
    const McfStableRef &reference, uint32_t kind, uint64_t limit,
    const char *label)
{
    if (reference.kind != kind || reference.objectId >= limit)
        throw Error(std::string("MCFREG2 pricing ") + label +
                    " reference differs");
}

void
sortBasket(std::vector<BasketState> &basket, int64_t minimum, int64_t maximum)
{
    int64_t left = minimum;
    int64_t right = maximum;
    const int64_t cut = basket[static_cast<size_t>((left + right) / 2)].absCost;
    do {
        while (basket[static_cast<size_t>(left)].absCost > cut)
            ++left;
        while (cut > basket[static_cast<size_t>(right)].absCost)
            --right;
        if (left < right)
            std::swap(
                basket[static_cast<size_t>(left)],
                basket[static_cast<size_t>(right)]);
        if (left <= right) {
            ++left;
            --right;
        }
    } while (left <= right);
    if (minimum < right)
        sortBasket(basket, minimum, right);
    if (left < maximum && left <= static_cast<int64_t>(BasketLimit))
        sortBasket(basket, left, maximum);
}

} // anonymous namespace

PricingDerivedOut
replayPricing(const PricingLiveIn &liveIn, KernelTraceSink &trace)
{
    if (liveIn.m == 0 || liveIn.nrGroup == 0 ||
        liveIn.groupPos >= liveIn.nrGroup ||
        liveIn.basket.size() > BasketLimit)
        throw Error("MCFREG2 pricing live-in state differs");
    if (liveIn.initialize) {
        const uint64_t expectedGroups = (liveIn.m - 1) / GroupTarget + 1;
        if (!liveIn.basket.empty() || liveIn.nrGroup != expectedGroups ||
            liveIn.groupPos != 0)
            throw Error("MCFREG2 pricing initialized state differs");
    }

    std::vector<BasketState> basket(1);
    basket.reserve(BasketLimit + 1);
    for (size_t index = 0; index < liveIn.basket.size(); ++index) {
        const auto &item = liveIn.basket[index];
        if (item.slot != index + 1)
            throw Error("MCFREG2 pricing live-in basket order differs");
        requireReference(item.arc, MCFREG2_OBJECT_ARC, liveIn.m, "basket");
        basket.push_back(item);
    }

    PricingDerivedOut output;
    output.ordinal = liveIn.ordinal;
    output.nrGroup = liveIn.nrGroup;
    uint64_t scanPosition = 0;
    uint64_t group = liveIn.groupPos;
    const uint64_t oldGroup = group;
    do {
        for (uint64_t arcIndex = group; arcIndex < liveIn.m;) {
            if (scanPosition >= liveIn.scans.size())
                throw Error("MCFREG2 pricing scan traversal is incomplete");
            const auto &scan = liveIn.scans[scanPosition];
            if (scan.scanPosition != scanPosition)
                throw Error("MCFREG2 pricing scan position differs");
            requireReference(scan.arc, MCFREG2_OBJECT_ARC, liveIn.m, "arc");
            requireReference(scan.tail, MCFREG2_OBJECT_NODE,
                             std::numeric_limits<uint64_t>::max(), "tail");
            requireReference(scan.head, MCFREG2_OBJECT_NODE,
                             std::numeric_limits<uint64_t>::max(), "head");
            if (scan.arc.objectId != arcIndex)
                throw Error("MCFREG2 pricing scan traversal differs");
            if (arcIndex >
                    (std::numeric_limits<uint64_t>::max() - ArcAddress) / 96U ||
                scan.tail.objectId >
                    (std::numeric_limits<uint64_t>::max() -
                     PotentialAddress) / 8U ||
                scan.head.objectId >
                    (std::numeric_limits<uint64_t>::max() -
                     PotentialAddress) / 8U)
                throw Error("MCFREG2 pricing trace address overflows");

            trace.emit(
                1, matched_trace::Opcode::LOAD_U64, scanPosition,
                ArcAddress + arcIndex * 96U, bits(scan.cost), 0,
                bits(scan.cost));
            trace.emit(
                1, matched_trace::Opcode::LOAD_U64, scanPosition,
                PotentialAddress + scan.tail.objectId * 8U,
                bits(scan.tailPotential), 0, bits(scan.tailPotential));
            trace.emit(
                1, matched_trace::Opcode::LOAD_U64, scanPosition,
                PotentialAddress + scan.head.objectId * 8U,
                bits(scan.headPotential), 0, bits(scan.headPotential));
            const int64_t partial = wrappedSubtract(
                scan.cost, scan.tailPotential);
            const int64_t reduced = wrappedAdd(partial, scan.headPotential);
            trace.emit(
                1, matched_trace::Opcode::I64_ADD, scanPosition, 0,
                bits(scan.cost), UINT64_C(0) - bits(scan.tailPotential),
                bits(partial));
            trace.emit(
                1, matched_trace::Opcode::I64_ADD, scanPosition, 0,
                bits(partial), bits(scan.headPotential), bits(reduced));

            const bool candidate =
                (reduced < 0 && scan.ident == 1) ||
                (reduced > 0 && scan.ident == 2);
            int64_t basketSlot = -1;
            if (candidate && basket.size() - 1 < BasketLimit) {
                basketSlot = static_cast<int64_t>(basket.size());
                basket.push_back(BasketState{
                    static_cast<uint64_t>(basketSlot), scan.arc, reduced,
                    wrappedAbsolute(reduced),
                });
            }
            output.candidates.push_back(PricingCandidate{
                scanPosition, reduced, candidate, basketSlot,
            });
            ++scanPosition;
            if (arcIndex > std::numeric_limits<uint64_t>::max() -
                               liveIn.nrGroup)
                break;
            arcIndex += liveIn.nrGroup;
        }
        group = group + 1 == liveIn.nrGroup ? 0 : group + 1;
    } while (basket.size() - 1 < BasketLimit && group != oldGroup);

    if (scanPosition != liveIn.scans.size())
        throw Error("MCFREG2 pricing scan traversal has extra records");
    output.arcsPriced = scanPosition;
    output.groupPos = group;
    output.initialize = basket.size() == 1;
    if (output.initialize) {
        output.selectedArc = McfStableRef{
            MCFREG2_OBJECT_NULL, 0, std::numeric_limits<uint64_t>::max(),
        };
        output.selectedReducedCost = 0;
        return output;
    }

    sortBasket(basket, 1, static_cast<int64_t>(basket.size() - 1));
    output.basket.assign(basket.begin() + 1, basket.end());
    for (size_t index = 0; index < output.basket.size(); ++index)
        output.basket[index].slot = index + 1;
    output.selectedArc = output.basket.front().arc;
    output.selectedReducedCost = output.basket.front().cost;
    return output;
}

} // namespace mcfreg2
