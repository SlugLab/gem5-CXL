/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/asmc.hh"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <utility>

#include "arch/generic/mmu.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/thread_context.hh"
#include "debug/ASMC.hh"
#include "mem/se_translating_port_proxy.hh"
#include "mem/translating_port_proxy.hh"
#include "sim/full_system.hh"
#include "sim/system.hh"

namespace gem5
{

std::unordered_map<System *, ASMC *> ASMC::registry;

ASMC::MemoryPort::MemoryPort(const std::string &name, ASMC &owner)
    : RequestPort(name), owner(owner)
{}

bool
ASMC::MemoryPort::recvTimingResp(PacketPtr pkt)
{
    return owner.recvTimingResp(pkt);
}

void
ASMC::MemoryPort::recvReqRetry()
{
    owner.recvReqRetry();
}

ASMC::ASMCStats::ASMCStats(statistics::Group *parent)
    : statistics::Group(parent),
      ADD_STAT(issuedLoads, statistics::units::Count::get(),
               "AMU aload requests issued"),
      ADD_STAT(issuedStores, statistics::units::Count::get(),
               "AMU astore requests issued"),
      ADD_STAT(completedLoads, statistics::units::Count::get(),
               "AMU aload requests completed"),
      ADD_STAT(completedStores, statistics::units::Count::get(),
               "AMU astore requests completed"),
      ADD_STAT(rejectedQueueFull, statistics::units::Count::get(),
               "AMU requests rejected because issue/send queues were full"),
      ADD_STAT(rejectedSpmFull, statistics::units::Count::get(),
               "AMU requests rejected because modeled SPM was full"),
      ADD_STAT(translationFaults, statistics::units::Count::get(),
               "AMU requests rejected because virtual translation failed"),
      ADD_STAT(readPackets, statistics::units::Count::get(),
               "Timing read packets sent by ASMC"),
      ADD_STAT(writePackets, statistics::units::Count::get(),
               "Timing write packets sent by ASMC"),
      ADD_STAT(readBytes, statistics::units::Byte::get(),
               "Timing read bytes sent by ASMC"),
      ADD_STAT(writeBytes, statistics::units::Byte::get(),
               "Timing write bytes sent by ASMC"),
      ADD_STAT(totalLatency, statistics::units::Tick::get(),
               "Total AMU request latency from m5op issue to finish queue"),
      ADD_STAT(avgLatency, statistics::units::Tick::get(),
               "Average AMU request latency",
               totalLatency / (completedLoads + completedStores)),
      ADD_STAT(translationCacheHits, statistics::units::Count::get(),
               "Translation cache hits for fast AMU operations"),
      ADD_STAT(translationCacheMisses, statistics::units::Count::get(),
               "Translation cache misses requiring full translation")
{}

ASMC::ASMC(const Params &p)
    : ClockedObject(p),
      system(p.system),
      memSidePort(name() + ".mem_side_port", *this),
      requestorId(system->getRequestorId(this)),
      spmSize(p.spm_size),
      cacheLineSize(p.cache_line_size),
      maxSendQueue(p.max_send_queue),
      issueLatency(p.issue_latency),
      completionLatency(p.completion_latency),
      granularity(p.default_granularity ? p.default_granularity : 1),
      maxOutstanding(p.max_outstanding),
      configuredLatency(p.asmc_latency),
      sendEvent([this] { trySend(); }, name()),
      stats(this)
{
    // Pre-allocate SPM buffer to avoid runtime resizing
    spmData.reserve(spmSize);
    // Reserve space for translation cache
    translationCache.reserve(maxCacheEntries);
}

ASMC::~ASMC()
{
    if (registry[system] == this)
        registry.erase(system);
    reset();
}

void
ASMC::init()
{
    panic_if(!memSidePort.isConnected(),
             "ASMC memory port of %s is not connected", name());
    panic_if(registry.count(system) && registry[system] != this,
             "Only one ASMC instance per System is currently supported");
    registry[system] = this;
    ClockedObject::init();
}

Port &
ASMC::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "mem_side_port")
        return memSidePort;
    return ClockedObject::getPort(if_name, idx);
}

ASMC *
ASMC::get(System *system)
{
    const auto it = registry.find(system);
    return it == registry.end() ? nullptr : it->second;
}

uint64_t
ASMC::issueAload(ThreadContext *tc, Addr spm_addr, Addr mem_addr)
{
    return issue(tc, ReqType::Load, spm_addr, mem_addr);
}

uint64_t
ASMC::issueAstore(ThreadContext *tc, Addr spm_addr, Addr mem_addr)
{
    return issue(tc, ReqType::Store, spm_addr, mem_addr);
}

bool
ASMC::translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                BaseMMU::Mode mode, std::vector<TranslationChunk> &chunks) const
{
    Addr offset = 0;
    auto gen = tc->getMMUPtr()->translateFunctional(vaddr, size, tc, mode, 0);

    for (const auto &range : *gen) {
        if (range.fault)
            return false;

        chunks.push_back({range.paddr, offset, range.size, range.flags});
        offset += range.size;
    }

    return offset == size;
}

