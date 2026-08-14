// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include <immintrin.h>

#include <gem5/m5ops.h>

#include "amu.h"

namespace {

constexpr size_t kSpmBytes = 64 * 1024;
constexpr size_t kWindowSlots = 256;
constexpr size_t kCompletionBatch = 4;
constexpr unsigned kCompletionCountBits = 3;
constexpr unsigned kCompletionTokenBits = 15;
constexpr uint64_t kCompletionTokenMask =
    (UINT64_C(1) << kCompletionTokenBits) - 1;
constexpr size_t kOwnerEntries = 1 << kCompletionTokenBits;
constexpr size_t kOwnerNotFound = kOwnerEntries;
constexpr unsigned kOwnerSlotShift = kCompletionTokenBits;
constexpr uint32_t kOwnerSlotMask = 0xff;
constexpr unsigned kOwnerPhaseShift = kOwnerSlotShift + 8;
constexpr uint32_t kOwnerPhaseMask = 0x3;
constexpr uint32_t kOwnerLive = UINT32_C(1) << (kOwnerPhaseShift + 2);
constexpr size_t kCacheLineBytes = 64;
constexpr size_t kGupsEntries = 1 << 16;
constexpr size_t kHashBuckets = 16000;
constexpr size_t kHashDepth = 4;
constexpr size_t kStreamGranularity = 512;
constexpr size_t kStreamBlocks = 256;
constexpr unsigned kChecksumMagic = 0x414d5531;

struct HashNode {
    uint64_t key;
    uint64_t value;
    int32_t next;
    uint32_t padding;
    uint8_t reserved[24];
};
static_assert(sizeof(HashNode) == 48, "paper hash node must be 48 bytes");

struct StreamBlock {
    uint64_t words[kStreamGranularity / sizeof(uint64_t)];
};
static_assert(sizeof(StreamBlock) == kStreamGranularity,
              "STREAM AMU granularity must be 512 bytes");

struct Options {
    std::string workload;
    std::string rawOutput;
    size_t iterations = 1;
    bool amu = false;
};

struct BenchmarkState {
    std::vector<uint64_t> gupsTable;
    std::vector<int32_t> hashBuckets;
    std::vector<HashNode> hashNodes;
    std::vector<int32_t> hashCurrent;
    std::vector<uint64_t> hashKeys;
    std::vector<uint64_t> hashResults;
    std::vector<StreamBlock> streamA;
    std::vector<StreamBlock> streamB;
    std::vector<StreamBlock> streamC;
};

enum class SlotPhase {
    Free,
    LoadPending,
    ReadyToStore,
    StorePending,
};

struct Slot {
    size_t op = 0;
    void *destination = nullptr;
    uint64_t id = 0;
    SlotPhase phase = SlotPhase::Free;
    uint8_t stage = 0;
};

static_assert(sizeof(Slot) * kWindowSlots <= 8 * 1024,
              "AMU scheduler control must fit in 8 KiB");
static_assert(sizeof(uint32_t) * kOwnerEntries <= 128 * 1024,
              "packed AMU token owner map must fit in 128 KiB");

alignas(64) std::array<uint8_t, kSpmBytes> spmArena{};
size_t queueSafeSlots = 0;

Options
parseOptions(int argc, char **argv)
{
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        if (argument == "--workload" && i + 1 < argc) {
            options.workload = argv[++i];
        } else if (argument == "--iterations" && i + 1 < argc) {
            options.iterations = std::stoull(argv[++i]);
        } else if (argument == "--raw-output" && i + 1 < argc) {
            options.rawOutput = argv[++i];
        } else if (argument == "--amu") {
            options.amu = true;
        } else {
            throw std::runtime_error("invalid AMU paper-profile argument");
        }
    }
    if (options.workload != "gups" && options.workload != "hj" &&
        options.workload != "stream") {
        throw std::runtime_error("--workload must be gups, hj, or stream");
    }
    if (options.iterations == 0 || options.rawOutput.empty())
        throw std::runtime_error("iterations and raw output are required");
    return options;
}

