/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/cira.hh"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <unordered_set>
#include <utility>
#include <vector>

#include "arch/generic/mmu.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/thread_context.hh"
#include "debug/CIRA.hh"
#include "mem/cache/base.hh"
#include "mem/port_proxy.hh"
#include "sim/system.hh"

namespace gem5
{

namespace
{

constexpr uint64_t CiraCsrPrefetchRecords = 1ULL << 0;
constexpr uint64_t CiraCsrPrefetchValues = 1ULL << 1;
constexpr uint64_t CiraCsrOffsetsArePtrs = 1ULL << 2;
constexpr uint64_t CiraCsrRecordSpan = 1ULL << 3;

} // anonymous namespace

std::unordered_map<System *, CIRA *> CIRA::registry;

CIRA::MemoryPort::MemoryPort(const std::string &name, CIRA &owner,
                             PortID target_core)
    : RequestPort(name), owner(owner), targetCore(target_core)
{}

bool
CIRA::MemoryPort::recvTimingResp(PacketPtr pkt)
{
    return owner.recvTimingResp(targetCore, pkt);
}

void
CIRA::MemoryPort::recvReqRetry()
{
    owner.recvReqRetry(targetCore);
}

CIRA::CsrMemoryPort::CsrMemoryPort(const std::string &name, CIRA &owner)
    : RequestPort(name), owner(owner)
{}

bool
CIRA::CsrMemoryPort::recvTimingResp(PacketPtr pkt)
{
    return owner.recvCsrTimingResp(pkt);
}

void
CIRA::CsrMemoryPort::recvReqRetry()
{
    owner.recvCsrReqRetry();
}

CIRA::CIRAStats::CIRAStats(statistics::Group *parent, size_t num_cores)
    : statistics::Group(parent),
      ADD_STAT(issuedPrefetches, statistics::units::Count::get(),
               "CIRA cacheline install/prefetch requests issued"),
      ADD_STAT(issuedIndexedPrefetches, statistics::units::Count::get(),
               "CIRA indexed prefetch descriptors issued"),
      ADD_STAT(issuedCsrPrefetches, statistics::units::Count::get(),
               "CIRA CSR region prefetch descriptors issued"),
      ADD_STAT(csrRowsVisited, statistics::units::Count::get(),
               "CSR rows visited by CIRA region prefetch descriptors"),
      ADD_STAT(droppedCsrDescriptors, statistics::units::Count::get(),
               "CIRA CSR descriptors dropped because the walk queue was full"),
      ADD_STAT(csrQueueHighWatermark, statistics::units::Count::get(),
               "Maximum number of queued CIRA CSR walk descriptors"),
      ADD_STAT(completedPrefetches, statistics::units::Count::get(),
               "CIRA cacheline install/prefetch requests completed"),
      ADD_STAT(coalescedPrefetches, statistics::units::Count::get(),
               "CIRA cacheline candidates suppressed by tracked lines"),
      ADD_STAT(usefulPrefetches, statistics::units::Count::get(),
               "CPU data demands served after a CIRA-origin L2 fill"),
      ADD_STAT(latePrefetches, statistics::units::Count::get(),
               "CPU data demands arriving before a CIRA-origin L2 fill"),
      ADD_STAT(issuedCsrPrefetchesPerCore, statistics::units::Count::get(),
               "CIRA CSR descriptors accepted per target core"),
      ADD_STAT(issuedPrefetchesPerCore, statistics::units::Count::get(),
               "CIRA prefetch requests issued per target core"),
      ADD_STAT(completedPrefetchesPerCore, statistics::units::Count::get(),
               "CIRA prefetch requests completed per target core"),
      ADD_STAT(coalescedPrefetchesPerCore, statistics::units::Count::get(),
               "CIRA cacheline candidates coalesced per target core"),
      ADD_STAT(usefulPrefetchesPerCore, statistics::units::Count::get(),
               "CIRA useful prefetches per target core"),
      ADD_STAT(latePrefetchesPerCore, statistics::units::Count::get(),
               "CIRA late prefetches per target core"),
      ADD_STAT(rejectedDisabled, statistics::units::Count::get(),
               "CIRA requests rejected because the model is disabled"),
      ADD_STAT(rejectedQueueFull, statistics::units::Count::get(),
               "CIRA requests rejected because queues were full"),
      ADD_STAT(translationFaults, statistics::units::Count::get(),
               "CIRA requests rejected because virtual translation failed"),
      ADD_STAT(readPackets, statistics::units::Count::get(),
               "Timing prefetch packets sent by CIRA"),
      ADD_STAT(readBytes, statistics::units::Byte::get(),
               "Timing prefetch bytes sent by CIRA"),
      ADD_STAT(csrIndexReadPackets, statistics::units::Count::get(),
               "Device-side timing CSR index read packets sent"),
      ADD_STAT(csrIndexReadBytes, statistics::units::Byte::get(),
               "Device-side timing CSR index bytes sent"),
      ADD_STAT(completedCsrIndexReads, statistics::units::Count::get(),
               "CSR index values completed through the timing path"),
      ADD_STAT(rejectedCsrIndexQueueFull, statistics::units::Count::get(),
               "CSR index reads blocked by the bounded timing queue"),
      ADD_STAT(timingCsrTraversalEnabled, statistics::units::Count::get(),
               "One when timing CSR traversal is enabled"),
      ADD_STAT(totalLatency, statistics::units::Tick::get(),
               "Total CIRA request latency from m5op issue to completion"),
      ADD_STAT(avgLatency, statistics::units::Tick::get(),
               "Average CIRA request latency",
               totalLatency / completedPrefetches)
{
    issuedCsrPrefetchesPerCore.init(num_cores);
    issuedPrefetchesPerCore.init(num_cores);
    completedPrefetchesPerCore.init(num_cores);
    coalescedPrefetchesPerCore.init(num_cores);
    usefulPrefetchesPerCore.init(num_cores);
    latePrefetchesPerCore.init(num_cores);
    for (size_t core = 0; core < num_cores; ++core) {
        const std::string label = std::to_string(core);
        issuedCsrPrefetchesPerCore.subname(core, label);
        issuedPrefetchesPerCore.subname(core, label);
        completedPrefetchesPerCore.subname(core, label);
        coalescedPrefetchesPerCore.subname(core, label);
        usefulPrefetchesPerCore.subname(core, label);
        latePrefetchesPerCore.subname(core, label);
    }
}

CIRA::CIRA(const Params &p)
    : ClockedObject(p),
      system(p.system),
      csrMemoryPort(std::make_unique<CsrMemoryPort>(
          name() + ".csr_mem_side_port", *this)),
      requestorId(system->getRequestorId(this)),
      demandProbeTargets(p.demand_probe_targets),
      cacheLineSize(p.cache_line_size),
      maxSendQueue(p.max_send_queue),
      maxCsrWalkQueue(p.max_csr_walk_queue),
      maxCsrIndexReads(p.max_csr_index_reads),
      csrLinesPerTurn(p.csr_lines_per_turn),
      issueLatency(p.issue_latency),
      completionLatency(p.completion_latency),
      maxOutstanding(p.max_outstanding),
      enabled(p.enabled),
      timingCsrTraversal(p.timing_csr_traversal),
      sendEvent([this] { trySend(); }, name()),
      csrWalkEvent([this] { processCsrWalk(); }, name() + ".csr_walk"),
      csrIndexSendEvent(
          [this] { tryCsrIndexSend(); }, name() + ".csr_index_send"),
      stats(this, p.port_mem_side_ports_connection_count)
{
    const size_t numCores = p.port_mem_side_ports_connection_count;
    panic_if(numCores == 0, "CIRA %s requires at least one memory port", name());
    panic_if(maxCsrWalkQueue == 0,
             "CIRA %s requires a nonzero CSR walk queue", name());
    panic_if(csrLinesPerTurn == 0,
             "CIRA %s requires a nonzero CSR scheduling quantum", name());
    panic_if(maxCsrIndexReads == 0,
             "CIRA %s requires a nonzero CSR index-read limit", name());
    stats.timingCsrTraversalEnabled = timingCsrTraversal ? 1 : 0;

    memSidePorts.reserve(numCores);
    lineTrackers.reserve(numCores);
    for (PortID core = 0; core < numCores; ++core) {
        memSidePorts.emplace_back(std::make_unique<MemoryPort>(
            csprintf("%s.mem_side_ports[%d]", name(), core), *this, core));
        lineTrackers.emplace_back(cacheLineSize, p.max_completed_lines);
    }
    sendQueues.resize(numCores);
    retryPkts.resize(numCores, nullptr);
    retryReady.resize(numCores, false);
    csrWalkQueues.resize(numCores);
}

CIRA::~CIRA()
{
    probeListeners.clear();
    if (registry[system] == this)
        registry.erase(system);
    reset();
}

void
CIRA::init()
{
    for (PortID core = 0; core < memSidePorts.size(); ++core) {
        panic_if(!memSidePorts[core]->isConnected(),
                 "CIRA memory port %d of %s is not connected", core, name());
    }
    panic_if(!csrMemoryPort->isConnected(),
             "CIRA device-side CSR memory port of %s is not connected",
             name());
    panic_if(!demandProbeTargets.empty() &&
             demandProbeTargets.size() != memSidePorts.size(),
             "CIRA %s has %llu ports but %llu probe targets", name(),
             static_cast<unsigned long long>(memSidePorts.size()),
             static_cast<unsigned long long>(demandProbeTargets.size()));
    targetCaches.clear();
    targetCaches.reserve(demandProbeTargets.size());
    for (SimObject *target : demandProbeTargets) {
        auto *cache = dynamic_cast<BaseCache *>(target);
        panic_if(!cache, "CIRA %s demand probe target %s is not a cache",
                 name(), target->name());
        targetCaches.push_back(cache);
    }
    panic_if(registry.count(system) && registry[system] != this,
             "Only one CIRA instance per System is currently supported");
    registry[system] = this;
    ClockedObject::init();
}

void
CIRA::CacheProbeListener::notify(const CacheAccessProbeArg &arg)
{
    owner.handleCacheProbe(targetCore, event, arg);
}

void
CIRA::regProbeListeners()
{
    ClockedObject::regProbeListeners();
    probeListeners.clear();
    if (demandProbeTargets.empty())
        return;

    for (PortID core = 0; core < demandProbeTargets.size(); ++core) {
        ProbeManager *manager = demandProbeTargets[core]->getProbeManager();
        probeListeners.emplace_back(std::make_unique<CacheProbeListener>(
            *this, manager, "Hit", CacheProbeEvent::Hit, core));
        probeListeners.emplace_back(std::make_unique<CacheProbeListener>(
            *this, manager, "Miss", CacheProbeEvent::Miss, core));
        probeListeners.emplace_back(std::make_unique<CacheProbeListener>(
            *this, manager, "Fill", CacheProbeEvent::Fill, core));
    }
}

void
CIRA::resetStats()
{
    ClockedObject::resetStats();
    stats.timingCsrTraversalEnabled = timingCsrTraversal ? 1 : 0;
    for (auto &tracker : lineTrackers)
        tracker.clear();
}

Port &
CIRA::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "mem_side_ports") {
        panic_if(idx == InvalidPortID || idx >= memSidePorts.size(),
                 "CIRA %s invalid memory port index %d", name(), idx);
        return *memSidePorts[idx];
    }
    if (if_name == "csr_mem_side_port") {
        panic_if(idx != InvalidPortID,
                 "CIRA %s scalar CSR port received index %d", name(), idx);
        return *csrMemoryPort;
    }
    return ClockedObject::getPort(if_name, idx);
}

