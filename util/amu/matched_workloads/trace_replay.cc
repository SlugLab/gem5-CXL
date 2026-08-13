/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <fcntl.h>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include <omp.h>

#include "util/amu/matched_workloads/canonical_trace.hh"

#ifndef TRACE_REPLAY_NATIVE
#include "util/amu/amu.h"
#include "util/cira/cira.h"
#endif

namespace
{

using matched_trace::Opcode;
using matched_trace::TraceRecord;
using matched_trace::LoadDependencyRelativeFlag;

class DependencyTracker
{
  public:
    static constexpr size_t MaxOutOfOrder = 65536;

    void wait(uint64_t sequence)
    {
        std::unique_lock<std::mutex> lock(mutex);
        ready.wait(lock, [&] {
            return cancelled || sequence < frontier ||
                   outOfOrder.count(sequence) != 0;
        });
        if (cancelled)
            throw std::runtime_error("canonical dependency cancelled: " +
                                     cancellationReason);
    }

    void publish(uint64_t sequence)
    {
        {
            std::unique_lock<std::mutex> lock(mutex);
            ready.wait(lock, [&] {
                return cancelled || sequence <= frontier ||
                       outOfOrder.size() < MaxOutOfOrder;
            });
            if (cancelled)
                throw std::runtime_error(
                    "canonical dependency cancelled: " +
                    cancellationReason);
            if (sequence < frontier || outOfOrder.count(sequence) != 0)
                throw std::runtime_error(
                    "canonical dependency completion is duplicate");
            if (sequence == frontier) {
                ++frontier;
                while (outOfOrder.erase(frontier) != 0)
                    ++frontier;
            } else if (!outOfOrder.insert(sequence).second) {
                throw std::runtime_error(
                    "canonical dependency completion is duplicate");
            }
        }
        ready.notify_all();
    }

    void cancel(const std::string &reason)
    {
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!cancelled) {
                cancelled = true;
                cancellationReason = reason;
            }
        }
        ready.notify_all();
    }

  private:
    std::mutex mutex;
    std::condition_variable ready;
    uint64_t frontier = 0;
    std::unordered_set<uint64_t> outOfOrder;
    bool cancelled = false;
    std::string cancellationReason;
};

uint64_t
dependencySequence(const TraceRecord &record)
{
    if (record.operand1 == 0)
        return std::numeric_limits<uint64_t>::max();
    const bool relative =
        (record.operand1 & LoadDependencyRelativeFlag) != 0;
    const uint64_t encoded = relative ?
        record.operand1 & ~LoadDependencyRelativeFlag : record.operand1;
    if (encoded == 0 || (relative && encoded > record.sequence))
        throw std::runtime_error("canonical load dependency is invalid");
    const uint64_t dependency = relative ?
        record.sequence - encoded : encoded - 1;
    if (dependency >= record.sequence)
        throw std::runtime_error("canonical load dependency is not prior");
    return dependency;
}

struct Request
{
    uint64_t handle = 0;
    uint64_t bits = 0;
    uint64_t address = 0;
    uint32_t bytes = 0;
};

struct Commit
{
    uint64_t sequence = 0;
    uint64_t address = 0;
    uint64_t bits = 0;
};

struct OutputBoundary
{
    struct Probe {
        uint64_t address = 0;
        uint64_t afterSequence = 0;
    };
    std::string name;
    uint32_t wordBits = 0;
    std::vector<Probe> probes;
    std::vector<uint64_t> words;
    std::vector<bool> captured;
};

using BoundaryProbes = std::unordered_map<
    uint64_t, std::vector<std::pair<OutputBoundary *, size_t>>>;

struct ReplayStats
{
    uint64_t issuedLoads = 0;
    uint64_t completedLoads = 0;
    uint64_t drains = 0;
    uint64_t maxObservedOutstanding = 0;
    std::array<uint64_t, 4> issuedPerCore{};
    std::array<uint64_t, 4> completedPerCore{};
    std::array<bool, 4> workerThreads{};
};

struct WorkGroup
{
    uint16_t phase = 0;
    uint64_t workItem = 0;
    std::vector<size_t> recordIndices;
};

struct Phase
{
    uint16_t id = 0;
    std::vector<WorkGroup> groups;
};

uint64_t
loadRaw(const void *address, uint32_t bytes)
{
    uint64_t result = 0;
    if (bytes != 4 && bytes != 8)
        throw std::runtime_error("unsupported memory width");
    std::memcpy(&result, address, bytes);
    return result;
}

void
storeRaw(void *address, uint32_t bytes, uint64_t bits)
{
    if (bytes != 4 && bytes != 8)
        throw std::runtime_error("unsupported memory width");
    std::memcpy(address, &bits, bytes);
}