size_t
slotStride(size_t granularity)
{
    if (granularity == 0 || granularity > kSpmBytes)
        throw std::runtime_error("invalid SPM slot granularity");
    return ((granularity + kCacheLineBytes - 1) / kCacheLineBytes) *
        kCacheLineBytes;
}

size_t
activeSlotCount(size_t granularity)
{
    return std::min(kWindowSlots, kSpmBytes / slotStride(granularity));
}

size_t
queueSafeSlotCount(size_t granularity)
{
    const size_t cache_line_bytes =
        amu_cfgrd(AMU_CFG_CACHE_LINE_BYTES);
    const size_t far_queue_packets =
        amu_cfgrd(AMU_CFG_FAR_SEND_QUEUE_PACKETS);
    const size_t spm_queue_packets =
        amu_cfgrd(AMU_CFG_SPM_SEND_QUEUE_PACKETS);
    if (cache_line_bytes == 0 || far_queue_packets == 0 ||
        spm_queue_packets == 0) {
        throw std::runtime_error("AMU queue geometry is unavailable");
    }
    if (cache_line_bytes != kCacheLineBytes) {
        throw std::runtime_error(
            "AMU paper proxy requires 64-byte cache lines");
    }

    // Far vectors are not guaranteed to be cache-line aligned. A range can
    // therefore touch one more line than its aligned SPM slot. An aload owns
    // reservations for both the coherent acquire and line-write SPM phases.
    const size_t max_far_packets =
        (granularity + 2 * cache_line_bytes - 2) / cache_line_bytes;
    const size_t spm_packets =
        (granularity + cache_line_bytes - 1) / cache_line_bytes;
    const size_t reserved_spm_packets = 2 * spm_packets;
    return std::min(far_queue_packets / max_far_packets,
                    spm_queue_packets / reserved_spm_packets);
}

void
flushRange(void *address, size_t size)
{
    auto *bytes = static_cast<uint8_t *>(address);
    for (size_t offset = 0; offset < size; offset += kCacheLineBytes)
        _mm_clflush(bytes + offset);
    _mm_mfence();
}

void
configure(size_t granularity)
{
    if (amu_cfgwr(AMU_CFG_MAX_OUTSTANDING, kWindowSlots) == 0 ||
        amu_cfgwr(AMU_CFG_GRANULARITY, granularity) == 0) {
        throw std::runtime_error("AMU configuration failed");
    }
}

__attribute__((always_inline)) inline uint64_t
profileAload(void *spm_address, const void *memory_address)
{
#if defined(__x86_64__)
    uint64_t result;
    asm volatile(".byte 0x0f, 0x04\n\t.word %c[function]"
                 : "=a"(result)
                 : "D"(spm_address), "S"(memory_address),
                   [function] "i"(M5OP_AMU_ALOAD)
                 : "memory");
    return result;
#else
    return amu_aload(spm_address, memory_address);
#endif
}

__attribute__((always_inline)) inline uint64_t
profileAstore(const void *spm_address, void *memory_address)
{
#if defined(__x86_64__)
    uint64_t result;
    asm volatile(".byte 0x0f, 0x04\n\t.word %c[function]"
                 : "=a"(result)
                 : "D"(spm_address), "S"(memory_address),
                   [function] "i"(M5OP_AMU_ASTORE)
                 : "memory");
    return result;
#else
    return amu_astore(spm_address, memory_address);
#endif
}

__attribute__((always_inline)) inline uint64_t
profileGetfinBatch()
{
#if defined(__x86_64__)
    uint64_t result;
    asm volatile(".byte 0x0f, 0x04\n\t.word %c[function]"
                 : "=a"(result)
                 : [function] "i"(M5OP_AMU_GETFIN_BATCH)
                 : "memory");
    return result;
#else
    return m5_amu_getfin_batch();
#endif
}