CIRA *
CIRA::get(System *system)
{
    const auto it = registry.find(system);
    return it == registry.end() ? nullptr : it->second;
}

bool
CIRA::translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                std::vector<TranslationChunk> &chunks) const
{
    Addr offset = 0;
    auto gen = tc->getMMUPtr()->translateFunctional(
        vaddr, size, tc, BaseMMU::Read, Request::PREFETCH);

    for (const auto &range : *gen) {
        if (range.fault)
            return false;

        Request::Flags flags = range.flags;
        flags.set(Request::PREFETCH);
        chunks.push_back({range.paddr, offset, range.size, flags});
        offset += range.size;
    }

    return offset == size;
}

bool
CIRA::readGuest(ThreadContext *tc, Addr addr, void *data,
                uint64_t size) const
{
    PortProxy proxy(tc, system->cacheLineSize());
    auto gen = tc->getMMUPtr()->translateFunctional(
        addr, size, tc, BaseMMU::Read, Request::Flags());

    auto *out = static_cast<uint8_t *>(data);
    for (const auto &range : *gen) {
        if (range.fault)
            return false;

        proxy.readBlobPhys(range.paddr, range.flags, out, range.size);
        out += range.size;
    }

    return true;
}

bool
CIRA::readIndex(ThreadContext *tc, Addr addr, uint64_t index_size,
                uint64_t &index) const
{
    if (index_size != 1 && index_size != 2 &&
        index_size != 4 && index_size != 8) {
        return false;
    }

    uint64_t raw = 0;
    if (!readGuest(tc, addr, &raw, index_size))
        return false;

    index = raw;
    return true;
}

