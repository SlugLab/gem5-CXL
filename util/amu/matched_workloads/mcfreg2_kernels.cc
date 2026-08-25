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

bool
sameRef(const McfStableRef &left, const McfStableRef &right)
{
    return left.kind == right.kind && left.generation == right.generation &&
           left.objectId == right.objectId;
}

McfStableRef
nullRef()
{
    return McfStableRef{
        MCFREG2_OBJECT_NULL, 0, std::numeric_limits<uint64_t>::max(),
    };
}

McfStableRef
arcRef(uint32_t generation, uint64_t index)
{
    return McfStableRef{MCFREG2_OBJECT_ARC, generation, index};
}

struct MutablePriceOut
{
    std::vector<int64_t> network;
    std::vector<ObjectState> nodes;
    std::vector<ObjectState> arcs;
    std::vector<ObjectState> dummyArcs;
    uint32_t generation = 0;
    uint64_t capacity = 0;
};

MutablePriceOut
decodePriceOut(const PriceOutLiveIn &liveIn)
{
    if (liveIn.networkWords.size() != 23U || liveIn.arenaCapacity == 0U ||
        !liveIn.heap.empty())
        throw Error("MCFREG2 price-out live-in layout differs");
    MutablePriceOut state;
    state.network = liveIn.networkWords;
    state.generation = liveIn.arenaGeneration;
    state.capacity = liveIn.arenaCapacity;
    for (const auto &object : liveIn.objects) {
        if (object.reference.kind == MCFREG2_OBJECT_NODE)
            state.nodes.push_back(object);
        else if (object.reference.kind == MCFREG2_OBJECT_ARC)
            state.arcs.push_back(object);
        else if (object.reference.kind == MCFREG2_OBJECT_DUMMY_ARC)
            state.dummyArcs.push_back(object);
        else
            throw Error("MCFREG2 price-out object kind differs");
    }
    const auto nonnegativeWord = [&](size_t index, const char *label) {
        if (state.network[index] < 0)
            throw Error(std::string("MCFREG2 price-out ") + label +
                        " is negative");
        return static_cast<uint64_t>(state.network[index]);
    };
    const uint64_t n = nonnegativeWord(0, "n");
    const uint64_t maxM = nonnegativeWord(2, "max_m");
    const uint64_t m = nonnegativeWord(3, "m");
    const uint64_t stop = nonnegativeWord(22, "stop_arcs");
    if (n == std::numeric_limits<uint64_t>::max())
        throw Error("MCFREG2 price-out node count overflows");
    if (m > maxM || maxM != state.capacity || stop != m ||
        state.nodes.size() != n + 1 || state.arcs.size() != m ||
        state.dummyArcs.size() != n)
        throw Error("MCFREG2 price-out object counts differ");
    auto sortObjects = [](auto &objects) {
        std::sort(objects.begin(), objects.end(), [](const auto &left,
                                                     const auto &right) {
            return left.reference.objectId < right.reference.objectId;
        });
    };
    sortObjects(state.nodes);
    sortObjects(state.arcs);
    sortObjects(state.dummyArcs);
    for (size_t index = 0; index < state.nodes.size(); ++index) {
        const auto &object = state.nodes[index];
        if (object.reference.generation != 0U ||
            object.reference.objectId != index || object.words.size() != 6U ||
            object.links.size() != 8U)
            throw Error("MCFREG2 price-out node state differs");
    }
    for (size_t index = 0; index < state.arcs.size(); ++index) {
        const auto &object = state.arcs[index];
        if (object.reference.generation != state.generation ||
            object.reference.objectId != index || object.words.size() != 4U ||
            object.links.size() != 4U)
            throw Error("MCFREG2 price-out active arc state differs");
    }
    for (size_t index = 0; index < state.dummyArcs.size(); ++index) {
        const auto &object = state.dummyArcs[index];
        if (object.reference.generation != 0U ||
            object.reference.objectId != index || object.words.size() != 4U ||
            object.links.size() != 4U)
            throw Error("MCFREG2 price-out dummy arc state differs");
    }
    return state;
}

uint64_t
nodeIndex(const McfStableRef &reference, uint64_t nodes, const char *label)
{
    if (reference.kind != MCFREG2_OBJECT_NODE ||
        reference.generation != 0U || reference.objectId >= nodes)
        throw Error(std::string("MCFREG2 price-out ") + label +
                    " node reference differs");
    return reference.objectId;
}

void
refreshAdjacency(MutablePriceOut &state)
{
    for (auto &node : state.nodes) {
        node.links[5] = nullRef();
        node.links[6] = nullRef();
    }
    for (size_t index = 0; index < state.arcs.size(); ++index) {
        auto &arc = state.arcs[index];
        const uint64_t tail = nodeIndex(
            arc.links[0], state.nodes.size(), "tail");
        const uint64_t head = nodeIndex(
            arc.links[1], state.nodes.size(), "head");
        arc.links[2] = state.nodes[tail].links[5];
        state.nodes[tail].links[5] = arcRef(state.generation, index);
        arc.links[3] = state.nodes[head].links[6];
        state.nodes[head].links[6] = arcRef(state.generation, index);
    }
}