__attribute__((always_inline)) inline void
profileWaitfin()
{
#if defined(__x86_64__)
    asm volatile(".byte 0x0f, 0x04\n\t.word %c[function]"
                 :
                 : [function] "i"(M5OP_AMU_WAITFIN)
                 : "rax", "memory");
#else
    m5_amu_waitfin();
#endif
}

class PersistentScheduler
{
  public:
    explicit PersistentScheduler(size_t granularity)
        : granularity(granularity), stride(slotStride(granularity)),
          activeSlots(std::min(activeSlotCount(granularity),
                               queueSafeSlots))
    {
        if (activeSlots == 0)
            throw std::runtime_error("SPM arena has no usable slots");
        configure(granularity);
    }

    size_t capacity() const { return activeSlots; }
    size_t live() const { return liveRequests; }

    Slot &slot(size_t index)
    {
        if (index >= activeSlots)
            throw std::runtime_error("scheduler slot index out of range");
        return slots[index];
    }

    template <typename T>
    T *payload(size_t index)
    {
        if (sizeof(T) > granularity)
            throw std::runtime_error("payload exceeds configured granularity");
        (void)slot(index);
        return reinterpret_cast<T *>(spmArena.data() + index * stride);
    }

    void issueLoad(size_t index, size_t op, const void *source, uint8_t stage)
    {
        Slot &entry = slot(index);
        if (entry.phase != SlotPhase::Free)
            throw std::runtime_error("load issued into a non-free slot");
        entry.op = op;
        entry.destination = nullptr;
        entry.stage = stage;
        entry.phase = SlotPhase::LoadPending;
        const uint64_t id = profileAload(
            spmArena.data() + index * stride, source);
        registerId(index, id, SlotPhase::LoadPending);
        entry.id = id;
    }

    void readyToStore(size_t index, void *destination)
    {
        Slot &entry = slot(index);
        if (entry.phase != SlotPhase::LoadPending || entry.id != 0)
            throw std::runtime_error("slot is not ready for a store transition");
        entry.destination = destination;
        entry.phase = SlotPhase::ReadyToStore;
    }

    void issueStore(size_t index)
    {
        Slot &entry = slot(index);
        if (entry.phase != SlotPhase::ReadyToStore || !entry.destination)
            throw std::runtime_error("store issued from an unready slot");
        entry.phase = SlotPhase::StorePending;
        const uint64_t id = profileAstore(
            spmArena.data() + index * stride, entry.destination);
        registerId(index, id, SlotPhase::StorePending);
        entry.id = id;
    }

    void issueGupsLoad(size_t index, size_t op, const uint64_t *source)
    {
        if (index >= activeSlots)
            throw std::runtime_error("GUPS slot index is out of range");
        gupsOps[index] = op;
        const uint64_t id = profileAload(
            spmArena.data() + index * stride, source);
        registerId(index, id, SlotPhase::LoadPending);
    }

    void issueGupsStore(size_t index, uint64_t *destination)
    {
        const uint64_t id = profileAstore(
            spmArena.data() + index * stride, destination);
        registerId(index, id, SlotPhase::StorePending);
    }

    uint64_t &gupsPayload(size_t index)
    {
        return *reinterpret_cast<uint64_t *>(
            spmArena.data() + index * stride);
    }

    size_t gupsOp(size_t index) const { return gupsOps[index]; }