bool
CIRA::hasPrefetchSlot(PortID targetCore) const
{
    if (!enabled)
        return false;

    panic_if(targetCore < 0 || targetCore >= sendQueues.size(),
             "CIRA %s invalid target core %d", name(), targetCore);
    const uint64_t queuedPackets = sendQueues[targetCore].size() +
        (retryPkts[targetCore] ? 1 : 0);
    return outstanding.size() < maxOutstanding &&
           queuedPackets < maxSendQueue;
}

PortID
CIRA::resolveTargetCore(ThreadContext *tc) const
{
    panic_if(!tc, "CIRA %s request has no thread context", name());
    const ContextID context = tc->contextId();
    panic_if(context < 0 || context >= memSidePorts.size(),
             "CIRA %s context %d has no target port (ports=%llu)", name(),
             context, static_cast<unsigned long long>(memSidePorts.size()));
    return static_cast<PortID>(context);
}

bool
CIRA::hasCsrWalks() const
{
    return std::any_of(csrWalkQueues.begin(), csrWalkQueues.end(),
                       [](const auto &queue) { return !queue.empty(); });
}

size_t
CIRA::queuedCsrWalks() const
{
    size_t queued = 0;
    for (const auto &queue : csrWalkQueues)
        queued += queue.size();
    return queued;
}

void
CIRA::scheduleCsrWalk(Tick when)
{
    if (!hasCsrWalks())
        return;

    if (csrWalkEvent.scheduled()) {
        if (when < csrWalkEvent.when())
            reschedule(csrWalkEvent, when);
        return;
    }

    schedule(csrWalkEvent, when);
}

CIRA::CsrWalkState *
CIRA::findCsrWalk(PortID targetCore, uint64_t walkId)
{
    panic_if(targetCore < 0 || targetCore >= csrWalkQueues.size(),
             "CIRA invalid CSR walk target core %d", targetCore);
    for (auto &walk : csrWalkQueues[targetCore]) {
        if (walk.walkId == walkId)
            return &walk;
    }
    return nullptr;
}

bool
CIRA::issueCsrIndexRead(CsrWalkState &walk, uint64_t entry)
{
    if (pendingCsrIndexReads.size() >= maxCsrIndexReads) {
        ++stats.rejectedCsrIndexQueueFull;
        return false;
    }
    if (walk.indexSize != 1 && walk.indexSize != 2 &&
        walk.indexSize != 4 && walk.indexSize != 8) {
        ++stats.translationFaults;
        return true;
    }

    const Addr indexAddr = walk.recordsBegin +
        entry * walk.recordStride + walk.indexOffset;
    std::vector<TranslationChunk> chunks;
    if (!translate(walk.tc, indexAddr, walk.indexSize, chunks)) {
        ++stats.translationFaults;
        return true;
    }

    const uint64_t id = nextCsrIndexReadId++;
    PendingCsrIndexRead pending;
    pending.walkId = walk.walkId;
    pending.targetCore = walk.targetCore;
    pending.tc = walk.tc;
    pending.valuesAddr = walk.valuesAddr;
    pending.valueSize = walk.valueSize;
    pending.indexSize = walk.indexSize;
    pending.pendingPackets = static_cast<uint32_t>(chunks.size());
    pendingCsrIndexReads.emplace(id, pending);
    ++walk.pendingIndexReads;

    for (const auto &chunk : chunks) {
        RequestPtr req = std::make_shared<Request>(
            chunk.paddr, chunk.size, chunk.flags, requestorId);
        PacketPtr pkt = new Packet(req, MemCmd::ReadReq);
        pkt->allocate();
        pkt->senderState = new PacketSenderState(
            PacketRole::CsrIndexRead, id, walk.targetCore, walk.walkId,
            entry, chunk.offset);
        enqueueCsrIndexPacket(pkt);
    }
    scheduleCsrIndexSend(curTick());
    return true;
}

bool
CIRA::finishCsrIndexRead(uint64_t id)
{
    const auto it = pendingCsrIndexReads.find(id);
    if (it == pendingCsrIndexReads.end())
        return true;
    PendingCsrIndexRead &pending = it->second;
    if (pending.pendingPackets != 0)
        return false;
    if (!hasPrefetchSlot(pending.targetCore))
        return false;

    uint64_t index = 0;
    for (uint64_t byte = 0; byte < pending.indexSize; ++byte)
        index |= static_cast<uint64_t>(pending.data[byte]) << (byte * 8);
    issuePrefetch(
        pending.tc,
        pending.valuesAddr + index * pending.valueSize,
        pending.valueSize);

    CsrWalkState *walk = findCsrWalk(
        pending.targetCore, pending.walkId);
    panic_if(!walk || walk->pendingIndexReads == 0,
             "CIRA completed CSR index read without owning walk");
    --walk->pendingIndexReads;
    ++stats.completedCsrIndexReads;
    pendingCsrIndexReads.erase(it);
    return true;
}

void
CIRA::enqueueCsrIndexPacket(PacketPtr pkt)
{
    csrIndexSendQueue.push_back(pkt);
}

void
CIRA::scheduleCsrIndexSend(Tick when)
{
    if (csrIndexRetryPkt && !csrIndexRetryReady)
        return;
    if (csrIndexSendEvent.scheduled()) {
        if (when < csrIndexSendEvent.when())
            reschedule(csrIndexSendEvent, when);
        return;
    }
    schedule(csrIndexSendEvent, when);
}

