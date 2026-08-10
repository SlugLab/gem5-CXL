// Copyright (c) 2026
// SPDX-License-Identifier: BSD-3-Clause

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include <immintrin.h>

#include <gem5/m5ops.h>

#include "amu.h"

namespace {

constexpr size_t kWindow = 256;
constexpr size_t kGupsEntries = 1 << 16;
constexpr size_t kHashBuckets = 16000;
constexpr size_t kHashDepth = 4;
constexpr size_t kStreamGranularity = 512;
constexpr size_t kStreamBlocks = 256;

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

void
flushRange(void *address, size_t size)
{
    auto *bytes = static_cast<uint8_t *>(address);
    for (size_t offset = 0; offset < size; offset += 64)
        _mm_clflush(bytes + offset);
    _mm_mfence();
}

void
configure(size_t granularity)
{
    amu_cfgwr(AMU_CFG_MAX_OUTSTANDING, kWindow);
    amu_cfgwr(AMU_CFG_GRANULARITY, granularity);
}

template <typename T>
struct alignas(64) Slot {
    T value;
};

template <typename T>
void
loadBatch(const std::vector<const T *> &addresses, std::vector<T> &values)
{
    if (addresses.size() > kWindow)
        throw std::runtime_error("AMU load batch exceeds the paper window");
    configure(sizeof(T));
    std::vector<Slot<T>> slots(addresses.size());
    std::vector<uint64_t> ids(addresses.size());
    for (auto &slot : slots)
        flushRange(&slot, sizeof(slot));
    for (size_t i = 0; i < addresses.size(); ++i) {
        ids[i] = amu_aload(&slots[i].value, addresses[i]);
        if (ids[i] == 0)
            throw std::runtime_error("AMU aload admission failed");
    }
    size_t complete = 0;
    while (complete != ids.size()) {
        const uint64_t id = amu_getfin();
        if (id == 0)
            continue;
        const auto found = std::find(ids.begin(), ids.end(), id);
        if (found == ids.end())
            throw std::runtime_error("AMU returned an unknown load ID");
        *found = 0;
        ++complete;
    }
    values.resize(addresses.size());
    for (size_t i = 0; i < addresses.size(); ++i)
        std::memcpy(&values[i], &slots[i].value, sizeof(T));
}

template <typename T>
void
storeBatch(const std::vector<T *> &addresses, const std::vector<T> &values)
{
    if (addresses.size() != values.size() || addresses.size() > kWindow)
        throw std::runtime_error("invalid AMU store batch");
    configure(sizeof(T));
    std::vector<Slot<T>> slots(addresses.size());
    std::vector<uint64_t> ids(addresses.size());
    for (size_t i = 0; i < addresses.size(); ++i) {
        std::memcpy(&slots[i].value, &values[i], sizeof(T));
        flushRange(&slots[i], sizeof(slots[i]));
        ids[i] = amu_astore(&slots[i].value, addresses[i]);
        if (ids[i] == 0)
            throw std::runtime_error("AMU astore admission failed");
    }
    size_t complete = 0;
    while (complete != ids.size()) {
        const uint64_t id = amu_getfin();
        if (id == 0)
            continue;
        const auto found = std::find(ids.begin(), ids.end(), id);
        if (found == ids.end())
            throw std::runtime_error("AMU returned an unknown store ID");
        *found = 0;
        ++complete;
    }
}

uint64_t
fold(const uint64_t current, const uint64_t value)
{
    return (current ^ value) * UINT64_C(0x100000001b3);
}

uint64_t
runGups(const Options &options)
{
    std::vector<uint64_t> table(kGupsEntries);
    for (size_t i = 0; i < table.size(); ++i)
        table[i] = UINT64_C(0x9e3779b97f4a7c15) ^ i;
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        for (size_t begin = 0; begin < table.size(); begin += kWindow) {
            const size_t count = std::min(kWindow, table.size() - begin);
            std::vector<size_t> indices(count);
            std::vector<const uint64_t *> reads(count);
            for (size_t i = 0; i < count; ++i) {
                indices[i] = ((begin + i) * 40503) & (table.size() - 1);
                reads[i] = &table[indices[i]];
            }
            std::vector<uint64_t> values(count);
            if (options.amu) {
                loadBatch(reads, values);
            } else {
                for (size_t i = 0; i < count; ++i)
                    values[i] = *reads[i];
            }
            std::vector<uint64_t *> writes(count);
            for (size_t i = 0; i < count; ++i) {
                values[i] ^= UINT64_C(0xd1b54a32d192ed03) ^ indices[i];
                writes[i] = &table[indices[i]];
            }
            if (options.amu) {
                storeBatch(writes, values);
            } else {
                for (size_t i = 0; i < count; ++i)
                    *writes[i] = values[i];
            }
        }
    }
    uint64_t checksum = UINT64_C(0xcbf29ce484222325);
    for (const auto value : table)
        checksum = fold(checksum, value);
    return checksum;
}