    size_t waitCompletionOwners(
        std::array<uint32_t, kCompletionBatch> &completedOwners)
    {
        uint64_t packed = 0;
        size_t count = 0;
        while (count == 0) {
            count = (packed = profileGetfinBatch()) & 0x7;
            if (count == 0)
                profileWaitfin();
        }
        if (count > kCompletionBatch)
            throw std::runtime_error("AMU returned an oversized batch");

        for (size_t index = 0; index < count; ++index) {
            const uint64_t token =
                (packed >> (kCompletionCountBits +
                            index * kCompletionTokenBits)) &
                kCompletionTokenMask;
            const size_t owner_index = findOwnerToken(token);
            if (owner_index == kOwnerNotFound)
                throw std::runtime_error("AMU returned an unknown or stale ID");
            const uint32_t owner = ownerWords[owner_index];
            const size_t owner_slot =
                (owner >> kOwnerSlotShift) & kOwnerSlotMask;
            const SlotPhase expected = static_cast<SlotPhase>(
                (owner >> kOwnerPhaseShift) & kOwnerPhaseMask);
            if (owner_slot >= activeSlots ||
                (expected != SlotPhase::LoadPending &&
                 expected != SlotPhase::StorePending)) {
                throw std::runtime_error("AMU completion has an invalid owner");
            }
            if (liveRequests == 0)
                throw std::runtime_error(
                    "AMU live-request accounting underflow");
            ownerWords[owner_index] = 0;
            --liveRequests;
            completedOwners[index] = owner;
        }
        return count;
    }

    size_t waitCompletionBatch(
        std::array<size_t, kCompletionBatch> &completedSlots)
    {
        // Do not value-initialize this buffer immediately before the m5op.
        // O3 may retain those zeroing stores in its store queue while the
        // functional pseudo instruction writes the completed IDs, allowing
        // the older stores to overwrite or forward zeros after the m5op.
        std::array<uint32_t, kCompletionBatch> completedOwners;
        const size_t count = waitCompletionOwners(completedOwners);

        for (size_t index = 0; index < count; ++index) {
            const uint32_t owner = completedOwners[index];
            const uint64_t token = owner & kCompletionTokenMask;
            const size_t owner_slot =
                (owner >> kOwnerSlotShift) & kOwnerSlotMask;
            const SlotPhase expected = static_cast<SlotPhase>(
                (owner >> kOwnerPhaseShift) & kOwnerPhaseMask);
            Slot &entry = slots[owner_slot];
            if ((entry.id & kCompletionTokenMask) != token ||
                entry.phase != expected) {
                throw std::runtime_error(
                    "AMU completion owner/phase mismatch");
            }

            entry.id = 0;
            completedSlots[index] = owner_slot;
        }
        return count;
    }

    void release(size_t index)
    {
        Slot &entry = slot(index);
        if (entry.id != 0 ||
            (entry.phase != SlotPhase::LoadPending &&
             entry.phase != SlotPhase::StorePending)) {
            throw std::runtime_error("invalid scheduler slot release");
        }
        entry = {};
    }

    void requireDrained() const
    {
        if (liveRequests != 0 ||
            amu_cfgrd(AMU_CFG_OUTSTANDING) != 0 ||
            amu_cfgrd(AMU_CFG_FINISHED) != 0) {
            throw std::runtime_error("AMU scheduler did not drain completely");
        }
    }

  private:
    size_t findOwnerToken(uint64_t token) const
    {
        return ownerWords[token] & kOwnerLive ? token : kOwnerNotFound;
    }

    void registerId(size_t index, uint64_t id, SlotPhase expected)
    {
        if (id == 0)
            throw std::runtime_error("AMU request admission failed");
        if (liveRequests >= kWindowSlots)
            throw std::runtime_error("AMU request window exceeded 256 IDs");
        const uint64_t token = id & kCompletionTokenMask;
        if (ownerWords[token] != 0)
            throw std::runtime_error("AMU returned a duplicate live token");
        ownerWords[token] =
            static_cast<uint32_t>(token) |
            (static_cast<uint32_t>(index) << kOwnerSlotShift) |
            (static_cast<uint32_t>(expected) << kOwnerPhaseShift) |
            kOwnerLive;
        ++liveRequests;
    }

    const size_t granularity;
    const size_t stride;
    const size_t activeSlots;
    size_t liveRequests = 0;
    std::array<Slot, kWindowSlots> slots{};
    std::array<size_t, kWindowSlots> gupsOps{};
    std::array<uint32_t, kOwnerEntries> ownerWords{};
};

