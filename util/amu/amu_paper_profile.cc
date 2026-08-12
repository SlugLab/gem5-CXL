// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
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
constexpr size_t kOwnerSets = 128;
constexpr size_t kOwnerWays = 4;
constexpr size_t kOwnerEntries = kOwnerSets * kOwnerWays;
constexpr size_t kOwnerNotFound = kOwnerEntries;
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
    std::vector<StreamBlock> streamStagedB;
};

enum class SlotPhase {
    Free,
    LoadPending,
    ReadyToStore,
    StorePending,
};

struct Slot {
    size_t op = 0;
    size_t spmOffset = 0;
    const void *source = nullptr;
    void *destination = nullptr;
    uint64_t id = 0;
    SlotPhase phase = SlotPhase::Free;
    uint8_t stage = 0;
};

struct IdOwner {
    uint64_t id = 0;
    uint16_t slot = 0;
    SlotPhase expected = SlotPhase::Free;
    bool live = false;
};

static_assert((kOwnerSets & (kOwnerSets - 1)) == 0,
              "owner set count must be a power of two");

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
        for (size_t index = 0; index < kWindowSlots; ++index)
            slots[index].spmOffset = index * stride;
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
        return reinterpret_cast<T *>(spmArena.data() + slot(index).spmOffset);
    }

    void issueLoad(size_t index, size_t op, const void *source, uint8_t stage)
    {
        Slot &entry = slot(index);
        if (entry.phase != SlotPhase::Free)
            throw std::runtime_error("load issued into a non-free slot");
        entry.op = op;
        entry.source = source;
        entry.destination = nullptr;
        entry.stage = stage;
        entry.phase = SlotPhase::LoadPending;
        entry.id = profileAload(
            spmArena.data() + entry.spmOffset, source);
        registerId(index, entry.id, SlotPhase::LoadPending);
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
        entry.id = profileAstore(
            spmArena.data() + entry.spmOffset, entry.destination);
        registerId(index, entry.id, SlotPhase::StorePending);
    }

    size_t waitCompletionBatch(
        std::array<size_t, kCompletionBatch> &completedSlots)
    {
        // Do not value-initialize this buffer immediately before the m5op.
        // O3 may retain those zeroing stores in its store queue while the
        // functional pseudo instruction writes the completed IDs, allowing
        // the older stores to overwrite or forward zeros after the m5op.
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
            if (owner_index == kOwnerNotFound) {
                throw std::runtime_error(
                    "AMU returned an unknown or stale ID");
            }
            const IdOwner owner = idOwners[owner_index];
            if (owner.slot >= activeSlots) {
                throw std::runtime_error(
                    "AMU completion has an invalid owner");
            }
            Slot &entry = slots[owner.slot];
            const SlotPhase expected = owner.expected;
            if (entry.id != owner.id ||
                (entry.id & kCompletionTokenMask) != token ||
                expected != entry.phase) {
                throw std::runtime_error(
                    "AMU completion owner/phase mismatch");
            }

            eraseOwner(owner_index);
            entry.id = 0;
            if (liveRequests == 0) {
                throw std::runtime_error(
                    "AMU live-request accounting underflow");
            }
            --liveRequests;
            completedSlots[index] = owner.slot;
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
        const size_t offset = entry.spmOffset;
        entry = {};
        entry.spmOffset = offset;
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
        const size_t set = static_cast<size_t>(token) & (kOwnerSets - 1);
        const size_t base = set * kOwnerWays;
        for (size_t way = 0; way < kOwnerWays; ++way) {
            const size_t location = base + way;
            const IdOwner &owner = idOwners[location];
            if (owner.live &&
                (owner.id & kCompletionTokenMask) == token)
                return location;
        }
        return kOwnerNotFound;
    }

    void insertOwner(const IdOwner &owner)
    {
        const uint64_t token = owner.id & kCompletionTokenMask;
        const size_t set = static_cast<size_t>(token) & (kOwnerSets - 1);
        const size_t base = set * kOwnerWays;
        size_t free_location = kOwnerNotFound;
        for (size_t way = 0; way < kOwnerWays; ++way) {
            const size_t location = base + way;
            const IdOwner &existing = idOwners[location];
            if (!existing.live) {
                if (free_location == kOwnerNotFound)
                    free_location = location;
            } else if ((existing.id & kCompletionTokenMask) == token) {
                throw std::runtime_error("AMU returned a duplicate live token");
            }
        }
        if (free_location == kOwnerNotFound)
            throw std::runtime_error("AMU owner set exceeds four live IDs");
        idOwners[free_location] = owner;
    }

    void eraseOwner(size_t location)
    {
        if (location >= kOwnerEntries || !idOwners[location].live)
            throw std::runtime_error("AMU owner location is invalid");
        idOwners[location] = {};
    }

    void registerId(size_t index, uint64_t id, SlotPhase expected)
    {
        if (id == 0)
            throw std::runtime_error("AMU request admission failed");
        if (liveRequests >= kWindowSlots)
            throw std::runtime_error("AMU request window exceeded 256 IDs");
        insertOwner(IdOwner{
            id, static_cast<uint16_t>(index), expected, true
        });
        ++liveRequests;
    }

    const size_t granularity;
    const size_t stride;
    const size_t activeSlots;
    size_t liveRequests = 0;
    std::array<Slot, kWindowSlots> slots{};
    std::array<IdOwner, kOwnerEntries> idOwners{};
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
        state.streamStagedB.resize(kStreamBlocks);
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
runGupsBaseline(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        for (size_t op = 0; op < state.gupsTable.size(); ++op) {
            const size_t index = (op * 40503) & (state.gupsTable.size() - 1);
            state.gupsTable[index] ^=
                UINT64_C(0xd1b54a32d192ed03) ^ index;
        }
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
    scheduler.issueLoad(slot_index, op, &state.gupsTable[index], 0);
    return true;
}