void
CIRA::tryCsrIndexSend()
{
    while ((csrIndexRetryPkt && csrIndexRetryReady) ||
           (!csrIndexRetryPkt && !csrIndexSendQueue.empty())) {
        PacketPtr pkt = csrIndexRetryPkt;
        if (!pkt) {
            pkt = csrIndexSendQueue.front();
            csrIndexSendQueue.pop_front();
        }
        if (!csrMemoryPort->sendTimingReq(pkt)) {
            csrIndexRetryPkt = pkt;
            csrIndexRetryReady = false;
            return;
        }
        csrIndexRetryPkt = nullptr;
        csrIndexRetryReady = false;
        ++stats.csrIndexReadPackets;
        stats.csrIndexReadBytes += pkt->req->getSize();
    }
}

bool
CIRA::recvCsrTimingResp(PacketPtr pkt)
{
    auto *senderState = dynamic_cast<PacketSenderState *>(pkt->senderState);
    panic_if(!senderState || senderState->role != PacketRole::CsrIndexRead,
             "CIRA CSR response without CSR-index sender state");
    const auto it = pendingCsrIndexReads.find(senderState->id);
    panic_if(it == pendingCsrIndexReads.end(),
             "CIRA response for unknown CSR index read %#llx",
             static_cast<unsigned long long>(senderState->id));
    PendingCsrIndexRead &pending = it->second;
    panic_if(pending.walkId != senderState->walkId ||
             pending.targetCore != senderState->targetCore ||
             senderState->dataOffset + pkt->getSize() > pending.indexSize ||
             pending.pendingPackets == 0,
             "CIRA malformed CSR index response state");
    std::memcpy(pending.data.data() + senderState->dataOffset,
                pkt->getConstPtr<uint8_t>(), pkt->getSize());
    --pending.pendingPackets;
    const uint64_t id = senderState->id;
    pkt->senderState = nullptr;
    delete senderState;
    delete pkt;

    if (!finishCsrIndexRead(id))
        scheduleCsrWalk(curTick() + 1);
    else if (hasCsrWalks())
        scheduleCsrWalk(curTick() + 1);
    return true;
}

void
CIRA::recvCsrReqRetry()
{
    panic_if(!csrIndexRetryPkt,
             "CIRA CSR retry without a blocked index packet");
    csrIndexRetryReady = true;
    scheduleCsrIndexSend(curTick());
}

void
CIRA::processCsrWalk()
{
    std::vector<uint64_t> readyReads;
    for (const auto &[id, pending] : pendingCsrIndexReads) {
        if (pending.pendingPackets == 0)
            readyReads.push_back(id);
    }
    for (uint64_t id : readyReads)
        finishCsrIndexRead(id);

    const PortID numCores = memSidePorts.size();
    const PortID startCore = nextCsrCore;
    bool consumedCandidate = false;

    for (PortID offset = 0; offset < numCores; ++offset) {
        const PortID targetCore = (startCore + offset) % numCores;
        auto &queue = csrWalkQueues[targetCore];
        if (queue.empty() || !hasPrefetchSlot(targetCore))
            continue;

        CsrWalkState &walk = queue.front();
        panic_if(walk.targetCore != targetCore,
                 "CIRA CSR walk queue/core ownership mismatch");
        uint64_t candidatesThisTurn = 0;

        while (candidatesThisTurn < csrLinesPerTurn &&
               hasPrefetchSlot(targetCore)) {
            bool consumedEntry = false;

            if (walk.prefetchRecords && walk.recordLine < walk.recordsEnd) {
                const Addr line = walk.recordLine;
                walk.recordLine += cacheLineSize;
                issuePrefetch(walk.tc, line, cacheLineSize);
                ++candidatesThisTurn;
                consumedCandidate = true;
                consumedEntry = true;
            }

            if (walk.prefetchValues && walk.nextEntry < walk.entryCount &&
                candidatesThisTurn < csrLinesPerTurn &&
                hasPrefetchSlot(targetCore) &&
                pendingCsrIndexReads.size() < maxCsrIndexReads) {
                const uint64_t entry = walk.nextEntry;
                if (!issueCsrIndexRead(walk, entry))
                    break;
                ++walk.nextEntry;
                ++candidatesThisTurn;
                consumedCandidate = true;
                consumedEntry = true;
            }

            if (!consumedEntry)
                break;
        }

        const bool recordsDone =
            !walk.prefetchRecords || walk.recordLine >= walk.recordsEnd;
        const bool valuesDone =
            !walk.prefetchValues ||
            (walk.nextEntry >= walk.entryCount &&
             walk.pendingIndexReads == 0);
        if (recordsDone && valuesDone) {
            DPRINTF(CIRA,
                    "csr walk complete records=[%#llx,%#llx) "
                    "rows=[%llu,%llu) entries=%llu\n",
                    static_cast<unsigned long long>(walk.recordsBegin),
                    static_cast<unsigned long long>(walk.recordsEnd),
                    static_cast<unsigned long long>(walk.rowStart),
                    static_cast<unsigned long long>(
                        walk.rowStart + walk.rowCount),
                    static_cast<unsigned long long>(walk.entryCount));
            queue.pop_front();
        }

        nextCsrCore = (targetCore + 1) % numCores;
    }

    bool canExpand = false;
    for (PortID core = 0; core < numCores; ++core) {
        canExpand = canExpand ||
            (!csrWalkQueues[core].empty() && hasPrefetchSlot(core));
    }
    if (hasCsrWalks() && consumedCandidate && canExpand)
        scheduleCsrWalk(curTick() + 1);
}