void
primeSpm(size_t granularity)
{
    const size_t bytes = activeSlotCount(granularity) * slotStride(granularity);
    std::memset(spmArena.data(), 0, bytes);
    flushRange(spmArena.data(), bytes);
    alignas(64) std::array<uint8_t, kCacheLineBytes> source{};
    flushRange(source.data(), source.size());

    configure(kCacheLineBytes);
    std::unordered_map<uint64_t, size_t> live;
    live.reserve(kWindowSlots);
    size_t nextLine = 0;
    size_t completed = 0;
    const size_t lines = bytes / kCacheLineBytes;
    while (completed != lines) {
        while (nextLine != lines && live.size() < kWindowSlots) {
            const uint64_t id = amu_aload(
                spmArena.data() + nextLine * kCacheLineBytes, source.data());
            if (id == 0 || !live.emplace(id, nextLine).second)
                throw std::runtime_error("SPM priming request admission failed");
            ++nextLine;
        }
        uint64_t id = 0;
        while (id == 0)
            id = amu_getfin();
        const auto owner = live.find(id);
        if (owner == live.end())
            throw std::runtime_error("SPM priming returned an unknown ID");
        live.erase(owner);
        ++completed;
    }
    if (!live.empty() || amu_cfgrd(AMU_CFG_OUTSTANDING) != 0 ||
        amu_cfgrd(AMU_CFG_FINISHED) != 0) {
        throw std::runtime_error("SPM priming did not drain all requests");
    }
    if (amu_cfgwr(AMU_CFG_RESET, 1) == 0 ||
        amu_cfgrd(AMU_CFG_OUTSTANDING) != 0) {
        throw std::runtime_error("AMU reset after SPM priming failed");
    }
}

size_t
workloadGranularity(const Options &options)
{
    if (options.workload == "gups")
        return sizeof(uint64_t);
    if (options.workload == "hj")
        return sizeof(HashNode);
    return sizeof(StreamBlock);
}

template <typename T>
void
flushVector(std::vector<T> &values)
{
    if (!values.empty())
        flushRange(values.data(), values.size() * sizeof(T));
}

void
flushFarWorkingSet(const Options &options, BenchmarkState &state)
{
    if (options.workload == "gups") {
        flushVector(state.gupsTable);
    } else if (options.workload == "hj") {
        flushVector(state.hashNodes);
    } else {
        flushVector(state.streamA);
        flushVector(state.streamB);
        flushVector(state.streamC);
    }
}

unsigned
workloadTag(const Options &options)
{
    if (options.workload == "gups")
        return 1;
    if (options.workload == "hj")
        return 2;
    return 3;
}

void
prepareAndPrime(const Options &options, BenchmarkState &state)
{
    if (options.workload == "gups") {
        state.gupsTable.resize(kGupsEntries);
        for (size_t index = 0; index < state.gupsTable.size(); ++index)
            state.gupsTable[index] = UINT64_C(0x9e3779b97f4a7c15) ^ index;
    } else if (options.workload == "hj") {
        state.hashBuckets.resize(kHashBuckets);
        state.hashNodes.resize(kHashBuckets * kHashDepth);
        state.hashCurrent.resize(kWindowSlots);
        state.hashKeys.resize(kWindowSlots);
        state.hashResults.resize(kWindowSlots);
        for (size_t bucket = 0; bucket < kHashBuckets; ++bucket) {
            state.hashBuckets[bucket] =
                static_cast<int32_t>(bucket * kHashDepth);
            for (size_t depth = 0; depth < kHashDepth; ++depth) {
                const size_t index = bucket * kHashDepth + depth;
                state.hashNodes[index] = {};
                state.hashNodes[index].key = index;
                state.hashNodes[index].value =
                    index ^ UINT64_C(0xa5a5a5a5);
                state.hashNodes[index].next = depth + 1 == kHashDepth ?
                    -1 : static_cast<int32_t>(index + 1);
            }
        }
    } else {
        state.streamA.resize(kStreamBlocks);
        state.streamB.resize(kStreamBlocks);
        state.streamC.resize(kStreamBlocks);
        for (size_t block = 0; block < kStreamBlocks; ++block) {
            for (size_t word = 0; word < 64; ++word) {
                state.streamB[block].words[word] = block * 64 + word;
                state.streamC[block].words[word] =
                    UINT64_C(0x100000001) + block + word;
            }
        }
    }

    if (options.amu) {
        flushFarWorkingSet(options, state);
        queueSafeSlots = queueSafeSlotCount(workloadGranularity(options));
        primeSpm(workloadGranularity(options));
    }
}