uint64_t
runHashJoin(const Options &options)
{
    std::vector<int32_t> buckets(kHashBuckets);
    std::vector<HashNode> nodes(kHashBuckets * kHashDepth);
    for (size_t bucket = 0; bucket < kHashBuckets; ++bucket) {
        buckets[bucket] = static_cast<int32_t>(bucket * kHashDepth);
        for (size_t depth = 0; depth < kHashDepth; ++depth) {
            const size_t index = bucket * kHashDepth + depth;
            nodes[index] = {};
            nodes[index].key = bucket * kHashDepth + depth;
            nodes[index].value = nodes[index].key ^ UINT64_C(0xa5a5a5a5);
            nodes[index].next = depth + 1 == kHashDepth ?
                -1 : static_cast<int32_t>(index + 1);
        }
    }
    const size_t queryCount = kWindow;
    std::vector<uint64_t> results(queryCount);
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        std::vector<int32_t> current(queryCount);
        std::vector<uint64_t> keys(queryCount);
        for (size_t query = 0; query < queryCount; ++query) {
            const size_t bucket = (query * 97 + iteration) % kHashBuckets;
            const size_t depth = (query + iteration) % kHashDepth;
            current[query] = buckets[bucket];
            keys[query] = bucket * kHashDepth + depth;
            results[query] = 0;
        }
        for (size_t depth = 0; depth < kHashDepth; ++depth) {
            std::vector<size_t> active;
            std::vector<const HashNode *> addresses;
            for (size_t query = 0; query < queryCount; ++query) {
                if (current[query] >= 0 && results[query] == 0) {
                    active.push_back(query);
                    addresses.push_back(&nodes[current[query]]);
                }
            }
            std::vector<HashNode> loaded(addresses.size());
            if (options.amu) {
                loadBatch(addresses, loaded);
            } else {
                for (size_t i = 0; i < addresses.size(); ++i)
                    loaded[i] = *addresses[i];
            }
            for (size_t i = 0; i < active.size(); ++i) {
                const size_t query = active[i];
                if (loaded[i].key == keys[query])
                    results[query] = loaded[i].value;
                current[query] = loaded[i].next;
            }
        }
    }
    uint64_t checksum = UINT64_C(0xcbf29ce484222325);
    for (const auto value : results)
        checksum = fold(checksum, value);
    return checksum;
}

uint64_t
runStream(const Options &options)
{
    std::vector<StreamBlock> a(kStreamBlocks), b(kStreamBlocks), c(kStreamBlocks);
    for (size_t block = 0; block < kStreamBlocks; ++block) {
        for (size_t word = 0; word < 64; ++word) {
            b[block].words[word] = block * 64 + word;
            c[block].words[word] = UINT64_C(0x100000001) + block + word;
        }
    }
    for (size_t iteration = 0; iteration < options.iterations; ++iteration) {
        for (size_t begin = 0; begin < kStreamBlocks; begin += kWindow / 2) {
            const size_t count = std::min(kWindow / 2, kStreamBlocks - begin);
            std::vector<const StreamBlock *> bAddresses(count), cAddresses(count);
            for (size_t i = 0; i < count; ++i) {
                bAddresses[i] = &b[begin + i];
                cAddresses[i] = &c[begin + i];
            }
            std::vector<StreamBlock> bValues(count), cValues(count), aValues(count);
            if (options.amu) {
                loadBatch(bAddresses, bValues);
                loadBatch(cAddresses, cValues);
            } else {
                for (size_t i = 0; i < count; ++i) {
                    bValues[i] = *bAddresses[i];
                    cValues[i] = *cAddresses[i];
                }
            }
            for (size_t i = 0; i < count; ++i)
                for (size_t word = 0; word < 64; ++word)
                    aValues[i].words[word] =
                        bValues[i].words[word] + 3 * cValues[i].words[word];
            std::vector<StreamBlock *> aAddresses(count);
            for (size_t i = 0; i < count; ++i)
                aAddresses[i] = &a[begin + i];
            if (options.amu) {
                storeBatch(aAddresses, aValues);
            } else {
                for (size_t i = 0; i < count; ++i)
                    *aAddresses[i] = aValues[i];
            }
        }
    }
    uint64_t checksum = UINT64_C(0xcbf29ce484222325);
    for (const auto &block : a)
        for (const auto value : block.words)
            checksum = fold(checksum, value);
    return checksum;
}

} // namespace

int
main(int argc, char **argv)
{
    try {
        const Options options = parseOptions(argc, argv);
        m5_work_begin(0, 0);
        uint64_t checksum = 0;
        if (options.workload == "gups")
            checksum = runGups(options);
        else if (options.workload == "hj")
            checksum = runHashJoin(options);
        else if (options.workload == "stream")
            checksum = runStream(options);
        m5_work_end(0, 0);
        const uint64_t written = m5_write_file(
            &checksum, sizeof(checksum), 0, options.rawOutput.c_str());
        if (written != sizeof(checksum))
            throw std::runtime_error("short proxy checksum write");
        std::printf("PROXY_CHECKSUM workload=%s kind=%s value=%016llx\n",
                    options.workload.c_str(), options.amu ? "amu" : "baseline",
                    static_cast<unsigned long long>(checksum));
        m5_exit(0);
        return 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "AMU_PAPER_PROFILE_ERROR %s\n", error.what());
        m5_fail(0, 2);
        return 2;
    }
}