uint64_t
CIRA::issuePrefetch(ThreadContext *tc, Addr addr, uint64_t size)
{
    if (!enabled) {
        ++stats.rejectedDisabled;
        return 0;
    }

    const PortID targetCore = resolveTargetCore(tc);

    const uint64_t requestSize = size ? size : cacheLineSize;
    const Addr lineBase = addr & ~(cacheLineSize - 1);
    const Addr lineEnd = (addr + requestSize + cacheLineSize - 1) &
                         ~(cacheLineSize - 1);
    const uint64_t installSize = std::max<uint64_t>(cacheLineSize,
                                                    lineEnd - lineBase);

    std::vector<TranslationChunk> chunks;
    if (!translate(tc, lineBase, installSize, chunks)) {
        ++stats.translationFaults;
        return 0;
    }

    struct Candidate
    {
        Addr paddr;
        unsigned size;
        Request::Flags flags;
    };
    std::vector<Candidate> candidates;
    std::unordered_set<Addr> localLines;
    auto &tracker = lineTrackers.at(targetCore);

    for (const auto &chunk : chunks) {
        Addr chunkOffset = 0;
        while (chunkOffset < chunk.size) {
            const uint64_t lineRemaining =
                cacheLineSize - ((chunk.paddr + chunkOffset) %
                                 cacheLineSize);
            const auto pktSize = static_cast<unsigned>(std::min(
                chunk.size - chunkOffset, lineRemaining));
            const Addr paddr = chunk.paddr + chunkOffset;
            const Addr physicalLine = paddr - paddr % cacheLineSize;
            const bool secure = chunk.flags.isSet(Request::SECURE);
            const bool alreadyCovered = !targetCaches.empty() &&
                (targetCaches.at(targetCore)->inCache(physicalLine, secure) ||
                 targetCaches.at(targetCore)->inMissQueue(
                     physicalLine, secure));
            if (tracker.tracked(physicalLine) ||
                alreadyCovered || !localLines.insert(physicalLine).second) {
                ++stats.coalescedPrefetches;
                ++stats.coalescedPrefetchesPerCore[targetCore];
            } else {
                candidates.push_back({paddr, pktSize, chunk.flags});
            }
            chunkOffset += pktSize;
        }
    }

    if (candidates.empty())
        return 0;

    const uint64_t queuedPackets = sendQueues[targetCore].size() +
        (retryPkts[targetCore] ? 1 : 0);
    if (outstanding.size() >= maxOutstanding ||
        queuedPackets + candidates.size() > maxSendQueue) {
        ++stats.rejectedQueueFull;
        return 0;
    }

    const uint64_t id = nextId++;
    auto state = std::make_unique<RequestState>();
    state->id = id;
    state->targetCore = targetCore;
    state->tc = tc;
    state->vaddr = addr;
    state->size = installSize;
    state->issueTick = curTick();
    RequestState *rawState = state.get();
    outstanding[id] = std::move(state);

    for (const auto &candidate : candidates) {
        RequestPtr req = std::make_shared<Request>(
            candidate.paddr, candidate.size, candidate.flags, requestorId);
        req->taskId(context_switch_task_id::Prefetcher);
        panic_if(!lineTrackers.at(targetCore).issueIfAbsent(
                     req->getPaddr()),
                 "CIRA duplicate line admitted after coalescing");

        PacketPtr pkt = new Packet(req, MemCmd::SoftPFReq);
        pkt->allocate();
        pkt->senderState = new PacketSenderState(
            PacketRole::PrefetchLine, id, targetCore);
        rawState->pendingPackets++;
        enqueuePacket(targetCore, pkt);
    }

    ++stats.issuedPrefetches;
    ++stats.issuedPrefetchesPerCore[targetCore];

    DPRINTF(CIRA,
            "issue id=%#llx vaddr=%#llx size=%llu install_size=%llu "
            "target_core=%d chunks=%llu packets=%llu\n",
            static_cast<unsigned long long>(id),
            static_cast<unsigned long long>(addr),
            static_cast<unsigned long long>(size),
            static_cast<unsigned long long>(installSize),
            targetCore, static_cast<unsigned long long>(chunks.size()),
            static_cast<unsigned long long>(candidates.size()));

    scheduleSend(curTick() + issueLatency);
    return id;
}

uint64_t
CIRA::issueIndexedPrefetch(ThreadContext *tc, Addr base_addr,
                           Addr records_addr, uint64_t count,
                           uint64_t record_stride, uint64_t index_offset,
                           uint64_t index_size, uint64_t value_size)
{
    if (!enabled) {
        ++stats.rejectedDisabled;
        return 0;
    }
    const PortID targetCore = resolveTargetCore(tc);

    IndexedPrefetchDesc desc;
    desc.baseAddr = base_addr;
    desc.recordsAddr = records_addr;
    desc.count = count;
    desc.recordStride = record_stride;
    desc.indexOffset = index_offset;
    desc.indexSize = index_size;
    desc.valueSize = value_size;

    DPRINTF(CIRA,
            "indexed base=%#llx records=%#llx count=%llu "
            "stride=%llu index_offset=%llu index_size=%llu "
            "value_size=%llu\n",
            static_cast<unsigned long long>(desc.baseAddr),
            static_cast<unsigned long long>(desc.recordsAddr),
            static_cast<unsigned long long>(desc.count),
            static_cast<unsigned long long>(desc.recordStride),
            static_cast<unsigned long long>(desc.indexOffset),
            static_cast<unsigned long long>(desc.indexSize),
            static_cast<unsigned long long>(desc.valueSize));

    if (desc.count == 0 || desc.valueSize == 0 ||
        desc.recordStride == 0 || desc.indexSize == 0) {
        return 0;
    }

    ++stats.issuedIndexedPrefetches;

    uint64_t accepted = 0;
    for (uint64_t i = 0; i < desc.count; ++i) {
        if (!hasPrefetchSlot(targetCore)) {
            break;
        }

        uint64_t index = 0;
        const Addr indexAddr = desc.recordsAddr +
            i * desc.recordStride + desc.indexOffset;
        if (!readIndex(tc, indexAddr, desc.indexSize, index)) {
            ++stats.translationFaults;
            break;
        }

        const Addr target = desc.baseAddr + index * desc.valueSize;
        if (issuePrefetch(tc, target, desc.valueSize) != 0)
            ++accepted;
    }

    DPRINTF(CIRA,
            "indexed base=%#llx records=%#llx count=%llu "
            "stride=%llu index_offset=%llu index_size=%llu "
            "value_size=%llu accepted=%llu\n",
            static_cast<unsigned long long>(desc.baseAddr),
            static_cast<unsigned long long>(desc.recordsAddr),
            static_cast<unsigned long long>(desc.count),
            static_cast<unsigned long long>(desc.recordStride),
            static_cast<unsigned long long>(desc.indexOffset),
            static_cast<unsigned long long>(desc.indexSize),
            static_cast<unsigned long long>(desc.valueSize),
            static_cast<unsigned long long>(accepted));

    return accepted;
}

