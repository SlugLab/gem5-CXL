/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mcfreg2_state.hh"

#include "mcfreg2.hh"

#include <algorithm>
#include <stdexcept>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <utility>

namespace mcfreg2
{
namespace
{

class Encoder
{
  public:
    Encoder() : bytes({'M', 'C', 'F', 'C', 'S', '3', 0, 0}) {}

    void u8(uint8_t value) { bytes.push_back(value); }

    void u32(uint32_t value)
    {
        for (unsigned shift = 0; shift < 32; shift += 8)
            bytes.push_back(static_cast<uint8_t>(value >> shift));
    }

    void u64(uint64_t value)
    {
        for (unsigned shift = 0; shift < 64; shift += 8)
            bytes.push_back(static_cast<uint8_t>(value >> shift));
    }

    void i64(int64_t value) { u64(static_cast<uint64_t>(value)); }
    void boolean(bool value) { u8(value ? 1 : 0); }

    void reference(const McfStableRef &value)
    {
        u32(value.kind);
        u32(value.generation);
        u64(value.objectId);
    }

    template<class T, class Function>
    void vector(const std::vector<T> &values, Function function)
    {
        u64(values.size());
        for (const auto &value : values)
            function(value);
    }

    std::vector<uint8_t> take() { return std::move(bytes); }

  private:
    std::vector<uint8_t> bytes;
};

auto
referenceKey(const McfStableRef &reference)
{
    return std::tie(reference.kind, reference.generation, reference.objectId);
}

void
encodeBasket(Encoder &encoder, const std::vector<BasketState> &values)
{
    encoder.vector(values, [&](const BasketState &value) {
        encoder.u64(value.slot);
        encoder.reference(value.arc);
        encoder.i64(value.cost);
        encoder.i64(value.absCost);
    });
}

void
encodeReferences(Encoder &encoder, const std::vector<McfStableRef> &values)
{
    encoder.vector(values, [&](const McfStableRef &value) {
        encoder.reference(value);
    });
}

void
encodeObjects(Encoder &encoder, const std::vector<ObjectState> &values)
{
    std::vector<const ObjectState *> ordered;
    ordered.reserve(values.size());
    for (const auto &value : values)
        ordered.push_back(&value);
    std::sort(ordered.begin(), ordered.end(), [](const auto *left,
                                                  const auto *right) {
        return referenceKey(left->reference) < referenceKey(right->reference);
    });
    for (size_t index = 1; index < ordered.size(); ++index) {
        if (referenceKey(ordered[index - 1]->reference) ==
            referenceKey(ordered[index]->reference))
            throw Error("MCFREG2 canonical object reference is duplicated");
    }
    encoder.u64(ordered.size());
    for (const auto *value : ordered) {
        encoder.reference(value->reference);
        encoder.vector(value->words, [&](int64_t word) { encoder.i64(word); });
        encodeReferences(encoder, value->links);
    }
}

template<class T>
void
encodePriceOutCommon(Encoder &encoder, const T &state)
{
    encoder.u64(state.ordinal);
    encoder.vector(
        state.networkWords, [&](int64_t word) { encoder.i64(word); });
    encodeObjects(encoder, state.objects);
    encoder.u32(state.arenaGeneration);
    encoder.u64(state.arenaCapacity);
    encodeReferences(encoder, state.heap);
}

} // anonymous namespace

std::vector<uint8_t>
encodeCallState(const CanonicalCallState &state)
{
    Encoder encoder;
    std::visit([&](const auto &value) {
        using T = std::decay_t<decltype(value)>;
        if constexpr (std::is_same_v<T, PricingLiveIn>) {
            encoder.u8(1);
            encoder.u64(value.ordinal);
            encoder.u64(value.m);
            encoder.u64(value.nrGroup);
            encoder.u64(value.groupPos);
            encoder.boolean(value.initialize);
            encodeBasket(encoder, value.basket);
            encoder.vector(value.scans, [&](const PricingScanLiveIn &scan) {
                encoder.u64(scan.scanPosition);
                encoder.reference(scan.arc);
                encoder.reference(scan.tail);
                encoder.reference(scan.head);
                encoder.i64(scan.cost);
                encoder.i64(scan.ident);
                encoder.i64(scan.tailPotential);
                encoder.i64(scan.headPotential);
            });
        } else if constexpr (std::is_same_v<T, PricingDerivedOut>) {
            encoder.u8(2);
            encoder.u64(value.ordinal);
            encoder.vector(value.candidates, [&](const PricingCandidate &item) {
                encoder.u64(item.scanPosition);
                encoder.i64(item.reducedCost);
                encoder.boolean(item.candidate);
                encoder.i64(item.basketSlot);
            });
            encodeBasket(encoder, value.basket);
            encoder.reference(value.selectedArc);
            encoder.i64(value.selectedReducedCost);
            encoder.u64(value.arcsPriced);
            encoder.u64(value.nrGroup);
            encoder.u64(value.groupPos);
            encoder.boolean(value.initialize);
        } else if constexpr (std::is_same_v<T, PriceOutLiveIn>) {
            encoder.u8(3);
            encodePriceOutCommon(encoder, value);
        } else {
            encoder.u8(4);
            encodePriceOutCommon(encoder, value);
            encoder.vector(value.candidates, [&](const PriceOutCandidate &item) {
                encoder.u64(item.candidate);
                encoder.reference(item.tail);
                encoder.reference(item.head);
                encoder.i64(item.cost);
                encoder.i64(item.reducedCost);
            });
            encoder.vector(value.decisions, [&](const PriceOutDecision &item) {
                encoder.u64(item.candidate);
                encoder.u8(static_cast<uint8_t>(item.decision));
                encoder.reference(item.reference);
            });
        }
    }, state);
    return encoder.take();
}

std::string
digestCallState(const CanonicalCallState &state)
{
    const auto bytes = encodeCallState(state);
    return sha256Hex(std::string_view(
        reinterpret_cast<const char *>(bytes.data()), bytes.size()));
}

void
CallFrameReader::begin(uint64_t call, uint64_t order, const std::string &phase)
{
    if (isActive)
        throw Error("MCFREG2 call entry is duplicated");
    if (phase != "pricing" && phase != "price_out")
        throw Error("MCFREG2 call phase is invalid");
    isActive = true;
    activeCall = call;
    activeOrder = order;
    activePhase = phase;
    liveIns.clear();
    observed.clear();
}

void
CallFrameReader::accept(RecordKind kind, RecordRole role)
{
    if (!isActive)
        throw Error("MCFREG2 record has no call entry");
    auto &target = role == RecordRole::LiveIn ? liveIns : observed;
    target.insert(kind);
}

void
CallFrameReader::end(uint64_t call, uint64_t order, const std::string &phase)
{
    if (!isActive)
        throw Error("MCFREG2 call exit has no entry");
    if (call != activeCall || order != activeOrder || phase != activePhase)
        throw Error("MCFREG2 call exit differs");
    isActive = false;
}

} // namespace mcfreg2