void
runGupsBaseline(BenchmarkState &state)
{
    for (size_t op = 0; op < state.gupsTable.size(); ++op) {
        const size_t index = (op * 40503) & (state.gupsTable.size() - 1);
        state.gupsTable[index] ^=
            UINT64_C(0xd1b54a32d192ed03) ^ index;
    }
}

bool
refillGupsSlot(PersistentScheduler &scheduler, BenchmarkState &state,
               size_t &next_op, size_t slot_index)
{
    if (next_op == state.gupsTable.size())
        return false;
    const size_t op = next_op++;
    const size_t index = (op * 40503) & (state.gupsTable.size() - 1);
    scheduler.issueGupsLoad(slot_index, op, &state.gupsTable[index]);
    return true;
}

void
runGupsAmu(BenchmarkState &state, PersistentScheduler &scheduler)
{
    size_t nextOp = 0;
    size_t completed = 0;
    for (size_t slot = 0; slot < scheduler.capacity(); ++slot)
        refillGupsSlot(scheduler, state, nextOp, slot);

    while (completed != state.gupsTable.size()) {
        std::array<uint32_t, kCompletionBatch> completedOwners;
        const size_t completionCount =
            scheduler.waitCompletionOwners(completedOwners);
        for (size_t completionIndex = 0;
             completionIndex < completionCount; ++completionIndex) {
            const uint32_t owner = completedOwners[completionIndex];
            const size_t slot_index =
                (owner >> kOwnerSlotShift) & kOwnerSlotMask;
            const SlotPhase phase = static_cast<SlotPhase>(
                (owner >> kOwnerPhaseShift) & kOwnerPhaseMask);
            const size_t op = scheduler.gupsOp(slot_index);
            const size_t index =
                (op * 40503) & (state.gupsTable.size() - 1);
            if (phase == SlotPhase::LoadPending) {
                scheduler.gupsPayload(slot_index) ^=
                    UINT64_C(0xd1b54a32d192ed03) ^ index;
                scheduler.issueGupsStore(
                    slot_index, &state.gupsTable[index]);
            } else if (phase == SlotPhase::StorePending) {
                ++completed;
                refillGupsSlot(scheduler, state, nextOp, slot_index);
            } else {
                throw std::runtime_error(
                    "GUPS slot completed in wrong phase");
            }
        }
    }
    scheduler.requireDrained();
}

void
initializeHashQueries(size_t iteration, BenchmarkState &state)
{
    for (size_t query = 0; query < kWindowSlots; ++query) {
        const size_t bucket = (query * 97 + iteration) % kHashBuckets;
        const size_t depth = (query + iteration) % kHashDepth;
        state.hashCurrent[query] = state.hashBuckets[bucket];
        state.hashKeys[query] = bucket * kHashDepth + depth;
        state.hashResults[query] = 0;
    }
}

void
runHashJoinBaseline(size_t iteration, BenchmarkState &state)
{
    initializeHashQueries(iteration, state);
    for (size_t depth = 0; depth < kHashDepth; ++depth) {
        for (size_t query = 0; query < kWindowSlots; ++query) {
            if (state.hashCurrent[query] < 0 || state.hashResults[query] != 0)
                continue;
            const HashNode node = state.hashNodes[state.hashCurrent[query]];
            if (node.key == state.hashKeys[query])
                state.hashResults[query] = node.value;
            state.hashCurrent[query] = node.next;
        }
    }
}