// Translation cache lookup for fast path
bool
ASMC::tryCachedTranslation(Addr vaddr, uint64_t size,
                           TranslationCacheEntry &entry) const
{
    auto it = translationCache.find(vaddr);
    if (it != translationCache.end() && it->second.size >= size) {
        entry = it->second;
        return true;
    }
    return false;
}

// Cache translation result for future use
void
ASMC::cacheTranslation(Addr vaddr, const TranslationCacheEntry &entry)
{
    // Simple cache eviction when full
    if (translationCache.size() >= maxCacheEntries) {
        auto it = translationCache.begin();
        translationCache.erase(it);
    }
    translationCache[vaddr] = entry;
}

bool
ASMC::readGuest(ThreadContext *tc, Addr addr, void *data, uint64_t size)
{
    if (FullSystem) {
        TranslatingPortProxy proxy(tc);
        return proxy.tryReadBlob(addr, data, size);
    }

    SETranslatingPortProxy proxy(tc);
    return proxy.tryReadBlob(addr, data, size);
}

bool
ASMC::writeGuest(ThreadContext *tc, Addr addr, const void *data, uint64_t size)
{
    if (FullSystem) {
        TranslatingPortProxy proxy(tc);
        return proxy.tryWriteBlob(addr, data, size);
    }

    SETranslatingPortProxy proxy(tc);
    return proxy.tryWriteBlob(addr, data, size);
}

bool
ASMC::readSpm(Addr addr, void *data, uint64_t size) const
{
    // Check bounds
    if (addr + size > spmSize)
        return false;
    
    // Fast path: direct memcpy from contiguous buffer
    std::memcpy(data, spmData.data() + addr, size);
    return true;
}

void
ASMC::writeSpm(Addr addr, const void *data, uint64_t size)
{
    // Ensure buffer is large enough
    if (addr + size > spmData.size())
        spmData.resize(addr + size, 0);
    
    // Fast path: direct memcpy to contiguous buffer
    std::memcpy(spmData.data() + addr, data, size);
}

uint64_t
ASMC::issue(ThreadContext *tc, ReqType type, Addr spm_addr, Addr mem_addr)
{
    if (outstanding.size() >= maxOutstanding) {
        ++stats.rejectedQueueFull;
        return 0;
    }

    if (spmUsed + granularity > spmSize) {
        ++stats.rejectedSpmFull;
        return 0;
    }

    const uint64_t id = nextId++;
    auto state = std::make_unique<RequestState>();
    state->id = id;
    state->type = type;
    state->tc = tc;
    state->spmAddr = spm_addr;
    state->memAddr = mem_addr;
    state->size = granularity;
    state->issueTick = curTick();
    state->data.resize(granularity);
    state->pendingPackets = 0; // No packet-based completion tracking

    const auto mode = type == ReqType::Load ? BaseMMU::Read : BaseMMU::Write;

    // Try translation cache first (hybrid optimization)
    TranslationCacheEntry cached_entry;
    bool cache_hit = tryCachedTranslation(mem_addr, granularity, cached_entry);

    if (cache_hit) {
        // Fast path: use cached translation for bulk operation
        ++stats.translationCacheHits;
        DPRINTF(ASMC, "cache hit vaddr=%#llx paddr=%#llx size=%llu\n",
                static_cast<unsigned long long>(mem_addr),
                static_cast<unsigned long long>(cached_entry.paddr),
                static_cast<unsigned long long>(granularity));

        if (type == ReqType::Load) {
            // Direct read from cached physical address
            if (!readGuest(tc, cached_entry.paddr, state->data.data(), granularity)) {
                ++stats.translationFaults;
                return 0;
            }
            ++stats.readPackets;
            stats.readBytes += granularity;
        } else {
            // For stores, read from SPM first
            if (!readSpm(spm_addr, state->data.data(), granularity)) {
                ++stats.translationFaults;
                return 0;
            }
            // Direct write to cached physical address
            if (!writeGuest(tc, cached_entry.paddr, state->data.data(), granularity)) {
                ++stats.translationFaults;
                return 0;
            }
            ++stats.writePackets;
            stats.writeBytes += granularity;
        }
    } else {
        // Slow path: full translation and cache result
        ++stats.translationCacheMisses;
        DPRINTF(ASMC, "cache miss vaddr=%#llx - performing full translation\n",
                static_cast<unsigned long long>(mem_addr));

        std::vector<TranslationChunk> chunks;
        if (!translate(tc, mem_addr, granularity, mode, chunks)) {
            ++stats.translationFaults;
            return 0;
        }

        // Cache the translation result for future use
        if (chunks.size() == 1) {
            TranslationCacheEntry entry;
            entry.paddr = chunks[0].paddr;
            entry.size = chunks[0].size;
            entry.page_mask = 0xFFF; // Assume 4KB pages
            cacheTranslation(mem_addr, entry);
        }

        // Process chunks (existing slow path)
        for (const auto &chunk : chunks) {
            if (type == ReqType::Load) {
                uint8_t *data_ptr = state->data.data() + chunk.offset;
                if (!readGuest(tc, mem_addr + chunk.offset, data_ptr, chunk.size)) {
                    ++stats.translationFaults;
                    return 0;
                }
                ++stats.readPackets;
                stats.readBytes += chunk.size;
            } else {
                if (!readSpm(spm_addr + chunk.offset,
                            state->data.data() + chunk.offset, chunk.size)) {
                    ++stats.translationFaults;
                    return 0;
                }
                if (!writeGuest(tc, mem_addr + chunk.offset,
                              state->data.data() + chunk.offset, chunk.size)) {
                    ++stats.translationFaults;
                    return 0;
                }
                ++stats.writePackets;
                stats.writeBytes += chunk.size;
            }
        }
    }

    spmUsed += state->size;
    outstanding[id] = std::move(state);

    // Complete immediately for loads (data already in buffer), or schedule for stores
    if (type == ReqType::Load) {
        // Write to SPM and complete immediately
        writeSpm(spm_addr, outstanding[id]->data.data(), granularity);
        finished[tc].push_back(id);
        spmUsed -= granularity;
        stats.totalLatency += curTick() - outstanding[id]->issueTick;
        ++stats.completedLoads;
        outstanding.erase(id);

        DPRINTF(ASMC, "immediate complete id=%#llx type=aload latency=%llu\n",
                static_cast<unsigned long long>(id),
                static_cast<unsigned long long>(curTick() - outstanding[id]->issueTick));
    } else {
        // Schedule completion for stores with minimal latency
        auto *event = new EventFunctionWrapper(
            [this, id] { completeRequest(id); },
            csprintf("%s.complete_%llu", name(),
                     static_cast<unsigned long long>(id)),
            true);
        schedule(event, curTick() + 1); // Just 1 tick for ordering
    }

    if (type == ReqType::Load)
        ++stats.issuedLoads;
    else
        ++stats.issuedStores;

    DPRINTF(ASMC,
            "cached issue id=%#llx type=%s spm=%#llx mem=%#llx size=%llu\n",
            static_cast<unsigned long long>(id),
            type == ReqType::Load ? "aload" : "astore",
            static_cast<unsigned long long>(spm_addr),
            static_cast<unsigned long long>(mem_addr),
            static_cast<unsigned long long>(granularity));

    return id;
}