uint64_t
CIRA::issueCsrPrefetch(ThreadContext *tc, Addr offsets_addr,
                       Addr records_addr, Addr values_addr,
                       uint64_t row_start, uint64_t row_count,
                       uint64_t packed)
{
    if (!enabled) {
        ++stats.rejectedDisabled;
        return 0;
    }
    const PortID targetCore = resolveTargetCore(tc);

    CsrPrefetchDesc desc;
    desc.offsetsAddr = offsets_addr;
    desc.recordsAddr = records_addr;
    desc.valuesAddr = values_addr;
    desc.rowStart = row_start;
    desc.rowCount = row_count;
    desc.offsetSize = packed & 0xffULL;
    desc.recordStride = (packed >> 8) & 0xffffULL;
    desc.indexOffset = (packed >> 24) & 0xffffULL;
    desc.indexSize = (packed >> 40) & 0xffULL;
    desc.valueSize = (packed >> 48) & 0xffULL;
    desc.flags = (packed >> 56) & 0xffULL;

    DPRINTF(CIRA,
            "csr raw offsets=%#llx records=%#llx values=%#llx "
            "row_start=%llu row_count=%llu offset_size=%llu "
            "record_stride=%llu index_offset=%llu index_size=%llu "
            "value_size=%llu flags=%#llx packed=%#llx\n",
            static_cast<unsigned long long>(desc.offsetsAddr),
            static_cast<unsigned long long>(desc.recordsAddr),
            static_cast<unsigned long long>(desc.valuesAddr),
            static_cast<unsigned long long>(desc.rowStart),
            static_cast<unsigned long long>(desc.rowCount),
            static_cast<unsigned long long>(desc.offsetSize),
            static_cast<unsigned long long>(desc.recordStride),
            static_cast<unsigned long long>(desc.indexOffset),
            static_cast<unsigned long long>(desc.indexSize),
            static_cast<unsigned long long>(desc.valueSize),
            static_cast<unsigned long long>(desc.flags),
            static_cast<unsigned long long>(packed));

    const bool recordSpan = (desc.flags & CiraCsrRecordSpan) != 0;

    if (!recordSpan && (desc.rowCount == 0 || desc.offsetsAddr == 0 ||
        desc.recordsAddr == 0 || desc.recordStride == 0 ||
        (desc.offsetSize != 4 && desc.offsetSize != 8))) {
        DPRINTF(CIRA, "csr invalid descriptor\n");
        return 0;
    }

    const bool prefetchRecords =
        desc.flags == 0 || (desc.flags & CiraCsrPrefetchRecords) != 0;
    const bool prefetchValues =
        desc.valuesAddr != 0 && desc.valueSize != 0 &&
        desc.indexSize != 0 &&
        (desc.flags == 0 || (desc.flags & CiraCsrPrefetchValues) != 0);
    const bool offsetsArePtrs = (desc.flags & CiraCsrOffsetsArePtrs) != 0;

    if (recordSpan) {
        if (desc.offsetsAddr == 0 || desc.recordsAddr <= desc.offsetsAddr ||
            desc.recordStride == 0) {
            DPRINTF(CIRA, "csr invalid record span descriptor\n");
            return 0;
        }
        if (prefetchValues && !timingCsrTraversal) {
            ++stats.rejectedDisabled;
            DPRINTF(CIRA,
                    "csr timing traversal disabled for value descriptor\n");
            return 0;
        }

        const Addr rowRecordAddr = desc.offsetsAddr;
        const uint64_t recordBytes = desc.recordsAddr - desc.offsetsAddr;
        const uint64_t count = recordBytes / desc.recordStride;
        if (count == 0)
            return 0;

        if (queuedCsrWalks() >= maxCsrWalkQueue) {
            ++stats.rejectedQueueFull;
            ++stats.droppedCsrDescriptors;
            return 0;
        }

        ++stats.issuedCsrPrefetches;
        ++stats.issuedCsrPrefetchesPerCore[targetCore];
        stats.csrRowsVisited += desc.rowCount;

        CsrWalkState walk;
        walk.walkId = nextCsrWalkId++;
        walk.tc = tc;
        walk.targetCore = targetCore;
        walk.recordsBegin = rowRecordAddr;
        walk.recordsEnd = desc.recordsAddr;
        walk.recordLine = rowRecordAddr & ~(cacheLineSize - 1);
        walk.valuesAddr = desc.valuesAddr;
        walk.recordStride = desc.recordStride;
        walk.indexOffset = desc.indexOffset;
        walk.indexSize = desc.indexSize;
        walk.valueSize = desc.valueSize;
        walk.entryCount = count;
        walk.rowStart = desc.rowStart;
        walk.rowCount = desc.rowCount;
        walk.prefetchRecords = prefetchRecords;
        walk.prefetchValues = prefetchValues;
        csrWalkQueues[targetCore].push_back(walk);
        stats.csrQueueHighWatermark = std::max(
            queuedCsrWalks(),
            static_cast<size_t>(stats.csrQueueHighWatermark.value()));
        scheduleCsrWalk(curTick() + issueLatency);

        DPRINTF(CIRA,
                "csr span queued records=[%#llx,%#llx) values=%#llx "
                "stride=%llu index_offset=%llu index_size=%llu "
                "value_size=%llu flags=%#llx entries=%llu queued_walks=%llu\n",
                static_cast<unsigned long long>(desc.offsetsAddr),
                static_cast<unsigned long long>(desc.recordsAddr),
                static_cast<unsigned long long>(desc.valuesAddr),
                static_cast<unsigned long long>(desc.recordStride),
                static_cast<unsigned long long>(desc.indexOffset),
                static_cast<unsigned long long>(desc.indexSize),
                static_cast<unsigned long long>(desc.valueSize),
                static_cast<unsigned long long>(desc.flags),
                static_cast<unsigned long long>(count),
                static_cast<unsigned long long>(
                    csrWalkQueues[targetCore].size()));

        return count;
    }

    ++stats.issuedCsrPrefetches;
    ++stats.issuedCsrPrefetchesPerCore[targetCore];
    uint64_t accepted = 0;
    const uint64_t rowEnd = desc.rowStart + desc.rowCount;
    for (uint64_t row = desc.rowStart; row < rowEnd; ++row) {
        if (!hasPrefetchSlot(targetCore)) {
            break;
        }

        uint64_t begin = 0;
        uint64_t end = 0;
        const Addr offsetAddr = desc.offsetsAddr + row * desc.offsetSize;
        if (!readIndex(tc, offsetAddr, desc.offsetSize, begin) ||
            !readIndex(tc, offsetAddr + desc.offsetSize, desc.offsetSize,
                       end)) {
            ++stats.translationFaults;
            break;
        }

        if (end < begin)
            continue;

        ++stats.csrRowsVisited;

        const uint64_t span = end - begin;
        const uint64_t count = offsetsArePtrs ?
            span / desc.recordStride : span;
        if (count == 0)
            continue;

        const Addr rowRecordAddr = offsetsArePtrs ?
            static_cast<Addr>(begin) :
            desc.recordsAddr + begin * desc.recordStride;
        const uint64_t recordBytes = offsetsArePtrs ?
            span : count * desc.recordStride;

        if (prefetchRecords) {
            if (issuePrefetch(tc, rowRecordAddr, recordBytes) != 0)
                ++accepted;
        }

        if (!prefetchValues)
            continue;

        for (uint64_t entry = 0; entry < count; ++entry) {
            if (!hasPrefetchSlot(targetCore)) {
                break;
            }

            uint64_t index = 0;
            const Addr indexAddr = rowRecordAddr +
                entry * desc.recordStride + desc.indexOffset;
            if (!readIndex(tc, indexAddr, desc.indexSize, index)) {
                ++stats.translationFaults;
                break;
            }

            const Addr target = desc.valuesAddr + index * desc.valueSize;
            if (issuePrefetch(tc, target, desc.valueSize) != 0)
                ++accepted;
        }
    }

    DPRINTF(CIRA,
            "csr offsets=%#llx records=%#llx values=%#llx "
            "rows=[%llu,%llu) stride=%llu index_offset=%llu "
            "index_size=%llu value_size=%llu flags=%#llx accepted=%llu\n",
            static_cast<unsigned long long>(desc.offsetsAddr),
            static_cast<unsigned long long>(desc.recordsAddr),
            static_cast<unsigned long long>(desc.valuesAddr),
            static_cast<unsigned long long>(desc.rowStart),
            static_cast<unsigned long long>(rowEnd),
            static_cast<unsigned long long>(desc.recordStride),
            static_cast<unsigned long long>(desc.indexOffset),
            static_cast<unsigned long long>(desc.indexSize),
            static_cast<unsigned long long>(desc.valueSize),
            static_cast<unsigned long long>(desc.flags),
            static_cast<unsigned long long>(accepted));

    return accepted;
}