template <typename To, typename From>
To
bitCast(From value)
{
    static_assert(sizeof(To) == sizeof(From), "bit cast width differs");
    To result;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

class Memory
{
  public:
    Memory() = default;

    explicit Memory(const std::vector<TraceRecord> &records)
    {
        prepare(records);
    }

    void prepare(const std::vector<TraceRecord> &records)
    {
        for (const auto &record : records) {
            const auto opcode = static_cast<Opcode>(record.opcode);
            if (isMemory(opcode)) {
                const uint64_t line = lineAddress(record.address);
                if (lineIndices.count(line) == 0) {
                    lineIndices.emplace(line, lineAddresses.size());
                    lineAddresses.push_back(line);
                    lines.push_back(std::make_unique<DataLine>());
                }
                if (trackerIndices.count(record.address) == 0) {
                    trackerIndices.emplace(
                        record.address, trackerAddresses.size());
                    trackerAddresses.push_back(record.address);
                    trackers.push_back(
                        std::make_unique<std::atomic<uint64_t>>(NoStore));
                }
            }
        }
    }

    void initialize(uint64_t base, const std::string &path, uint64_t bytes)
    {
        std::ifstream stream(path, std::ios::binary);
        if (!stream)
            throw std::runtime_error("cannot open initial memory image");
        for (uint64_t offset = 0; offset < bytes;) {
            const uint64_t logical = base + offset;
            const uint64_t line = lineAddress(logical);
            if (lineIndices.count(line) == 0) {
                lineIndices.emplace(line, lineAddresses.size());
                lineAddresses.push_back(line);
                lines.push_back(std::make_unique<DataLine>());
            }
            const size_t count = std::min<uint64_t>(
                bytes - offset, 64 - lineOffset(logical));
            stream.read(static_cast<char *>(pointer(logical)), count);
            if (stream.gcount() != static_cast<std::streamsize>(count))
                throw std::runtime_error("initial memory image is truncated");
            offset += count;
        }
        char trailing;
        if (stream.get(trailing))
            throw std::runtime_error("initial memory image is oversized");
    }

    static bool isLoad(Opcode opcode)
    {
        return opcode == Opcode::LOAD_U32 || opcode == Opcode::LOAD_U64 ||
               opcode == Opcode::LOAD_F32 || opcode == Opcode::LOAD_F64;
    }

    static bool isStore(Opcode opcode)
    {
        return opcode == Opcode::STORE_U32 || opcode == Opcode::STORE_U64 ||
               opcode == Opcode::STORE_F32 || opcode == Opcode::STORE_F64;
    }

    static bool isMemory(Opcode opcode)
    {
        return isLoad(opcode) || isStore(opcode);
    }

    static uint32_t width(Opcode opcode)
    {
        switch (opcode) {
          case Opcode::LOAD_U32:
          case Opcode::LOAD_F32:
          case Opcode::STORE_U32:
          case Opcode::STORE_F32:
            return 4;
          case Opcode::LOAD_U64:
          case Opcode::LOAD_F64:
          case Opcode::STORE_U64:
          case Opcode::STORE_F64:
            return 8;
          default:
            throw std::runtime_error("operation is not a memory access");
        }
    }

    void *pointer(uint64_t logical)
    {
        const auto found = lineIndices.find(lineAddress(logical));
        if (found == lineIndices.end())
            throw std::runtime_error("logical address is not mapped");
        return lines[found->second]->bytes.data() + lineOffset(logical);
    }

    uint64_t load(uint64_t logical, uint32_t bytes)
    {
        return loadRaw(pointer(logical), bytes);
    }

    void store(uint64_t logical, uint32_t bytes, uint64_t bits)
    {
        storeRaw(pointer(logical), bytes, bits);
    }

    size_t allocatedBytes() const
    {
        return lineAddresses.size() * sizeof(DataLine);
    }

    void flushForRoi()
    {
        for (size_t index = 0; index < lineAddresses.size(); ++index) {
            __asm__ volatile("clflush (%0)" : : "r"(lines[index].get())
                             : "memory");
        }
        __asm__ volatile("mfence" : : : "memory");
    }

    void flushPrepared()
    {
        for (const size_t index : preparedLines) {
            __asm__ volatile("clflush (%0)" : : "r"(lines[index].get())
                             : "memory");
        }
        __asm__ volatile("mfence" : : : "memory");
        preparedLines.clear();
    }

    void waitForStore(uint64_t logical, uint64_t sequence)
    {
        if (sequence == NoStore)
            return;
        auto &tracker = trackerFor(logical);
        while (tracker.load(std::memory_order_acquire) != sequence)
            std::this_thread::yield();
    }

    void publishStore(uint64_t logical, uint64_t sequence)
    {
        trackerFor(logical).store(sequence, std::memory_order_release);
    }

    static constexpr uint64_t NoStore = ~uint64_t(0);

  private:
    struct alignas(64) DataLine
    {
        std::array<unsigned char, 64> bytes{};
    };

    static uint64_t lineAddress(uint64_t logical)
    {
        return logical & ~uint64_t(63);
    }

    static size_t lineOffset(uint64_t logical)
    {
        return size_t(logical & uint64_t(63));
    }

    std::atomic<uint64_t> &trackerFor(uint64_t logical)
    {
        const auto found = trackerIndices.find(logical);
        if (found == trackerIndices.end())
            throw std::runtime_error("logical address is not mapped");
        return *trackers[found->second];
    }

    std::vector<uint64_t> lineAddresses;
    std::unordered_map<uint64_t, size_t> lineIndices;
    std::vector<std::unique_ptr<DataLine>> lines;
    std::vector<uint64_t> trackerAddresses;
    std::unordered_map<uint64_t, size_t> trackerIndices;
    std::vector<std::unique_ptr<std::atomic<uint64_t>>> trackers;
    std::unordered_set<size_t> preparedLines;
};

class Accessor
{
  public:
    explicit Accessor(Memory &memory) : memory(memory) {}
    virtual ~Accessor() = default;
    virtual Request load(uint64_t address, uint32_t bytes, uint64_t slot) = 0;
    virtual uint64_t collect(Request request) = 0;
    virtual void store(uint64_t address, uint32_t bytes, uint64_t bits)
    {
        memory.store(address, bytes, bits);
    }
    virtual void drain() = 0;

  protected:
    Memory &memory;
};

class VanillaAccessor : public Accessor
{
  public:
    using Accessor::Accessor;

    Request load(uint64_t address, uint32_t bytes, uint64_t slot) override
    {
        return Request{slot + 1, memory.load(address, bytes), address, bytes};
    }

    uint64_t collect(Request request) override { return request.bits; }
    void drain() override {}
};

class AmuAccessor : public Accessor
{
  public:
    explicit AmuAccessor(Memory &memory) : Accessor(memory)
    {
#ifndef TRACE_REPLAY_NATIVE
        const size_t cacheLineBytes =
            amu_cfgrd(AMU_CFG_CACHE_LINE_BYTES);
        const size_t farQueuePackets =
            amu_cfgrd(AMU_CFG_FAR_SEND_QUEUE_PACKETS);
        const size_t spmQueuePackets =
            amu_cfgrd(AMU_CFG_SPM_SEND_QUEUE_PACKETS);
        if (cacheLineBytes != 64 || farQueuePackets == 0 ||
            spmQueuePackets == 0) {
            throw std::runtime_error("AMU queue geometry is unavailable");
        }
        constexpr size_t maxGranularity = sizeof(uint64_t);
        const size_t maxFarPackets =
            (maxGranularity + 2 * cacheLineBytes - 2) / cacheLineBytes;
        const size_t spmPackets =
            (maxGranularity + cacheLineBytes - 1) / cacheLineBytes;
        windowSlots = std::min({
            slots.size(), farQueuePackets / maxFarPackets,
            spmQueuePackets / (2 * spmPackets)
        });
        if (windowSlots == 0 ||
            amu_cfgwr(AMU_CFG_MAX_OUTSTANDING, windowSlots) == 0) {
            throw std::runtime_error("AMU queue-safe window is unavailable");
        }
#endif
    }

    Request load(uint64_t address, uint32_t bytes, uint64_t slot) override
    {
#ifdef TRACE_REPLAY_NATIVE
        ++issued;
        ++active;
        maxObserved = std::max(maxObserved, active);
        return Request{slot + 1, memory.load(address, bytes), address, bytes};
#else
        (void)slot;
        if (active >= windowSlots)
            throw std::runtime_error("AMU window full before consumer reach");
        const size_t index = tail;
        tail = (tail + 1) % slots.size();
        ++active;
        auto &entry = slots[index];
        entry.bytes = bytes;
        entry.ready = false;
        __asm__ volatile("clflush (%0)" : : "r"(entry.spm.data()) : "memory");
        __asm__ volatile("mfence" : : : "memory");
        if (amu_cfgwr(AMU_CFG_GRANULARITY, bytes) == 0)
            throw std::runtime_error("AMU granularity configuration failed");
        entry.id = amu_aload(entry.spm.data(), memory.pointer(address));
        if (entry.id == 0)
            throw std::runtime_error("AMU load issue failed");
        ++issued;
        maxObserved = std::max(maxObserved, active);
        return Request{index + 1, 0, address, bytes};
#endif
    }

    uint64_t collect(Request request) override
    {
#ifdef TRACE_REPLAY_NATIVE
        if (active == 0)
            throw std::runtime_error("AMU completion has no active request");
        --active;
        ++completed;
        return request.bits;
#else
        if (request.handle == 0 || request.handle > slots.size())
            throw std::runtime_error("AMU request handle is invalid");
        auto &wanted = slots[request.handle - 1];
        while (!wanted.ready) {
            const uint64_t id = amu_getfin();
            if (id == 0)
                continue;
            bool matched = false;
            for (auto &entry : slots) {
                if (entry.id == id) {
                    entry.ready = true;
                    matched = true;
                    break;
                }
            }
            if (!matched)
                throw std::runtime_error("AMU completion id is unknown");
        }
        const uint64_t bits = loadRaw(wanted.spm.data(), wanted.bytes);
        wanted.id = 0;
        wanted.ready = false;
        --active;
        ++completed;
        return bits;
#endif
    }

    void drain() override
    {
        if (active != 0)
            throw std::runtime_error("AMU phase drained with live requests");
        ++drains;
    }

    uint64_t issued = 0;
    uint64_t completed = 0;
    uint64_t drains = 0;
    size_t maxObserved = 0;

  private:
#ifndef TRACE_REPLAY_NATIVE
    struct Slot
    {
        alignas(64) std::array<unsigned char, 64> spm{};
        uint64_t id = 0;
        uint32_t bytes = 0;
        bool ready = false;
    };
    std::array<Slot, 32> slots{};
    size_t windowSlots = 32;
    size_t tail = 0;
    size_t active = 0;
#else
    size_t windowSlots = 32;
    size_t active = 0;
#endif
};

class CiraAccessor : public Accessor
{
  public:
    using Accessor::Accessor;

    Request load(uint64_t address, uint32_t bytes, uint64_t slot) override
    {
        const int core = omp_get_thread_num();
        if (core < 0 || core >= 4)
            throw std::runtime_error("CIRA replay core is outside [0,4)");
#ifndef TRACE_REPLAY_NATIVE
        const uint64_t id = cira_prefetch(memory.pointer(address), bytes);
        if (id != 0 && !pendingIds.insert(id).second)
            throw std::runtime_error("CIRA prefetch id is duplicate");
#endif
        ++issuedPerCore[core];
        return Request{slot + 1, 0, address, bytes};
    }

    uint64_t collect(Request request) override
    {
        const int core = omp_get_thread_num();
        ++completedPerCore[core];
        const uint64_t bits = memory.load(request.address, request.bytes);
        reap(false);
        return bits;
    }

    void drain() override { reap(true); }

    std::array<uint64_t, 4> issuedPerCore{};
    std::array<uint64_t, 4> completedPerCore{};

  private:
    void reap(bool wait)
    {
#ifndef TRACE_REPLAY_NATIVE
        while (!pendingIds.empty()) {
            const uint64_t id = cira_getfin();
            if (id == 0) {
                if (!wait)
                    return;
                continue;
            }
            if (pendingIds.erase(id) != 1)
                throw std::runtime_error("CIRA completion id is unknown");
        }
#else
        (void)wait;
#endif
    }

#ifndef TRACE_REPLAY_NATIVE
    std::unordered_set<uint64_t> pendingIds;
#endif
};

uint64_t
evaluate(const TraceRecord &record)
{
    const auto opcode = static_cast<Opcode>(record.opcode);
    switch (opcode) {
      case Opcode::F32_ADD:
        return bitCast<uint32_t>(bitCast<float>(uint32_t(record.operand0)) +
                                 bitCast<float>(uint32_t(record.operand1)));
      case Opcode::F32_MUL:
        return bitCast<uint32_t>(bitCast<float>(uint32_t(record.operand0)) *
                                 bitCast<float>(uint32_t(record.operand1)));
      case Opcode::F32_DIV:
        return bitCast<uint32_t>(bitCast<float>(uint32_t(record.operand0)) /
                                 bitCast<float>(uint32_t(record.operand1)));
      case Opcode::F64_ADD:
        return bitCast<uint64_t>(bitCast<double>(record.operand0) +
                                 bitCast<double>(record.operand1));
      case Opcode::F64_MAX:
        return bitCast<uint64_t>(std::max(bitCast<double>(record.operand0),
                                          bitCast<double>(record.operand1)));
      case Opcode::F64_MUL:
        return bitCast<uint64_t>(bitCast<double>(record.operand0) *
                                 bitCast<double>(record.operand1));
      case Opcode::F64_SUB:
        return bitCast<uint64_t>(bitCast<double>(record.operand0) -
                                 bitCast<double>(record.operand1));
      case Opcode::F64_DIV:
        return bitCast<uint64_t>(bitCast<double>(record.operand0) /
                                 bitCast<double>(record.operand1));
      case Opcode::F64_SQRT:
        return bitCast<uint64_t>(std::sqrt(bitCast<double>(record.operand0)));
      case Opcode::F64_MOV:
        return record.operand0;
      case Opcode::F64_ABS:
        return bitCast<uint64_t>(std::fabs(bitCast<double>(record.operand0)));
      case Opcode::I64_ADD:
        return record.operand0 + record.operand1;
      case Opcode::I64_MIN:
        return std::min(int64_t(record.operand0), int64_t(record.operand1));
      case Opcode::BARRIER:
      case Opcode::COMMIT:
        return record.result;
      default:
        throw std::runtime_error(
            "operation is not arithmetic: opcode=" +
            std::to_string(record.opcode) + " sequence=" +
            std::to_string(record.sequence));
    }
}

std::vector<TraceRecord>
readTrace(const std::string &path)
{
    const int descriptor = ::open(path.c_str(), O_RDONLY);
    if (descriptor < 0)
        throw std::runtime_error("cannot open canonical trace");
    std::vector<TraceRecord> records;
    off_t offset = 0;
    while (true) {
        alignas(64) std::array<unsigned char, 64> payload;
        size_t received = 0;
        while (received < sizeof(TraceRecord)) {
            __asm__ volatile("clflush (%0)" : : "r"(payload.data()) : "memory");
            __asm__ volatile("mfence" : : : "memory");
            const ssize_t count = ::pread(
                descriptor, payload.data() + received,
                sizeof(TraceRecord) - received, offset + off_t(received));
            if (count < 0) {
                ::close(descriptor);
                throw std::runtime_error("canonical trace pread failed");
            }
            if (count == 0)
                break;
            received += size_t(count);
        }
        if (received == 0)
            break;
        if (received != sizeof(TraceRecord)) {
            ::close(descriptor);
            throw std::runtime_error("canonical trace byte count differs");
        }
        TraceRecord record;
        std::memcpy(&record, payload.data(), sizeof(record));
        records.push_back(record);
        offset += off_t(sizeof(record));
    }
    if (::close(descriptor) != 0)
        throw std::runtime_error("canonical trace close failed");
    if (records.empty())
        throw std::runtime_error("canonical trace is empty");
    for (size_t index = 0; index < records.size(); ++index) {
        if (records[index].sequence != index || records[index].reserved != 0)
            throw std::runtime_error("canonical trace sequence differs");
        if (records[index].opcode < static_cast<uint16_t>(Opcode::LOAD_U32) ||
            records[index].opcode > static_cast<uint16_t>(Opcode::F64_ABS)) {
            throw std::runtime_error(
                "canonical trace opcode differs at sequence " +
                std::to_string(index));
        }
        if (Memory::isLoad(static_cast<Opcode>(records[index].opcode)))
            (void)dependencySequence(records[index]);
    }
    return records;
}

uint8_t
hexNibble(char value)
{
    if (value >= '0' && value <= '9')
        return uint8_t(value - '0');
    if (value >= 'a' && value <= 'f')
        return uint8_t(value - 'a' + 10);
    throw std::runtime_error("boundary name encoding is invalid");
}

std::string
decodeHexName(const std::string &encoded)
{
    if (encoded.empty() || encoded.size() % 2 != 0)
        throw std::runtime_error("boundary name encoding is invalid");
    std::string name;
    name.reserve(encoded.size() / 2);
    for (size_t index = 0; index < encoded.size(); index += 2) {
        const char value = char(
            (hexNibble(encoded[index]) << 4) | hexNibble(encoded[index + 1]));
        if (value == '\0')
            throw std::runtime_error("boundary name contains NUL");
        name.push_back(value);
    }
    return name;
}

std::vector<OutputBoundary>
readBoundaryMap(const std::string &path)
{
    if (path.empty())
        return {};
    std::ifstream stream(path);
    if (!stream)
        throw std::runtime_error("cannot open canonical boundary map");
    std::string magic;
    uint64_t boundaryCount = 0;
    if (!(stream >> magic >> boundaryCount) || magic != "MTRBND2")
        throw std::runtime_error("canonical boundary map header differs");
    if (boundaryCount > (uint64_t(1) << 32))
        throw std::runtime_error("canonical boundary map count is invalid");
    std::vector<OutputBoundary> boundaries;
    std::unordered_set<std::string> names;
    boundaries.reserve(size_t(boundaryCount));
    for (uint64_t boundary = 0; boundary < boundaryCount; ++boundary) {
        std::string encodedName;
        uint64_t wordBits = 0;
        uint64_t wordCount = 0;
        if (!(stream >> encodedName >> wordBits >> wordCount) ||
            (wordBits != 32 && wordBits != 64) ||
            wordCount > (uint64_t(1) << 32)) {
            throw std::runtime_error("canonical boundary map shape differs");
        }
        const std::string name = decodeHexName(encodedName);
        if (!names.insert(name).second)
            throw std::runtime_error("canonical boundary name is duplicate");
        OutputBoundary output{name, uint32_t(wordBits), {}, {}, {}};
        output.probes.reserve(size_t(wordCount));
        for (uint64_t word = 0; word < wordCount; ++word) {
            uint64_t address = 0;
            uint64_t afterSequence = 0;
            if (!(stream >> address >> afterSequence))
                throw std::runtime_error(
                    "canonical boundary probe mapping differs");
            output.probes.push_back({address, afterSequence});
        }
        output.words.resize(size_t(wordCount));
        output.captured.resize(size_t(wordCount), false);
        boundaries.push_back(std::move(output));
    }
    std::string trailing;
    if (stream >> trailing)
        throw std::runtime_error("canonical boundary map has trailing data");
    return boundaries;
}

void
observeBoundarySequence(std::vector<OutputBoundary> &boundaries,
                        uint64_t sequence, Memory &memory)
{
    for (auto &boundary : boundaries) {
        for (size_t index = 0; index < boundary.probes.size(); ++index) {
            const auto &probe = boundary.probes[index];
            if (probe.afterSequence != sequence)
                continue;
            if (boundary.captured[index])
                throw std::runtime_error("canonical boundary probe is duplicate");
            boundary.words[index] = memory.load(
                probe.address, boundary.wordBits / 8);
            boundary.captured[index] = true;
        }
    }
}

BoundaryProbes
indexBoundaryProbes(std::vector<OutputBoundary> &boundaries, size_t records)
{
    BoundaryProbes result;
    for (auto &boundary : boundaries) {
        for (size_t index = 0; index < boundary.probes.size(); ++index) {
            const auto &probe = boundary.probes[index];
            if (probe.afterSequence >= records)
                throw std::runtime_error(
                    "canonical boundary after-sequence is out of range");
            result[probe.afterSequence].push_back({&boundary, index});
        }
    }
    return result;
}

void
readInitialMemoryMap(const std::string &path, Memory &memory)
{
    if (path.empty())
        throw std::runtime_error("canonical initial memory map is missing");
    std::ifstream stream(path);
    std::string magic;
    uint64_t count = 0;
    if (!(stream >> magic >> count) || magic != "MTRINI1")
        throw std::runtime_error("canonical initial memory map header differs");
    for (uint64_t index = 0; index < count; ++index) {
        uint64_t base = 0, wordBits = 0, words = 0;
        std::string imagePath;
        if (!(stream >> base >> wordBits >> words >> imagePath) ||
            (wordBits != 32 && wordBits != 64) ||
            words > std::numeric_limits<uint64_t>::max() / (wordBits / 8)) {
            throw std::runtime_error("canonical initial memory image differs");
        }
        memory.initialize(base, imagePath, words * (wordBits / 8));
    }
    std::string trailing;
    if (stream >> trailing)
        throw std::runtime_error("canonical initial memory map has trailing data");
}

bool
readExact(int descriptor, void *destination, size_t bytes, bool allowEof)
{
    auto *payload = static_cast<unsigned char *>(destination);
    for (size_t offset = 0; offset < bytes; offset += 64)
        __asm__ volatile("clflush (%0)" : : "r"(payload + offset) : "memory");
    __asm__ volatile("mfence" : : : "memory");
    size_t received = 0;
    while (received < bytes) {
        const ssize_t count = ::read(
            descriptor, payload + received, bytes - received);
        if (count < 0)
            throw std::runtime_error("canonical stream read failed");
        if (count == 0) {
            if (allowEof && received == 0)
                return false;
            throw std::runtime_error("canonical stream is truncated");
        }
        received += size_t(count);
    }
    return true;
}

std::vector<TraceRecord>
readStreamFrame(int descriptor, uint64_t &expectedSequence, bool &finished,
                bool &invocationEnd)
{
    struct Header
    {
        std::array<unsigned char, 8> magic;
        uint64_t records;
        uint64_t flags;
    };
    static_assert(sizeof(Header) == 24, "stream header ABI differs");
    constexpr std::array<unsigned char, 8> magic = {
        'M', 'T', 'R', 'C', 'V', '2', 0, 0
    };
    alignas(64) Header header{};
    if (!readExact(descriptor, &header, sizeof(header), true))
        throw std::runtime_error("canonical stream has no final frame");
    if (header.magic != magic || header.flags > 2 ||
        header.records > 4096 ||
        (header.flags != 0 && header.records != 0) ||
        (header.flags == 0 && header.records == 0)) {
        throw std::runtime_error("canonical stream frame header differs");
    }
    if (header.flags == 1) {
        finished = true;
        return {};
    }
    if (header.flags == 2) {
        invocationEnd = true;
        return {};
    }
    const size_t bytes = size_t(header.records) * sizeof(TraceRecord);
    void *rawPayload = nullptr;
    if (posix_memalign(&rawPayload, 64, bytes) != 0)
        throw std::runtime_error("canonical stream staging allocation failed");
    try {
        readExact(descriptor, rawPayload, bytes, false);
    } catch (...) {
        std::free(rawPayload);
        throw;
    }
    std::vector<TraceRecord> records(size_t(header.records));
    std::memcpy(records.data(), rawPayload, bytes);
    std::free(rawPayload);
    for (const auto &record : records) {
        if (record.sequence != expectedSequence++ || record.reserved != 0)
            throw std::runtime_error("canonical stream sequence differs");
        if (record.opcode < static_cast<uint16_t>(Opcode::LOAD_U32) ||
            record.opcode > static_cast<uint16_t>(Opcode::F64_ABS)) {
            throw std::runtime_error("canonical stream opcode differs");
        }
        if (Memory::isLoad(static_cast<Opcode>(record.opcode)))
            (void)dependencySequence(record);
    }
    return records;
}

std::unique_ptr<Accessor>
makeAccessor(const std::string &system, Memory &memory)
{
    if (system == "vanilla")
        return std::make_unique<VanillaAccessor>(memory);
    if (system == "amu")
        return std::make_unique<AmuAccessor>(memory);
    if (system == "cira")
        return std::make_unique<CiraAccessor>(memory);
    throw std::runtime_error("unknown replay system");
}

std::vector<Phase>
buildPhases(const std::vector<TraceRecord> &records)
{
    std::vector<Phase> phases;
    std::vector<std::unordered_map<uint64_t, size_t>> groupIndices;
    std::unordered_map<uint16_t, bool> closed;
    for (size_t recordIndex = 0; recordIndex < records.size(); ++recordIndex) {
        const auto &record = records[recordIndex];
        if (phases.empty() || phases.back().id != record.phase) {
            if (closed[record.phase])
                throw std::runtime_error("canonical phase is not contiguous");
            if (!phases.empty())
                closed[phases.back().id] = true;
            phases.push_back(Phase{record.phase, {}});
            groupIndices.emplace_back();
        }
        auto &phase = phases.back();
        auto &indices = groupIndices.back();
        auto found = indices.find(record.work_item);
        if (found == indices.end()) {
            const size_t index = phase.groups.size();
            indices.emplace(record.work_item, index);
            phase.groups.push_back(
                WorkGroup{record.phase, record.work_item, {}});
            found = indices.find(record.work_item);
        }
        phase.groups[found->second].recordIndices.push_back(recordIndex);
    }
    return phases;
}

std::vector<Phase>
buildOrderedPhases(const std::vector<TraceRecord> &records)
{
    if (records.empty())
        return {};
    Phase phase{records.front().phase, {}};
    for (size_t index = 0; index < records.size(); ++index)
        phase.groups.push_back(WorkGroup{
            records[index].phase, records[index].work_item, {index}});
    return {std::move(phase)};
}

void
captureAccessorStats(Accessor &accessor, ReplayStats &stats)
{
    if (auto *amu = dynamic_cast<AmuAccessor *>(&accessor)) {
        stats.issuedLoads += amu->issued;
        stats.completedLoads += amu->completed;
        stats.drains += amu->drains;
        stats.maxObservedOutstanding = std::max<uint64_t>(
            stats.maxObservedOutstanding, amu->maxObserved);
    }
    if (auto *cira = dynamic_cast<CiraAccessor *>(&accessor)) {
        for (size_t core = 0; core < 4; ++core) {
            stats.issuedPerCore[core] += cira->issuedPerCore[core];
            stats.completedPerCore[core] += cira->completedPerCore[core];
        }
    }
}

void
accumulateStats(ReplayStats &total, const ReplayStats &observed)
{
    total.issuedLoads += observed.issuedLoads;
    total.completedLoads += observed.completedLoads;
    total.drains += observed.drains;
    total.maxObservedOutstanding = std::max(
        total.maxObservedOutstanding, observed.maxObservedOutstanding);
    for (size_t core = 0; core < 4; ++core) {
        total.issuedPerCore[core] += observed.issuedPerCore[core];
        total.completedPerCore[core] += observed.completedPerCore[core];
        total.workerThreads[core] =
            total.workerThreads[core] || observed.workerThreads[core];
    }
}

void
executeGroup(const WorkGroup &group, const std::vector<TraceRecord> &records,
             Accessor &accessor, Memory &memory,
             DependencyTracker &dependencies,
             const std::unordered_map<uint64_t, uint64_t> &previousStore,
             const std::unordered_map<uint64_t, size_t> &commitSlots,
             std::vector<Commit> &commits,
             const BoundaryProbes *boundaryProbes)
{
    const auto waitForPreviousStore = [&](const TraceRecord &operation) {
        const auto found = previousStore.find(operation.sequence);
        memory.waitForStore(
            operation.address,
            found == previousStore.end() ? Memory::NoStore : found->second);
    };
    size_t index = 0;
    while (index < group.recordIndices.size()) {
        const auto *record = &records[group.recordIndices[index]];
        const auto opcode = static_cast<Opcode>(record->opcode);
        if (!Memory::isStore(opcode) && opcode != Opcode::BARRIER &&
            opcode != Opcode::COMMIT) {
            const size_t begin = index;
            size_t end = begin;
            std::vector<Request> requests;
            std::unordered_set<uint64_t> pendingSequences;
            while (end < group.recordIndices.size()) {
                const auto *candidate = &records[group.recordIndices[end]];
                const auto candidateOpcode =
                    static_cast<Opcode>(candidate->opcode);
                if (Memory::isStore(candidateOpcode) ||
                    candidateOpcode == Opcode::BARRIER ||
                    candidateOpcode == Opcode::COMMIT) {
                    break;
                }
                if (Memory::isLoad(candidateOpcode)) {
                    if (requests.size() == 32)
                        break;
                    const uint64_t dependency =
                        dependencySequence(*candidate);
                    if (pendingSequences.count(dependency) != 0)
                        break;
                    if (dependency != std::numeric_limits<uint64_t>::max())
                        dependencies.wait(dependency);
                    waitForPreviousStore(*candidate);
                    requests.push_back(accessor.load(
                        candidate->address, Memory::width(candidateOpcode),
                        candidate->sequence));
                }
                pendingSequences.insert(candidate->sequence);
                ++end;
            }
            if (!requests.empty()) {
                size_t requestIndex = 0;
                for (size_t position = begin; position < end; ++position) {
                    const auto *operation =
                        &records[group.recordIndices[position]];
                    const auto operationOpcode =
                        static_cast<Opcode>(operation->opcode);
                    uint64_t observed = operation->result;
                    if (Memory::isLoad(operationOpcode)) {
                        observed = accessor.collect(requests[requestIndex++]);
                    } else {
                        observed = evaluate(*operation);
                    }
                    if (observed != operation->result) {
                        throw std::runtime_error(
                            "bit-exact result differs at canonical sequence " +
                            std::to_string(operation->sequence));
                    }
                    dependencies.publish(operation->sequence);
                }
                index = end;
                continue;
            }
        }

        uint64_t observed = record->result;
        if (Memory::isStore(opcode)) {
            waitForPreviousStore(*record);
            observed = record->operand0;
            accessor.store(record->address, Memory::width(opcode),
                           observed);
            memory.publishStore(record->address, record->sequence);
        } else {
            observed = evaluate(*record);
            if (opcode == Opcode::COMMIT) {
                const auto found = commitSlots.find(record->sequence);
                if (found == commitSlots.end())
                    throw std::runtime_error("commit slot is missing");
                commits[found->second] =
                    Commit{record->sequence, record->address, observed};
            }
        }
        if (!Memory::isStore(opcode) && observed != record->result) {
            throw std::runtime_error(
                "bit-exact result differs at canonical sequence " +
                std::to_string(record->sequence));
        }
        dependencies.publish(record->sequence);
        if (boundaryProbes) {
            const auto probes = boundaryProbes->find(record->sequence);
            if (probes != boundaryProbes->end()) {
                for (const auto &[boundary, word] : probes->second) {
                    const auto &probe = boundary->probes[word];
                    boundary->words[word] = memory.load(
                        probe.address, boundary->wordBits / 8);
                }
            }
        }
        ++index;
    }
}

ReplayStats
executeTrace(const std::string &system, const std::vector<Phase> &phases,
             const std::vector<TraceRecord> &records, Memory &memory,
             DependencyTracker &dependencies,
             const std::unordered_map<uint64_t, uint64_t> &previousStore,
             const std::unordered_map<uint64_t, size_t> &commitSlots,
             std::vector<Commit> &commits,
             const BoundaryProbes *boundaryProbes = nullptr)
{
    ReplayStats total;
    omp_set_dynamic(0);
    for (const auto &phase : phases) {
        std::array<ReplayStats, 4> threadStats{};
        std::atomic<bool> failed{false};
        std::string failure;
        std::mutex failureMutex;
#pragma omp parallel num_threads(4)
        {
            const int core = omp_get_thread_num();
            threadStats[core].workerThreads[core] = true;
            auto accessor = makeAccessor(system, memory);
#pragma omp for ordered schedule(static)
            for (size_t group = 0; group < phase.groups.size(); ++group) {
                if (failed.load(std::memory_order_relaxed))
                    continue;
                try {
                    if (boundaryProbes) {
#pragma omp ordered
                        executeGroup(
                            phase.groups[group], records, *accessor, memory,
                            dependencies,
                            previousStore, commitSlots, commits,
                            boundaryProbes);
                    } else {
                        executeGroup(
                            phase.groups[group], records, *accessor, memory,
                            dependencies,
                            previousStore, commitSlots, commits, nullptr);
                    }
                } catch (const std::exception &error) {
                    dependencies.cancel(error.what());
                    failed.store(true, std::memory_order_relaxed);
                    std::lock_guard<std::mutex> guard(failureMutex);
                    if (failure.empty())
                        failure = error.what();
                }
            }
            try {
                accessor->drain();
                captureAccessorStats(*accessor, threadStats[core]);
            } catch (const std::exception &error) {
                dependencies.cancel(error.what());
                failed.store(true, std::memory_order_relaxed);
                std::lock_guard<std::mutex> guard(failureMutex);
                if (failure.empty())
                    failure = error.what();
            }
        }
        if (failed)
            throw std::runtime_error(failure);
        for (const auto &stats : threadStats)
            accumulateStats(total, stats);
    }
    return total;
}

std::vector<Phase>
selectGroups(const std::vector<Phase> &phases, uint64_t measureStart,
             bool warmup)
{
    std::vector<Phase> selected;
    for (const auto &phase : phases) {
        Phase filtered{phase.id, {}};
        for (const auto &group : phase.groups) {
            if ((group.workItem < measureStart) == warmup)
                filtered.groups.push_back(group);
        }
        if (!filtered.groups.empty())
            selected.push_back(std::move(filtered));
    }
    return selected;
}

void
writeResult(const std::string &path, const std::string &system,
            size_t traceRecords,
            const std::vector<Commit> &commits, size_t allocatedBytes,
            size_t phases, const ReplayStats &stats, const std::string &mode,
            const std::vector<OutputBoundary> &boundaries = {})
{
    std::ofstream stream(path);
    if (!stream)
        throw std::runtime_error("cannot create replay result");
    stream << "{\"allocated_bytes\":" << allocatedBytes
           << ",\"commit_order\":[";
    for (size_t index = 0; index < commits.size(); ++index) {
        if (index)
            stream << ',';
        stream << commits[index].sequence;
    }
    stream << "],\"raw_outputs\":[";
    for (size_t index = 0; index < commits.size(); ++index) {
        if (index)
            stream << ',';
        stream << commits[index].bits;
    }
    stream << "],\"system\":\"" << system << "\",\"mode\":\"" << mode
           << "\",\"threads\":4,\"phases\":" << phases
           << ",\"issued_loads\":" << stats.issuedLoads
           << ",\"completed_loads\":" << stats.completedLoads
           << ",\"drains\":" << stats.drains
           << ",\"max_observed_outstanding\":"
           << stats.maxObservedOutstanding << ",\"issued_per_core\":[";
    for (size_t core = 0; core < 4; ++core) {
        if (core)
            stream << ',';
        stream << stats.issuedPerCore[core];
    }
    stream << "],\"completed_per_core\":[";
    for (size_t core = 0; core < 4; ++core) {
        if (core)
            stream << ',';
        stream << stats.completedPerCore[core];
    }
    stream << "],\"worker_threads\":[";
    bool first = true;
    for (size_t core = 0; core < 4; ++core) {
        if (!stats.workerThreads[core])
            continue;
        if (!first)
            stream << ',';
        stream << core;
        first = false;
    }
    stream << "],\"output_boundaries\":{";
    for (size_t boundary = 0; boundary < boundaries.size(); ++boundary) {
        if (boundary)
            stream << ',';
        stream << std::quoted(boundaries[boundary].name)
               << ":{\"word_bits\":" << boundaries[boundary].wordBits
               << ",\"count\":" << boundaries[boundary].words.size()
               << ",\"raw_words\":[";
        for (size_t word = 0; word < boundaries[boundary].words.size(); ++word) {
            if (word)
                stream << ',';
            stream << boundaries[boundary].words[word];
        }
        stream << "]}";
    }
    stream << "},\"trace_records\":" << traceRecords
           << ",\"verification\":\"pass\"}\n";
    if (!stream)
        throw std::runtime_error("replay result write failed");
}

void
executeStreamFile(const std::string &path, const std::string &system,
                  const std::string &resultPath,
                  const std::string &initialMemoryMap,
                  const std::string &boundaryMap)
{
    const int descriptor = ::open(path.c_str(), O_RDONLY);
    if (descriptor < 0)
        throw std::runtime_error("cannot open canonical stream");
    Memory memory;
    readInitialMemoryMap(initialMemoryMap, memory);
    auto boundaries = readBoundaryMap(boundaryMap);
    const BoundaryProbes orderedExecution;
    ReplayStats total;
    std::vector<Commit> commits;
    std::unordered_map<uint64_t, uint64_t> lastStore;
    DependencyTracker dependencies;
    uint64_t expectedSequence = 0;
    size_t phaseExecutions = 0;
    bool finished = false;
    uint64_t invocationCommit = 0;
    size_t commitsSinceMarker = 0;
    try {
        while (!finished) {
            bool invocationEnd = false;
            auto records = readStreamFrame(
                descriptor, expectedSequence, finished, invocationEnd);
            if (finished)
                break;
            if (invocationEnd) {
                if (commitsSinceMarker != 1)
                    throw std::runtime_error(
                        "canonical invocation COMMIT count differs");
                observeBoundarySequence(boundaries, invocationCommit, memory);
                commitsSinceMarker = 0;
                continue;
            }
            memory.prepare(records);
            memory.flushPrepared();
            std::unordered_map<uint64_t, uint64_t> previousStore;
            std::unordered_map<uint64_t, size_t> commitSlots;
            for (const auto &record : records) {
                const auto opcode = static_cast<Opcode>(record.opcode);
                if (Memory::isLoad(opcode) || Memory::isStore(opcode)) {
                    const auto prior = lastStore.find(record.address);
                    if (prior != lastStore.end())
                        previousStore.emplace(record.sequence, prior->second);
                    if (Memory::isStore(opcode))
                        lastStore[record.address] = record.sequence;
                }
                if (opcode == Opcode::COMMIT) {
                    invocationCommit = record.sequence;
                    ++commitsSinceMarker;
                    commitSlots.emplace(record.sequence, commits.size());
                    commits.push_back({});
                }
            }
            const auto phases = buildOrderedPhases(records);
            phaseExecutions += phases.size();
            const auto observed = executeTrace(
                system, phases, records, memory, dependencies, previousStore,
                commitSlots, commits, &orderedExecution);
            accumulateStats(total, observed);
        }
    } catch (...) {
        ::close(descriptor);
        throw;
    }
    if (::close(descriptor) != 0)
        throw std::runtime_error("canonical stream close failed");
    if (commitsSinceMarker != 0)
        throw std::runtime_error("canonical stream invocation marker is missing");
    for (const auto &boundary : boundaries) {
        if (std::find(boundary.captured.begin(), boundary.captured.end(), false)
            != boundary.captured.end()) {
            throw std::runtime_error("canonical boundary probe was not captured");
        }
        for (const auto &probe : boundary.probes) {
            if (probe.afterSequence >= expectedSequence)
                throw std::runtime_error(
                    "canonical boundary after-sequence is out of range");
        }
    }
    std::cout << "TRACE_REPLAY_ALLOCATION logical_bytes="
              << memory.allocatedBytes() << " allocated_bytes="
              << memory.allocatedBytes()
              << " all_memory_cxl=true" << std::endl;
    writeResult(resultPath, system, size_t(expectedSequence), commits,
                memory.allocatedBytes(), phaseExecutions, total,
                "functional", boundaries);
}

} // anonymous namespace