void
runHashJoinAmu(size_t iteration, BenchmarkState &state,
               PersistentScheduler &scheduler)
{
    initializeHashQueries(iteration, state);
    for (size_t query = 0; query < kWindowSlots; ++query) {
        scheduler.issueLoad(
            query, query, &state.hashNodes[state.hashCurrent[query]], 0);
    }

    size_t completed = 0;
    while (completed != kWindowSlots) {
        std::array<size_t, kCompletionBatch> completedSlots;
        const size_t completionCount =
            scheduler.waitCompletionBatch(completedSlots);
        for (size_t completionIndex = 0;
             completionIndex < completionCount; ++completionIndex) {
            const size_t slot_index = completedSlots[completionIndex];
            Slot &slot = scheduler.slot(slot_index);
            if (slot.phase != SlotPhase::LoadPending) {
                throw std::runtime_error(
                    "hash-join slot completed in wrong phase");
            }
            const size_t query = slot.op;
            const uint8_t next_stage = slot.stage + 1;
            HashNode node{};
            std::memcpy(&node, scheduler.payload<HashNode>(slot_index),
                        sizeof(node));
            if (node.key == state.hashKeys[query])
                state.hashResults[query] = node.value;
            state.hashCurrent[query] = node.next;
            scheduler.release(slot_index);
            if (state.hashResults[query] != 0 || node.next < 0) {
                ++completed;
            } else {
                scheduler.issueLoad(
                    slot_index, query,
                    &state.hashNodes[state.hashCurrent[query]], next_stage);
            }
        }
    }
    scheduler.requireDrained();
}

void
runStreamBaseline(BenchmarkState &state)
{
    for (size_t block = 0; block < kStreamBlocks; ++block) {
        for (size_t word = 0; word < 64; ++word) {
            state.streamA[block].words[word] =
                state.streamB[block].words[word] +
                3 * state.streamC[block].words[word];
        }
    }
}

bool
refillStreamPair(PersistentScheduler &scheduler, BenchmarkState &state,
                 size_t &next_block, size_t pair)
{
    if (next_block == kStreamBlocks)
        return false;
    const size_t block = next_block++;
    scheduler.issueLoad(2 * pair, block, &state.streamB[block], 0);
    scheduler.issueLoad(2 * pair + 1, block, &state.streamC[block], 1);
    return true;
}

void
startStreamStoreIfReady(PersistentScheduler &scheduler,
                        BenchmarkState &state, size_t pair)
{
    const size_t bSlotIndex = 2 * pair;
    const size_t cSlotIndex = 2 * pair + 1;
    Slot &bSlot = scheduler.slot(bSlotIndex);
    Slot &cSlot = scheduler.slot(cSlotIndex);
    if (bSlot.phase != SlotPhase::LoadPending ||
        cSlot.phase != SlotPhase::LoadPending ||
        bSlot.stage != 0 || cSlot.stage != 1 ||
        bSlot.op != cSlot.op) {
        throw std::runtime_error("STREAM pair ownership/phase mismatch");
    }
    if (bSlot.id != 0 || cSlot.id != 0)
        return;

    StreamBlock *bValue = scheduler.payload<StreamBlock>(bSlotIndex);
    StreamBlock *cValue = scheduler.payload<StreamBlock>(cSlotIndex);
    for (size_t word = 0; word < 64; ++word) {
        cValue->words[word] =
            bValue->words[word] + 3 * cValue->words[word];
    }
    const size_t block = cSlot.op;
    scheduler.release(bSlotIndex);
    scheduler.readyToStore(cSlotIndex, &state.streamA[block]);
    scheduler.issueStore(cSlotIndex);
}