void
CIRA::enqueuePacket(PortID targetCore, PacketPtr pkt)
{
    auto &queue = sendQueues.at(targetCore);
    panic_if(queue.size() >= maxSendQueue,
             "CIRA send queue overflow after admission check");
    queue.push_back(pkt);
}

void
CIRA::scheduleSend(Tick when)
{
    if (sendEvent.scheduled()) {
        if (when < sendEvent.when())
            reschedule(sendEvent, when);
        return;
    }
    schedule(sendEvent, when);
}

void
CIRA::trySend()
{
    bool sendableWork = false;
    const PortID startCore = nextSendCore;
    for (size_t offset = 0; offset < memSidePorts.size(); ++offset) {
        const PortID targetCore =
            (startCore + offset) % memSidePorts.size();
        auto &queue = sendQueues[targetCore];
        PacketPtr &retryPkt = retryPkts[targetCore];
        const bool retrying = retryPkt != nullptr;

        if (retrying && !retryReady[targetCore])
            continue;
        if (!retrying && queue.empty())
            continue;

        PacketPtr pkt = retryPkt;
        if (!pkt) {
            pkt = queue.front();
            queue.pop_front();
        }

        DPRINTF(CIRA,
                "send core=%d addr=%#llx size=%u cmd=%s retry=%d "
                "queued=%llu\n", targetCore,
                static_cast<unsigned long long>(pkt->getAddr()),
                pkt->req->getSize(), pkt->cmd.toString(), retrying,
                static_cast<unsigned long long>(queue.size()));

        if (memSidePorts[targetCore]->sendTimingReq(pkt)) {
            ++stats.readPackets;
            stats.readBytes += pkt->req->getSize();
            retryPkt = nullptr;
            retryReady[targetCore] = false;
            nextSendCore = (targetCore + 1) % memSidePorts.size();
            sendableWork = sendableWork || !queue.empty();
        } else {
            retryPkt = pkt;
            retryReady[targetCore] = false;
        }
    }

    for (PortID core = 0; core < memSidePorts.size(); ++core) {
        sendableWork = sendableWork || !sendQueues[core].empty() ||
            (retryPkts[core] && retryReady[core]);
    }
    if (sendableWork)
        scheduleSend(curTick() + 1);
    if (hasCsrWalks())
        scheduleCsrWalk(curTick() + 1);
}

bool
CIRA::recvTimingResp(PortID targetCore, PacketPtr pkt)
{
    auto *senderState = dynamic_cast<PacketSenderState *>(pkt->senderState);
    panic_if(!senderState || senderState->role != PacketRole::PrefetchLine,
             "CIRA response without prefetch-line sender state");
    panic_if(senderState->targetCore != targetCore,
             "CIRA response returned on core %d for core %d", targetCore,
             senderState->targetCore);

    DPRINTF(CIRA, "response id=%#llx addr=%#llx size=%u error=%d\n",
            static_cast<unsigned long long>(senderState->id),
            static_cast<unsigned long long>(pkt->getAddr()),
            pkt->req->getSize(), pkt->isError());

    const uint64_t id = senderState->id;
    const auto it = outstanding.find(id);
    if (it != outstanding.end()) {
        RequestState &state = *it->second;
        panic_if(state.targetCore != targetCore,
                 "CIRA request/response target ownership mismatch");
        panic_if(state.pendingPackets == 0,
                 "CIRA response underflow for request %#llx",
                 static_cast<unsigned long long>(id));
        state.pendingPackets--;

        if (state.pendingPackets == 0) {
            auto *event = new EventFunctionWrapper(
                [this, id] { completeRequest(id); },
                csprintf("%s.complete_%llu", name(),
                         static_cast<unsigned long long>(id)),
                true);
            schedule(event, curTick() + completionLatency);
        }
    }

    pkt->senderState = nullptr;
    delete senderState;
    delete pkt;
    return true;
}