void
runGupsAmu(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        PersistentScheduler scheduler(sizeof(uint64_t));
        size_t nextOp = 0;
        size_t completed = 0;
        for (size_t slot = 0; slot < scheduler.capacity(); ++slot)
            refillGupsSlot(scheduler, state, nextOp, slot);

        while (completed != state.gupsTable.size()) {
            std::array<size_t, kCompletionBatch> completedSlots;
            const size_t completionCount =
                scheduler.waitCompletionBatch(completedSlots);
            for (size_t completionIndex = 0;
                 completionIndex < completionCount; ++completionIndex) {
                const size_t slot_index = completedSlots[completionIndex];
                Slot &slot = scheduler.slot(slot_index);
                const size_t index =
                    (slot.op * 40503) & (state.gupsTable.size() - 1);
                if (slot.phase == SlotPhase::LoadPending) {
                    uint64_t value = 0;
                    std::memcpy(
                        &value, scheduler.payload<uint64_t>(slot_index),
                        sizeof(value));
                    value ^= UINT64_C(0xd1b54a32d192ed03) ^ index;
                    std::memcpy(scheduler.payload<uint64_t>(slot_index),
                                &value, sizeof(value));
                    scheduler.readyToStore(
                        slot_index, &state.gupsTable[index]);
                    scheduler.issueStore(slot_index);
                } else if (slot.phase == SlotPhase::StorePending) {
                    scheduler.release(slot_index);
                    ++completed;
                    refillGupsSlot(
                        scheduler, state, nextOp, slot_index);
                } else {
                    throw std::runtime_error(
                        "GUPS slot completed in wrong phase");
                }
            }
        }
        scheduler.requireDrained();
    }
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
runHashJoinBaseline(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
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
}

void
runHashJoinAmu(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        initializeHashQueries(iteration, state);
        PersistentScheduler scheduler(sizeof(HashNode));
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
}

void
runStreamBaseline(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        for (size_t block = 0; block < kStreamBlocks; ++block) {
            for (size_t word = 0; word < 64; ++word) {
                state.streamA[block].words[word] =
                    state.streamB[block].words[word] +
                    3 * state.streamC[block].words[word];
            }
        }
    }
}

bool
refillStreamSlot(PersistentScheduler &scheduler, BenchmarkState &state,
                 size_t &next_block, size_t slot_index)
{
    if (next_block == kStreamBlocks)
        return false;
    const size_t block = next_block++;
    scheduler.issueLoad(slot_index, block, &state.streamB[block], 0);
    return true;
}

void
runStreamAmu(const Options &options, BenchmarkState &state)
{
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        PersistentScheduler scheduler(sizeof(StreamBlock));
        size_t nextBlock = 0;
        size_t completed = 0;
        for (size_t slot = 0; slot < scheduler.capacity(); ++slot)
            refillStreamSlot(scheduler, state, nextBlock, slot);

        while (completed != kStreamBlocks) {
            std::array<size_t, kCompletionBatch> completedSlots;
            const size_t completionCount =
                scheduler.waitCompletionBatch(completedSlots);
            for (size_t completionIndex = 0;
                 completionIndex < completionCount; ++completionIndex) {
                const size_t slot_index = completedSlots[completionIndex];
                Slot &slot = scheduler.slot(slot_index);
                const size_t block = slot.op;
                if (slot.phase == SlotPhase::LoadPending && slot.stage == 0) {
                    std::memcpy(&state.streamStagedB[block],
                                scheduler.payload<StreamBlock>(slot_index),
                                sizeof(StreamBlock));
                    scheduler.release(slot_index);
                    scheduler.issueLoad(
                        slot_index, block, &state.streamC[block], 1);
                } else if (slot.phase == SlotPhase::LoadPending &&
                           slot.stage == 1) {
                    StreamBlock value{};
                    std::memcpy(
                        &value, scheduler.payload<StreamBlock>(slot_index),
                        sizeof(value));
                    for (size_t word = 0; word < 64; ++word) {
                        value.words[word] =
                            state.streamStagedB[block].words[word] +
                            3 * value.words[word];
                    }
                    std::memcpy(scheduler.payload<StreamBlock>(slot_index),
                                &value, sizeof(value));
                    scheduler.readyToStore(
                        slot_index, &state.streamA[block]);
                    scheduler.issueStore(slot_index);
                } else if (slot.phase == SlotPhase::StorePending) {
                    scheduler.release(slot_index);
                    ++completed;
                    refillStreamSlot(
                        scheduler, state, nextBlock, slot_index);
                } else {
                    throw std::runtime_error(
                        "STREAM slot completed in wrong phase");
                }
            }
        }
        scheduler.requireDrained();
    }
}

void
runKernel(const Options &options, BenchmarkState &state)
{
    if (options.workload == "gups") {
        if (options.amu)
            runGupsAmu(options, state);
        else
            runGupsBaseline(options, state);
    } else if (options.workload == "hj") {
        if (options.amu)
            runHashJoinAmu(options, state);
        else
            runHashJoinBaseline(options, state);
    } else if (options.amu) {
        runStreamAmu(options, state);
    } else {
        runStreamBaseline(options, state);
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
        m5_work_begin(0, 0);
        runKernel(options, state);
        m5_work_end(0, 0);
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
