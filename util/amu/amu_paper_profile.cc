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
    uint16_t slot = 0;
    SlotPhase expected = SlotPhase::Free;
    bool live = false;
};

alignas(64) std::array<uint8_t, kSpmBytes> spmArena{};

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

class PersistentScheduler
{
  public:
    explicit PersistentScheduler(size_t granularity)
        : granularity(granularity), stride(slotStride(granularity)),
          activeSlots(activeSlotCount(granularity))
    {
        if (activeSlots == 0)
            throw std::runtime_error("SPM arena has no usable slots");
        configure(granularity);
        idOwners.reserve(kWindowSlots);
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
        entry.id = amu_aload(spmArena.data() + entry.spmOffset, source);
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
        _mm_mfence();
        entry.phase = SlotPhase::StorePending;
        entry.id = amu_astore(
            spmArena.data() + entry.spmOffset, entry.destination);
        registerId(index, entry.id, SlotPhase::StorePending);
    }

    size_t waitCompletion()
    {
        uint64_t id = 0;
        while (id == 0)
            id = amu_getfin();

        const auto owner_it = idOwners.find(id);
        if (owner_it == idOwners.end() || !owner_it->second.live)
            throw std::runtime_error("AMU returned an unknown or stale ID");
        const IdOwner owner = owner_it->second;
        if (owner.slot >= activeSlots)
            throw std::runtime_error("AMU completion has an invalid owner");
        Slot &entry = slots[owner.slot];
        const SlotPhase expected = owner.expected;
        if (entry.id != id || expected != entry.phase)
            throw std::runtime_error("AMU completion owner/phase mismatch");

        idOwners.erase(owner_it);
        entry.id = 0;
        if (liveRequests == 0)
            throw std::runtime_error("AMU live-request accounting underflow");
        --liveRequests;
        return owner.slot;
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
        if (liveRequests != 0 || !idOwners.empty() ||
            amu_cfgrd(AMU_CFG_OUTSTANDING) != 0 ||
            amu_cfgrd(AMU_CFG_FINISHED) != 0) {
            throw std::runtime_error("AMU scheduler did not drain completely");
        }
    }

  private:
    void registerId(size_t index, uint64_t id, SlotPhase expected)
    {
        if (id == 0)
            throw std::runtime_error("AMU request admission failed");
        if (liveRequests >= kWindowSlots)
            throw std::runtime_error("AMU request window exceeded 256 IDs");
        const auto inserted = idOwners.emplace(
            id, IdOwner{static_cast<uint16_t>(index), expected, true});
        if (!inserted.second)
            throw std::runtime_error("AMU returned a duplicate live ID");
        ++liveRequests;
    }

    const size_t granularity;
    const size_t stride;
    const size_t activeSlots;
    size_t liveRequests = 0;
    std::array<Slot, kWindowSlots> slots{};
    std::unordered_map<uint64_t, IdOwner> idOwners;
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

    if (options.amu)
        primeSpm(workloadGranularity(options));
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
            const size_t slot_index = scheduler.waitCompletion();
            Slot &slot = scheduler.slot(slot_index);
            const size_t index =
                (slot.op * 40503) & (state.gupsTable.size() - 1);
            if (slot.phase == SlotPhase::LoadPending) {
                uint64_t value = 0;
                std::memcpy(&value, scheduler.payload<uint64_t>(slot_index),
                            sizeof(value));
                value ^= UINT64_C(0xd1b54a32d192ed03) ^ index;
                std::memcpy(scheduler.payload<uint64_t>(slot_index),
                            &value, sizeof(value));
                scheduler.readyToStore(slot_index, &state.gupsTable[index]);
                scheduler.issueStore(slot_index);
            } else if (slot.phase == SlotPhase::StorePending) {
                scheduler.release(slot_index);
                ++completed;
                refillGupsSlot(scheduler, state, nextOp, slot_index);
            } else {
                throw std::runtime_error("GUPS slot completed in wrong phase");
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
            const size_t slot_index = scheduler.waitCompletion();
            Slot &slot = scheduler.slot(slot_index);
            if (slot.phase != SlotPhase::LoadPending)
                throw std::runtime_error("hash-join slot completed in wrong phase");
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
            const size_t slot_index = scheduler.waitCompletion();
            Slot &slot = scheduler.slot(slot_index);
            const size_t block = slot.op;
            if (slot.phase == SlotPhase::LoadPending && slot.stage == 0) {
                std::memcpy(&state.streamStagedB[block],
                            scheduler.payload<StreamBlock>(slot_index),
                            sizeof(StreamBlock));
                scheduler.release(slot_index);
                scheduler.issueLoad(
                    slot_index, block, &state.streamC[block], 1);
            } else if (slot.phase == SlotPhase::LoadPending && slot.stage == 1) {
                StreamBlock value{};
                std::memcpy(&value, scheduler.payload<StreamBlock>(slot_index),
                            sizeof(value));
                for (size_t word = 0; word < 64; ++word) {
                    value.words[word] =
                        state.streamStagedB[block].words[word] +
                        3 * value.words[word];
                }
                std::memcpy(scheduler.payload<StreamBlock>(slot_index), &value,
                            sizeof(value));
                scheduler.readyToStore(slot_index, &state.streamA[block]);
                scheduler.issueStore(slot_index);
            } else if (slot.phase == SlotPhase::StorePending) {
                scheduler.release(slot_index);
                ++completed;
                refillStreamSlot(scheduler, state, nextBlock, slot_index);
            } else {
                throw std::runtime_error("STREAM slot completed in wrong phase");
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