void
CIRA::recvReqRetry(PortID targetCore)
{
    panic_if(targetCore < 0 || targetCore >= retryPkts.size(),
             "CIRA retry for invalid core %d", targetCore);
    panic_if(!retryPkts[targetCore],
             "CIRA retry for core %d without a blocked packet", targetCore);
    retryReady[targetCore] = true;
    DPRINTF(CIRA, "recvReqRetry core=%d queued=%llu\n", targetCore,
            static_cast<unsigned long long>(sendQueues[targetCore].size()));
    scheduleSend(curTick());
}

bool
CIRA::isCpuDataDemand(const PacketPtr pkt) const
{
    if (!pkt || !pkt->req || !pkt->req->hasPaddr())
        return false;

    const bool demandTask =
        pkt->req->taskId() != context_switch_task_id::Prefetcher;
    if (pkt->req->requestorId() == requestorId ||
        !pkt->isDemand() || pkt->req->isInstFetch() ||
        pkt->req->isCacheMaintenance() || pkt->req->isPrefetch() ||
        pkt->cmd.isSWPrefetch() || pkt->cmd.isHWPrefetch() ||
        pkt->isEviction() || pkt->isWriteback() ||
        !demandTask) {
        return false;
    }

    const RequestorID id = pkt->req->requestorId();
    if (id >= system->maxRequestors())
        return false;

    const std::string requestorName = system->getRequestorName(id);
    static constexpr char CpuDataSuffix[] = ".data";
    static constexpr size_t CpuDataSuffixLength =
        sizeof(CpuDataSuffix) - 1;
    return requestorName.size() >= CpuDataSuffixLength &&
           requestorName.compare(
               requestorName.size() - CpuDataSuffixLength,
               CpuDataSuffixLength, CpuDataSuffix) == 0;
}

void
CIRA::handleCacheProbe(PortID targetCore, CacheProbeEvent event,
                       const CacheAccessProbeArg &arg)
{
    PacketPtr pkt = arg.pkt;
    if (!pkt || !pkt->req || !pkt->req->hasPaddr())
        return;

    const bool ciraOrigin = pkt->req->requestorId() == requestorId;
    auto &lineTracker = lineTrackers.at(targetCore);

    if (event == CacheProbeEvent::Fill) {
        lineTracker.fill(pkt->getAddr(), ciraOrigin);
        return;
    }

    if (ciraOrigin) {
        if (event == CacheProbeEvent::Hit)
            lineTracker.prefetchHit(pkt->getAddr());
        return;
    }

    if (!isCpuDataDemand(pkt))
        return;

    const auto attribution = lineTracker.demand(
        pkt->getAddr(), event == CacheProbeEvent::Hit);
    if (attribution ==
        CiraLineUsefulnessTracker::DemandAttribution::Useful) {
        ++stats.usefulPrefetches;
        ++stats.usefulPrefetchesPerCore[targetCore];
    } else if (attribution ==
               CiraLineUsefulnessTracker::DemandAttribution::Late) {
        ++stats.latePrefetches;
        ++stats.latePrefetchesPerCore[targetCore];
    }
}

void
CIRA::completeRequest(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    finished[state.tc].push_back(id);
    stats.totalLatency += curTick() - state.issueTick;
    ++stats.completedPrefetches;
    ++stats.completedPrefetchesPerCore[state.targetCore];

    DPRINTF(CIRA, "complete id=%#llx latency=%llu\n",
            static_cast<unsigned long long>(id),
            static_cast<unsigned long long>(curTick() - state.issueTick));

    outstanding.erase(it);
    if (hasCsrWalks())
        scheduleCsrWalk(curTick() + 1);
}

uint64_t
CIRA::getFinished(ThreadContext *tc)
{
    auto &queue = finished[tc];
    if (queue.empty())
        return 0;

    const uint64_t id = queue.front();
    queue.pop_front();
    return id;
}

uint64_t
CIRA::cfgWrite(ThreadContext *tc, uint64_t reg, uint64_t value)
{
    switch (reg) {
      case 0:
        enabled = value != 0;
        return 1;
      case 1:
        maxOutstanding = value;
        return 1;
      case 2:
        reset();
        return 1;
      default:
        return 0;
    }
}

uint64_t
CIRA::cfgRead(ThreadContext *tc, uint64_t reg) const
{
    switch (reg) {
      case 0:
        return enabled ? 1 : 0;
      case 1:
        return maxOutstanding;
      case 2:
        return outstanding.size() + queuedCsrWalks() +
            pendingCsrIndexReads.size();
      case 3: {
        const auto it = finished.find(tc);
        return it == finished.end() ? 0 : it->second.size();
      }
      default:
        return 0;
    }
}

void
CIRA::deleteQueuedPacket(PacketPtr pkt)
{
    if (!pkt)
        return;

    delete pkt->senderState;
    pkt->senderState = nullptr;
    delete pkt;
}

void
CIRA::reset()
{
    if (sendEvent.scheduled())
        deschedule(sendEvent);
    if (csrWalkEvent.scheduled())
        deschedule(csrWalkEvent);
    if (csrIndexSendEvent.scheduled())
        deschedule(csrIndexSendEvent);

    for (auto &queue : sendQueues) {
        while (!queue.empty()) {
            deleteQueuedPacket(queue.front());
            queue.pop_front();
        }
    }

    for (auto &retryPkt : retryPkts) {
        deleteQueuedPacket(retryPkt);
        retryPkt = nullptr;
    }
    std::fill(retryReady.begin(), retryReady.end(), false);
    for (auto &queue : csrWalkQueues)
        queue.clear();
    while (!csrIndexSendQueue.empty()) {
        deleteQueuedPacket(csrIndexSendQueue.front());
        csrIndexSendQueue.pop_front();
    }
    deleteQueuedPacket(csrIndexRetryPkt);
    csrIndexRetryPkt = nullptr;
    csrIndexRetryReady = false;
    pendingCsrIndexReads.clear();
    outstanding.clear();
    finished.clear();
    for (auto &tracker : lineTrackers)
        tracker.clear();
    nextSendCore = 0;
    nextCsrCore = 0;
    nextId = 1;
    nextCsrWalkId = 1;
    nextCsrIndexReadId = 1;
}

} // namespace gem5