int
main(int argc, char **argv)
{
    try {
        std::string system;
        std::string tracePath;
        std::string resultPath;
        std::string mode = "functional";
        std::string windowManifest;
        std::string boundaryMap;
        std::string initialMemoryMap;
        uint64_t selectedPhase = 0;
        uint64_t windowIndex = 0;
        uint64_t measureStartItem = 0;
        bool hasPhase = false;
        bool hasWindowIndex = false;
        bool hasMeasureStartItem = false;
        bool streamMode = false;
        for (int index = 1; index < argc; ++index) {
            const std::string option = argv[index];
            if (index + 1 >= argc)
                throw std::runtime_error("replay option has no value");
            if (option == "--system")
                system = argv[++index];
            else if (option == "--trace")
                tracePath = argv[++index];
            else if (option == "--result")
                resultPath = argv[++index];
            else if (option == "--boundary-map")
                boundaryMap = argv[++index];
            else if (option == "--initial-memory-map")
                initialMemoryMap = argv[++index];
            else if (option == "--mode")
                mode = argv[++index];
            else if (option == "--window-manifest")
                windowManifest = argv[++index];
            else if (option == "--phase") {
                selectedPhase = std::stoull(argv[++index]);
                hasPhase = true;
            } else if (option == "--window-index") {
                windowIndex = std::stoull(argv[++index]);
                hasWindowIndex = true;
            } else if (option == "--measure-start-item") {
                measureStartItem = std::stoull(argv[++index]);
                hasMeasureStartItem = true;
            } else if (option == "--stream") {
                const std::string value = argv[++index];
                if (value != "0" && value != "1")
                    throw std::runtime_error("stream mode value is invalid");
                streamMode = value == "1";
            }
            else
                throw std::runtime_error("unknown replay option: " + option);
        }
        if (system.empty() || tracePath.empty() || resultPath.empty())
            throw std::runtime_error("replay options are incomplete");
        if (mode != "functional" && mode != "window")
            throw std::runtime_error("replay mode is invalid");
        if (streamMode && mode != "functional")
            throw std::runtime_error(
                "stream replay must be functional");
        if (mode == "functional" &&
            (!windowManifest.empty() || hasPhase || hasWindowIndex ||
             hasMeasureStartItem)) {
            throw std::runtime_error(
                "functional replay may not select a timing window");
        }
        if (mode == "window") {
            if (windowManifest.empty() || !hasPhase || !hasWindowIndex ||
                !hasMeasureStartItem)
                throw std::runtime_error("window replay selection is incomplete");
            std::ifstream manifest(windowManifest);
            if (!manifest)
                throw std::runtime_error("cannot open window manifest");
        }
#ifdef TRACE_REPLAY_NATIVE
        (void)selectedPhase;
        (void)windowIndex;
#endif

        if (streamMode) {
            executeStreamFile(
                tracePath, system, resultPath, initialMemoryMap, boundaryMap);
#ifndef TRACE_REPLAY_NATIVE
            m5_exit(0);
#endif
            return 0;
        }

        const auto records = readTrace(tracePath);
        auto outputBoundaries = readBoundaryMap(boundaryMap);
        if (mode == "window" && !outputBoundaries.empty())
            throw std::runtime_error("timing replay may not capture boundaries");
        Memory memory(records);
        readInitialMemoryMap(initialMemoryMap, memory);
        auto boundaryProbes = indexBoundaryProbes(
            outputBoundaries, records.size());
        std::cout << "TRACE_REPLAY_ALLOCATION logical_bytes="
                  << memory.allocatedBytes() << " allocated_bytes="
                  << memory.allocatedBytes()
                  << " all_memory_cxl=true" << std::endl;
        const auto phases = buildPhases(records);
        std::unordered_map<uint64_t, uint64_t> previousStore;
        std::unordered_map<uint64_t, uint64_t> lastStore;
        std::unordered_map<uint64_t, size_t> commitSlots;
        size_t commitCount = 0;
        for (const auto &record : records) {
            const auto opcode = static_cast<Opcode>(record.opcode);
            if (Memory::isLoad(opcode) || Memory::isStore(opcode)) {
                const auto found = lastStore.find(record.address);
                if (found != lastStore.end())
                    previousStore.emplace(record.sequence, found->second);
                if (Memory::isStore(opcode))
                    lastStore[record.address] = record.sequence;
            }
            if (opcode == Opcode::COMMIT)
                commitSlots.emplace(record.sequence, commitCount++);
        }
        std::vector<Commit> commits(commitCount);
        DependencyTracker dependencies;
        memory.flushForRoi();
        ReplayStats stats;
        size_t measuredPhases = phases.size();
        if (mode == "window") {
            const auto warmup = selectGroups(phases, measureStartItem, true);
            const auto measured = selectGroups(phases, measureStartItem, false);
            if (measured.empty())
                throw std::runtime_error("window measured range is empty");
            if (!warmup.empty()) {
                executeTrace(system, warmup, records, memory, dependencies,
                             previousStore, commitSlots, commits);
            }
#ifndef TRACE_REPLAY_NATIVE
            m5_work_begin(selectedPhase, windowIndex);
#endif
            stats = executeTrace(system, measured, records, memory,
                                 dependencies,
                                 previousStore, commitSlots, commits);
#ifndef TRACE_REPLAY_NATIVE
            m5_work_end(selectedPhase, windowIndex);
#endif
            measuredPhases = measured.size();
        } else {
            const BoundaryProbes *orderedProbes =
                outputBoundaries.empty() ? nullptr : &boundaryProbes;
            stats = executeTrace(system, phases, records, memory,
                                 dependencies,
                                 previousStore, commitSlots, commits,
                                 orderedProbes);
        }
        writeResult(resultPath, system, records.size(), commits,
                    memory.allocatedBytes(), measuredPhases, stats, mode,
                    outputBoundaries);
#ifndef TRACE_REPLAY_NATIVE
        m5_exit(0);
#endif
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "TRACE_REPLAY_ERROR " << error.what() << '\n';
#ifndef TRACE_REPLAY_NATIVE
        m5_fail(0, 2);
#endif
        return 2;
    }
}