void
runStreamAmu(BenchmarkState &state, PersistentScheduler &scheduler)
{
    size_t nextBlock = 0;
    size_t completed = 0;
    const size_t pairCount = scheduler.capacity() / 2;
    if (pairCount == 0)
        throw std::runtime_error("STREAM requires two scheduler slots");
    for (size_t pair = 0; pair < pairCount; ++pair)
        refillStreamPair(scheduler, state, nextBlock, pair);

    while (completed != kStreamBlocks) {
        std::array<size_t, kCompletionBatch> completedSlots;
        const size_t completionCount =
            scheduler.waitCompletionBatch(completedSlots);
        // waitCompletionBatch clears every completed ID before returning.
        // Classify the whole batch before changing either slot in a pair:
        // B and C can both occur in one batch, and transitioning on B would
        // otherwise make C look like the newly issued store.
        std::array<bool, kWindowSlots / 2> completedLoadPairs{};
        for (size_t completionIndex = 0;
             completionIndex < completionCount; ++completionIndex) {
            const size_t slot_index = completedSlots[completionIndex];
            Slot &slot = scheduler.slot(slot_index);
            if (slot.phase == SlotPhase::LoadPending) {
                completedLoadPairs[slot_index / 2] = true;
            } else if (slot.phase == SlotPhase::StorePending) {
                if ((slot_index & 1) == 0 || slot.stage != 1)
                    throw std::runtime_error(
                        "STREAM store completed outside a C slot");
                const size_t pair = slot_index / 2;
                scheduler.release(slot_index);
                ++completed;
                refillStreamPair(scheduler, state, nextBlock, pair);
            } else {
                throw std::runtime_error(
                    "STREAM slot completed in wrong phase");
            }
        }
        for (size_t pair = 0; pair < pairCount; ++pair) {
            if (completedLoadPairs[pair])
                startStreamStoreIfReady(scheduler, state, pair);
        }
    }
    scheduler.requireDrained();
}

void
runKernelIteration(const Options &options, BenchmarkState &state,
                   PersistentScheduler *scheduler, size_t iteration)
{
    if (options.workload == "gups") {
        if (options.amu)
            runGupsAmu(state, *scheduler);
        else
            runGupsBaseline(state);
    } else if (options.workload == "hj") {
        if (options.amu)
            runHashJoinAmu(iteration, state, *scheduler);
        else
            runHashJoinBaseline(iteration, state);
    } else if (options.amu) {
        runStreamAmu(state, *scheduler);
    } else {
        runStreamBaseline(state);
    }
}

uint64_t
fold(const uint64_t current, const uint64_t value)
{
    return (current ^ value) * UINT64_C(0x100000001b3);
}

uint64_t
checksum(const Options &options, const BenchmarkState &state)
{
    uint64_t digest = UINT64_C(0xcbf29ce484222325);
    if (options.workload == "gups") {
        for (const auto value : state.gupsTable)
            digest = fold(digest, value);
    } else if (options.workload == "hj") {
        for (const auto value : state.hashResults)
            digest = fold(digest, value);
    } else {
        for (const auto &block : state.streamA)
            for (const auto value : block.words)
                digest = fold(digest, value);
    }
    return digest;
}

} // namespace

int
main(int argc, char **argv)
{
    try {
        const Options options = parseOptions(argc, argv);
        BenchmarkState state;
        prepareAndPrime(options, state);
        std::unique_ptr<PersistentScheduler> scheduler;
        if (options.amu) {
            scheduler = std::make_unique<PersistentScheduler>(
                workloadGranularity(options));
        }
        for (size_t iteration = 0; iteration < options.iterations;
             ++iteration) {
            m5_work_begin(iteration, 0);
            runKernelIteration(options, state, scheduler.get(), iteration);
            m5_work_end(iteration, 0);
        }
        uint64_t digest = checksum(options, state);
        (void)m5_sum(static_cast<unsigned>(digest),
                     static_cast<unsigned>(digest >> 32), kChecksumMagic,
                     workloadTag(options), options.amu ? 1 : 0, 0);
        std::printf("PROXY_CHECKSUM workload=%s kind=%s value=%016llx\n",
                    options.workload.c_str(), options.amu ? "amu" : "baseline",
                    static_cast<unsigned long long>(digest));
        m5_exit(0);
        return 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "AMU_PAPER_PROFILE_ERROR %s\n", error.what());
        m5_fail(0, 2);
        return 2;
    }
}