// Functions now defined as inline stubs in header (not used in fast path)

void
ASMC::completeRequest(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;

    finished[state.tc].push_back(id);
    spmUsed -= state.size;
    stats.totalLatency += curTick() - state.issueTick;

    if (state.type == ReqType::Load)
        ++stats.completedLoads;
    else
        ++stats.completedStores;

    DPRINTF(ASMC, "complete id=%#llx type=%s latency=%llu\n",
            static_cast<unsigned long long>(id),
            state.type == ReqType::Load ? "aload" : "astore",
            static_cast<unsigned long long>(curTick() - state.issueTick));

    outstanding.erase(it);
}

uint64_t
ASMC::getFinished(ThreadContext *tc)
{
    auto &queue = finished[tc];
    if (queue.empty())
        return 0;

    const uint64_t id = queue.front();
    queue.pop_front();
    return id;
}

uint64_t
ASMC::cfgWrite(ThreadContext *tc, uint64_t reg, uint64_t value)
{
    switch (reg) {
      case 0:
        granularity = value ? value : 1;
        return 1;
      case 1:
        maxOutstanding = value;
        return 1;
      case 2:
        configuredLatency = value * sim_clock::as_int::ns;
        return 1;
      case 3:
        reset();
        return 1;
      default:
        return 0;
    }
}

uint64_t
ASMC::cfgRead(ThreadContext *tc, uint64_t reg) const
{
    switch (reg) {
      case 0:
        return granularity;
      case 1:
        return maxOutstanding;
      case 2:
        return configuredLatency / sim_clock::as_int::ns;
      case 3:
        return outstanding.size();
      case 4: {
        const auto it = finished.find(tc);
        return it == finished.end() ? 0 : it->second.size();
      }
      case 5:
        return spmSize;
      case 6:
        return spmUsed;
      default:
        return 0;
    }
}

void
ASMC::deleteQueuedPacket(PacketPtr pkt)
{
    if (!pkt)
        return;

    delete pkt->senderState;
    pkt->senderState = nullptr;
    delete pkt;
}

void
ASMC::reset()
{
    while (!sendQueue.empty()) {
        deleteQueuedPacket(sendQueue.front());
        sendQueue.pop_front();
    }

    deleteQueuedPacket(retryPkt);
    retryPkt = nullptr;
    outstanding.clear();
    finished.clear();
    spmData.clear();
    spmUsed = 0;
    nextId = 1;
}

} // namespace gem5