void
resizePriceOut(MutablePriceOut &state, KernelTraceSink &trace)
{
    const uint32_t oldGeneration = state.generation;
    if (state.network[6] < 0 || state.network[7] < 0)
        throw Error("MCFREG2 price-out resize state is negative");
    const uint64_t maxNew = static_cast<uint64_t>(state.network[7]);
    if (state.capacity > std::numeric_limits<uint64_t>::max() - maxNew ||
        static_cast<uint64_t>(state.network[6]) >
            std::numeric_limits<uint64_t>::max() - maxNew ||
        state.generation == std::numeric_limits<uint32_t>::max() ||
        state.capacity + maxNew >
            static_cast<uint64_t>(std::numeric_limits<int64_t>::max()) ||
        static_cast<uint64_t>(state.network[6]) + maxNew >
            static_cast<uint64_t>(std::numeric_limits<int64_t>::max()))
        throw Error("MCFREG2 price-out resize overflows");
    state.capacity += maxNew;
    state.network[2] = static_cast<int64_t>(state.capacity);
    state.network[6] = static_cast<int64_t>(
        static_cast<uint64_t>(state.network[6]) + maxNew);
    ++state.generation;
    for (auto &arc : state.arcs)
        arc.reference.generation = state.generation;
    const McfStableRef root{MCFREG2_OBJECT_NODE, 0, 0};
    for (size_t index = 1; index < state.nodes.size(); ++index) {
        auto &node = state.nodes[index];
        if (!sameRef(node.links[1], root) &&
            node.links[4].kind == MCFREG2_OBJECT_ARC &&
            node.links[4].generation == oldGeneration)
            node.links[4].generation = state.generation;
    }
    refreshAdjacency(state);
    trace.emit(
        2, matched_trace::Opcode::BARRIER, 0, 0, oldGeneration,
        state.generation, state.capacity);
}

PriceOutDerivedOut
encodePriceOut(const PriceOutLiveIn &liveIn, MutablePriceOut state)
{
    PriceOutDerivedOut output;
    output.ordinal = liveIn.ordinal;
    output.networkWords = std::move(state.network);
    output.objects.reserve(
        state.nodes.size() + state.arcs.size() + state.dummyArcs.size());
    output.objects.insert(
        output.objects.end(), state.nodes.begin(), state.nodes.end());
    output.objects.insert(
        output.objects.end(), state.arcs.begin(), state.arcs.end());
    output.objects.insert(
        output.objects.end(), state.dummyArcs.begin(), state.dummyArcs.end());
    output.arenaGeneration = state.generation;
    output.arenaCapacity = state.capacity;
    return output;
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

PriceOutDerivedOut
replayPriceOut(const PriceOutLiveIn &liveIn, KernelTraceSink &trace)
{
    MutablePriceOut state = decodePriceOut(liveIn);
    const auto word = [&](size_t index, const char *label) {
        if (state.network[index] < 0)
            throw Error(std::string("MCFREG2 price-out ") + label +
                        " is negative");
        return static_cast<uint64_t>(state.network[index]);
    };
    const uint64_t trips = word(1, "n_trips");
    const uint64_t maxM = word(2, "max_m");
    const uint64_t m = word(3, "m");
    const uint64_t maxNew = word(7, "max_new_m");
    if (trips > std::numeric_limits<uint64_t>::max() / 3U ||
        trips * 3U > m)
        throw Error("MCFREG2 price-out trip arc layout differs");
    bool resize = false;
    if (trips <= 15000U) {
        if (m > std::numeric_limits<uint64_t>::max() - maxNew ||
            (trips != 0U &&
             trips > std::numeric_limits<uint64_t>::max() / trips))
            throw Error("MCFREG2 price-out resize predicate overflows");
        const uint64_t square = trips * trips / 2U;
        if (square > std::numeric_limits<uint64_t>::max() - m)
            throw Error("MCFREG2 price-out resize predicate overflows");
        resize = m + maxNew > maxM && square + m > maxM;
    }
    if (resize)
        resizePriceOut(state, trace);

    for (uint64_t trip = 0; trip < trips; ++trip) {
        const uint64_t secondArc = trip * 3U + 1U;
        if (state.arcs[secondArc].words[1] == -1)
            throw Error(
                "MCFREG2 price-out sparse prefix reaches undefined native "
                "first_of_sparse_list");
    }
    return encodePriceOut(liveIn, std::move(state));
}

} // namespace mcfreg2
