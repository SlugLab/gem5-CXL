/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/cira.hh"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <limits>
#include <unordered_set>
#include <utility>
#include <vector>

#include "arch/generic/mmu.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/thread_context.hh"
#include "debug/CIRA.hh"
#include "mem/cache/base.hh"
#include "mem/pr_row_math.hh"
#include "mem/se_translating_port_proxy.hh"
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
      ADD_STAT(issuedPrDescriptors, statistics::units::Count::get(),
               "CIRA PageRank descriptors accepted"),
      ADD_STAT(completedPrDescriptors, statistics::units::Count::get(),
               "CIRA PageRank descriptors completed"),
      ADD_STAT(rejectedPrDescriptors, statistics::units::Count::get(),
               "CIRA PageRank descriptors rejected before admission"),
      ADD_STAT(prRows, statistics::units::Count::get(),
               "CIRA PageRank rows completed"),
      ADD_STAT(prCsrReads, statistics::units::Count::get(),
               "CIRA PageRank device-side CSR packets sent"),
      ADD_STAT(prCoherentReads, statistics::units::Count::get(),
               "CIRA PageRank coherent read packets sent"),
      ADD_STAT(prCoherentWrites, statistics::units::Count::get(),
               "CIRA PageRank coherent write packets sent"),
      ADD_STAT(prComputeTicks, statistics::units::Tick::get(),
               "CIRA PageRank modeled compute ticks"),
      ADD_STAT(prQueueStallTicks, statistics::units::Tick::get(),
               "CIRA PageRank descriptor queue stall ticks"),
      ADD_STAT(issuedPrReconfigurations, statistics::units::Count::get(),
               "CIRA PageRank JIT reconfigurations issued"),
      ADD_STAT(completedPrReconfigurations, statistics::units::Count::get(),
               "CIRA PageRank JIT reconfigurations completed"),
      ADD_STAT(usefulHoists, statistics::units::Count::get(),
               "CIRA PageRank rows executed by accepted hoists"),
      ADD_STAT(ineffectiveHoists, statistics::units::Count::get(),
               "CIRA PageRank descriptors rejected as ineffective"),
      ADD_STAT(prOutstandingWork, statistics::units::Count::get(),
               "Current CIRA PageRank descriptors and reconfigurations"),
      ADD_STAT(prHighWatermark, statistics::units::Count::get(),
               "Maximum CIRA PageRank outstanding work"),
      ADD_STAT(issuedPrDescriptorsPerCore, statistics::units::Count::get(),
               "CIRA PageRank descriptors accepted per core"),
      ADD_STAT(completedPrDescriptorsPerCore,
               statistics::units::Count::get(),
               "CIRA PageRank descriptors completed per core"),
      ADD_STAT(prRowsPerCore, statistics::units::Count::get(),
               "CIRA PageRank rows completed per core"),
      ADD_STAT(prCsrReadsPerCore, statistics::units::Count::get(),
               "CIRA PageRank CSR packets per core"),
      ADD_STAT(prCoherentReadsPerCore, statistics::units::Count::get(),
               "CIRA PageRank coherent reads per core"),
      ADD_STAT(prCoherentWritesPerCore, statistics::units::Count::get(),
               "CIRA PageRank coherent writes per core"),
      ADD_STAT(prComputeTicksPerCore, statistics::units::Tick::get(),
               "CIRA PageRank compute ticks per core"),
      ADD_STAT(prQueueStallTicksPerCore, statistics::units::Tick::get(),
               "CIRA PageRank queue stall ticks per core"),
      ADD_STAT(issuedPrReconfigurationsPerCore,
               statistics::units::Count::get(),
               "CIRA PageRank reconfigurations issued per core"),
      ADD_STAT(completedPrReconfigurationsPerCore,
               statistics::units::Count::get(),
               "CIRA PageRank reconfigurations completed per core"),
      ADD_STAT(usefulHoistsPerCore, statistics::units::Count::get(),
               "CIRA PageRank useful hoisted rows per core"),
      ADD_STAT(ineffectiveHoistsPerCore, statistics::units::Count::get(),
               "CIRA PageRank ineffective hoists per core"),
      ADD_STAT(prOutstandingWorkPerCore, statistics::units::Count::get(),
               "Current CIRA PageRank work per core"),
      ADD_STAT(prHighWatermarkPerCore, statistics::units::Count::get(),
               "Maximum CIRA PageRank work per core"),
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
    issuedPrDescriptorsPerCore.init(num_cores);
    completedPrDescriptorsPerCore.init(num_cores);
    prRowsPerCore.init(num_cores);
    prCsrReadsPerCore.init(num_cores);
    prCoherentReadsPerCore.init(num_cores);
    prCoherentWritesPerCore.init(num_cores);
    prComputeTicksPerCore.init(num_cores);
    prQueueStallTicksPerCore.init(num_cores);
    issuedPrReconfigurationsPerCore.init(num_cores);
    completedPrReconfigurationsPerCore.init(num_cores);
    usefulHoistsPerCore.init(num_cores);
    ineffectiveHoistsPerCore.init(num_cores);
    prOutstandingWorkPerCore.init(num_cores);
    prHighWatermarkPerCore.init(num_cores);
    for (size_t core = 0; core < num_cores; ++core) {
        const std::string label = std::to_string(core);
        issuedCsrPrefetchesPerCore.subname(core, label);
        issuedPrefetchesPerCore.subname(core, label);
        completedPrefetchesPerCore.subname(core, label);
        coalescedPrefetchesPerCore.subname(core, label);
        usefulPrefetchesPerCore.subname(core, label);
        latePrefetchesPerCore.subname(core, label);
        issuedPrDescriptorsPerCore.subname(core, label);
        completedPrDescriptorsPerCore.subname(core, label);
        prRowsPerCore.subname(core, label);
        prCsrReadsPerCore.subname(core, label);
        prCoherentReadsPerCore.subname(core, label);
        prCoherentWritesPerCore.subname(core, label);
        prComputeTicksPerCore.subname(core, label);
        prQueueStallTicksPerCore.subname(core, label);
        issuedPrReconfigurationsPerCore.subname(core, label);
        completedPrReconfigurationsPerCore.subname(core, label);
        usefulHoistsPerCore.subname(core, label);
        ineffectiveHoistsPerCore.subname(core, label);
        prOutstandingWorkPerCore.subname(core, label);
        prHighWatermarkPerCore.subname(core, label);
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
      prDescriptorEntries(p.pr_descriptor_entries),
      prCsrReadEntries(p.pr_csr_read_entries),
      prCoherentEntries(p.pr_coherent_entries),
      prFpAddCycles(p.pr_fp_add_cycles),
      prFpMulCycles(p.pr_fp_mul_cycles),
      prFpDivCycles(p.pr_fp_div_cycles),
      prReconfigurationLatency(p.pr_reconfiguration_latency),
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
    panic_if(prDescriptorEntries == 0 || prCsrReadEntries == 0 ||
             prCoherentEntries == 0,
             "CIRA %s requires nonzero PageRank queue limits", name());
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
    prDescriptors.resize(numCores);
    reservedPrCoherentSlots.resize(numCores, 0);
    pendingPrCoherentPackets.resize(numCores, 0);
    prEvents.reserve(numCores);
    for (PortID core = 0; core < numCores; ++core) {
        prEvents.emplace_back(std::make_unique<EventFunctionWrapper>(
            [this, core] { processPr(core); },
            csprintf("%s.pr_service_%d", name(), core)));
    }
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
CIRA::translatePr(ThreadContext *tc, Addr vaddr, uint64_t size,
                  BaseMMU::Mode mode, bool csr,
                  std::vector<TranslationChunk> &chunks) const
{
    Addr offset = 0;
    auto gen = tc->getMMUPtr()->translateFunctional(
        vaddr, size, tc, mode, Request::Flags());
    for (const auto &range : *gen) {
        if (range.fault)
            return false;
        Request::Flags flags = range.flags;
        if (csr)
            flags.set(Request::UNCACHEABLE);
        chunks.push_back({range.paddr, offset, range.size, flags});
        offset += range.size;
    }
    return offset == size;
}

uint32_t
CIRA::countPackets(const std::vector<TranslationChunk> &chunks) const
{
    uint32_t count = 0;
    for (const auto &chunk : chunks) {
        Addr offset = 0;
        while (offset < chunk.size) {
            const uint64_t lineRemaining = cacheLineSize -
                ((chunk.paddr + offset) % cacheLineSize);
            offset += std::min<uint64_t>(chunk.size - offset, lineRemaining);
            ++count;
        }
    }
    return count;
}

bool
CIRA::readGuest(ThreadContext *tc, Addr addr, void *data,
                uint64_t size) const
{
    SETranslatingPortProxy proxy(tc);
    return proxy.tryReadBlob(addr, data, size);
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
        (retryPkts[targetCore] ? 1 : 0) +
        reservedPrCoherentSlots[targetCore];
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
    if (pendingCsrIndexReads.size() + pendingPrCsrPackets +
            reservedPrCsrSlots >= maxCsrIndexReads) {
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
        auto *senderState = dynamic_cast<PacketSenderState *>(
            pkt->senderState);
        panic_if(!senderState, "CIRA CSR packet has no sender state");
        if (senderState->prPacket) {
            panic_if(senderState->prRole != PrPacketRole::CsrRead,
                     "CIRA PR packet used an invalid CSR route");
            ++stats.prCsrReads;
            ++stats.prCsrReadsPerCore[senderState->targetCore];
        } else {
            ++stats.csrIndexReadPackets;
            stats.csrIndexReadBytes += pkt->req->getSize();
        }
    }
}

bool
CIRA::recvCsrTimingResp(PacketPtr pkt)
{
    auto *senderState = dynamic_cast<PacketSenderState *>(pkt->senderState);
    if (senderState && senderState->prPacket)
        return recvPrCsrTimingResp(pkt, senderState);
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

bool
CIRA::validatePrDescriptor(
    ThreadContext *tc, const pr_row_offload_desc &desc) const
{
    if ((desc.phase != PR_ROW_CONTRIB && desc.phase != PR_ROW_PULL) ||
        desc.row_count == 0 || desc.node_count == 0 ||
        desc.row_count == std::numeric_limits<uint64_t>::max() ||
        desc.row_begin > desc.node_count ||
        desc.row_count > desc.node_count - desc.row_begin ||
        desc.in_offsets_addr == 0 || desc.in_neighbors_addr == 0 ||
        desc.out_degree_addr == 0 || desc.scores_in_addr == 0 ||
        desc.contributions_addr == 0 || desc.scores_out_addr == 0) {
        return false;
    }

    auto translated = [this, tc](Addr base, uint64_t first,
                                  uint64_t count, uint64_t width,
                                  BaseMMU::Mode mode, bool csr) {
        if (first > std::numeric_limits<uint64_t>::max() / width ||
            count > std::numeric_limits<uint64_t>::max() / width)
            return false;
        const uint64_t offset = first * width;
        const uint64_t size = count * width;
        if (base > std::numeric_limits<Addr>::max() - offset)
            return false;
        const Addr start = base + offset;
        if (size > std::numeric_limits<Addr>::max() - start)
            return false;
        std::vector<TranslationChunk> chunks;
        return size != 0 && translatePr(tc, start, size, mode, csr, chunks);
    };

    if (desc.phase == PR_ROW_CONTRIB) {
        return translated(desc.scores_in_addr, desc.row_begin,
                          desc.row_count, sizeof(float), BaseMMU::Read,
                          false) &&
               translated(desc.out_degree_addr, desc.row_begin,
                          desc.row_count, sizeof(int64_t), BaseMMU::Read,
                          true) &&
               translated(desc.contributions_addr, desc.row_begin,
                          desc.row_count, sizeof(float), BaseMMU::Write,
                          false);
    }

    return translated(desc.in_offsets_addr, desc.row_begin,
                      desc.row_count + 1, sizeof(uint64_t), BaseMMU::Read,
                      true) &&
           translated(desc.in_neighbors_addr, 0, 1, sizeof(int32_t),
                      BaseMMU::Read, true) &&
           translated(desc.contributions_addr, 0, desc.node_count,
                      sizeof(float), BaseMMU::Read, false) &&
           translated(desc.scores_out_addr, desc.row_begin,
                      desc.row_count, sizeof(float), BaseMMU::Write, false);
}

uint64_t
CIRA::issuePrRows(ThreadContext *tc, Addr desc_addr)
{
    if (!enabled || !tc || desc_addr == 0) {
        ++stats.rejectedPrDescriptors;
        return 0;
    }
    const ContextID context = tc->contextId();
    if (context < 0 || context >= memSidePorts.size()) {
        ++stats.rejectedPrDescriptors;
        return 0;
    }
    const PortID targetCore = static_cast<PortID>(context);
    if (prDescriptors[targetCore].size() >= prDescriptorEntries) {
        ++stats.rejectedPrDescriptors;
        ++stats.ineffectiveHoists;
        ++stats.ineffectiveHoistsPerCore[targetCore];
        return 0;
    }

    pr_row_offload_desc desc = {};
    if (!readGuest(tc, desc_addr, &desc, sizeof(desc)) ||
        !validatePrDescriptor(tc, desc)) {
        ++stats.translationFaults;
        ++stats.rejectedPrDescriptors;
        return 0;
    }

    auto packetCount = [this, tc](Addr addr, uint64_t size, bool csr) {
        std::vector<TranslationChunk> chunks;
        if (!translatePr(tc, addr, size, BaseMMU::Read, csr, chunks))
            return uint32_t{0};
        return countPackets(chunks);
    };

    uint32_t initialCsrPackets = 0;
    uint32_t initialCoherentPackets = 0;
    if (desc.phase == PR_ROW_CONTRIB) {
        initialCoherentPackets = packetCount(
            desc.scores_in_addr + desc.row_begin * sizeof(float),
            sizeof(float), false);
        initialCsrPackets = packetCount(
            desc.out_degree_addr + desc.row_begin * sizeof(int64_t),
            sizeof(int64_t), true);
    } else {
        initialCsrPackets = packetCount(
            desc.in_offsets_addr + desc.row_begin * sizeof(uint64_t),
            2 * sizeof(uint64_t), true);
    }
    if (initialCsrPackets == 0 ||
        (desc.phase == PR_ROW_CONTRIB && initialCoherentPackets == 0)) {
        ++stats.translationFaults;
        ++stats.rejectedPrDescriptors;
        return 0;
    }

    const uint64_t csrOccupied = pendingCsrIndexReads.size() +
        pendingPrCsrPackets + reservedPrCsrSlots;
    const uint64_t coherentQueued = sendQueues[targetCore].size() +
        (retryPkts[targetCore] ? 1 : 0) +
        reservedPrCoherentSlots[targetCore];
    if (csrOccupied + initialCsrPackets > maxCsrIndexReads ||
        pendingPrCsrPackets + reservedPrCsrSlots + initialCsrPackets >
            prCsrReadEntries ||
        coherentQueued + initialCoherentPackets > maxSendQueue ||
        pendingPrCoherentPackets[targetCore] +
                reservedPrCoherentSlots[targetCore] +
                initialCoherentPackets > prCoherentEntries) {
        ++stats.rejectedPrDescriptors;
        ++stats.ineffectiveHoists;
        ++stats.ineffectiveHoistsPerCore[targetCore];
        return 0;
    }

    const uint64_t id = nextId++;
    auto state = std::make_unique<PrDescriptorState>();
    state->id = id;
    state->targetCore = targetCore;
    state->tc = tc;
    state->desc = desc;
    state->row = desc.row_begin;
    state->issueTick = curTick();
    state->reservedInitialCsrPackets = initialCsrPackets;
    state->reservedInitialCoherentPackets = initialCoherentPackets;
    prOutstanding.emplace(id, std::move(state));
    prDescriptors[targetCore].push_back(id);
    reservedPrCsrSlots += initialCsrPackets;
    reservedPrCoherentSlots[targetCore] += initialCoherentPackets;

    ++stats.issuedPrDescriptors;
    ++stats.issuedPrDescriptorsPerCore[targetCore];
    ++stats.prOutstandingWork;
    ++stats.prOutstandingWorkPerCore[targetCore];
    stats.prHighWatermark = std::max(
        static_cast<uint64_t>(stats.prOutstandingWork.value()),
        static_cast<uint64_t>(stats.prHighWatermark.value()));
    stats.prHighWatermarkPerCore[targetCore] = std::max(
        static_cast<uint64_t>(
            stats.prOutstandingWorkPerCore[targetCore].value()),
        static_cast<uint64_t>(
            stats.prHighWatermarkPerCore[targetCore].value()));
    schedulePr(targetCore, curTick() + issueLatency);
    return id;
}

void
CIRA::notePrStall(PrDescriptorState &state)
{
    if (!state.queueStalled) {
        state.queueStalled = true;
        state.stallStart = curTick();
    }
}

void
CIRA::clearPrStall(PrDescriptorState &state)
{
    if (!state.queueStalled)
        return;
    const Tick stalled = curTick() - state.stallStart;
    stats.prQueueStallTicks += stalled;
    stats.prQueueStallTicksPerCore[state.targetCore] += stalled;
    state.queueStalled = false;
}

bool
CIRA::reservePrRead(PrDescriptorState &state, Addr addr, uint64_t size,
                    PrPacketRole route, PrPayloadRole payload,
                    uint64_t index)
{
    const bool csr = route == PrPacketRole::CsrRead;
    panic_if(route == PrPacketRole::CoherentWrite,
             "CIRA PR read requested a write route");
    std::vector<TranslationChunk> chunks;
    if (!translatePr(state.tc, addr, size, BaseMMU::Read, csr, chunks))
        panic("CIRA accepted PR descriptor encountered a read fault");
    const uint32_t packets = countPackets(chunks);

    if (csr && state.reservedInitialCsrPackets != 0) {
        panic_if(packets > state.reservedInitialCsrPackets ||
                 packets > reservedPrCsrSlots,
                 "CIRA PR CSR reservation underflow");
        state.reservedInitialCsrPackets -= packets;
        reservedPrCsrSlots -= packets;
    } else if (!csr && state.reservedInitialCoherentPackets != 0) {
        panic_if(packets > state.reservedInitialCoherentPackets ||
                 packets > reservedPrCoherentSlots[state.targetCore],
                 "CIRA PR coherent reservation underflow");
        state.reservedInitialCoherentPackets -= packets;
        reservedPrCoherentSlots[state.targetCore] -= packets;
    } else if (csr) {
        const uint64_t occupied = pendingCsrIndexReads.size() +
            pendingPrCsrPackets + reservedPrCsrSlots;
        if (occupied + packets > maxCsrIndexReads ||
            pendingPrCsrPackets + reservedPrCsrSlots + packets >
                prCsrReadEntries) {
            notePrStall(state);
            return false;
        }
    } else {
        const uint64_t queued = sendQueues[state.targetCore].size() +
            (retryPkts[state.targetCore] ? 1 : 0) +
            reservedPrCoherentSlots[state.targetCore];
        if (queued + packets > maxSendQueue ||
            pendingPrCoherentPackets[state.targetCore] +
                    reservedPrCoherentSlots[state.targetCore] + packets >
                prCoherentEntries) {
            notePrStall(state);
            return false;
        }
    }
    clearPrStall(state);

    for (const auto &chunk : chunks) {
        Addr chunkOffset = 0;
        while (chunkOffset < chunk.size) {
            const uint64_t lineRemaining = cacheLineSize -
                ((chunk.paddr + chunkOffset) % cacheLineSize);
            const auto packetSize = static_cast<unsigned>(std::min<uint64_t>(
                chunk.size - chunkOffset, lineRemaining));
            RequestPtr req = std::make_shared<Request>(
                chunk.paddr + chunkOffset, packetSize, chunk.flags,
                requestorId);
            req->taskId(context_switch_task_id::DMA);
            PacketPtr pkt = new Packet(req, MemCmd::ReadReq);
            pkt->allocate();
            pkt->senderState = new PacketSenderState(
                route, payload, state.id, state.targetCore, index,
                chunk.offset + chunkOffset);
            ++state.pendingPackets;
            if (csr) {
                ++pendingPrCsrPackets;
                enqueueCsrIndexPacket(pkt);
            } else {
                ++pendingPrCoherentPackets[state.targetCore];
                enqueuePacket(state.targetCore, pkt);
            }
            chunkOffset += packetSize;
        }
    }
    if (csr)
        scheduleCsrIndexSend(curTick());
    else
        scheduleSend(curTick());
    return true;
}

bool
CIRA::reservePrWrite(PrDescriptorState &state, Addr addr,
                     const void *data, uint64_t size)
{
    std::vector<TranslationChunk> chunks;
    if (!translatePr(state.tc, addr, size, BaseMMU::Write, false, chunks))
        panic("CIRA accepted PR descriptor encountered a write fault");
    const uint32_t packets = countPackets(chunks);
    const uint64_t queued = sendQueues[state.targetCore].size() +
        (retryPkts[state.targetCore] ? 1 : 0) +
        reservedPrCoherentSlots[state.targetCore];
    if (queued + packets > maxSendQueue ||
        pendingPrCoherentPackets[state.targetCore] +
                reservedPrCoherentSlots[state.targetCore] + packets >
            prCoherentEntries) {
        notePrStall(state);
        return false;
    }
    clearPrStall(state);

    const auto *bytes = static_cast<const uint8_t *>(data);
    for (const auto &chunk : chunks) {
        Addr chunkOffset = 0;
        while (chunkOffset < chunk.size) {
            const uint64_t lineRemaining = cacheLineSize -
                ((chunk.paddr + chunkOffset) % cacheLineSize);
            const auto packetSize = static_cast<unsigned>(std::min<uint64_t>(
                chunk.size - chunkOffset, lineRemaining));
            RequestPtr req = std::make_shared<Request>(
                chunk.paddr + chunkOffset, packetSize, chunk.flags,
                requestorId);
            req->taskId(context_switch_task_id::DMA);
            PacketPtr pkt = new Packet(req, MemCmd::WriteReq);
            pkt->allocate();
            std::memcpy(pkt->getPtr<uint8_t>(),
                        bytes + chunk.offset + chunkOffset, packetSize);
            pkt->senderState = new PacketSenderState(
                PrPacketRole::CoherentWrite, PrPayloadRole::Result,
                state.id, state.targetCore, 0,
                chunk.offset + chunkOffset);
            ++state.pendingPackets;
            ++pendingPrCoherentPackets[state.targetCore];
            enqueuePacket(state.targetCore, pkt);
            chunkOffset += packetSize;
        }
    }
    scheduleSend(curTick());
    return true;
}

void
CIRA::schedulePr(PortID targetCore, Tick when)
{
    if (prDescriptors[targetCore].empty())
        return;
    auto &event = *prEvents[targetCore];
    if (event.scheduled()) {
        if (when < event.when())
            reschedule(event, when);
        return;
    }
    schedule(event, when);
}

void
CIRA::processPr(PortID targetCore)
{
    auto &queue = prDescriptors[targetCore];
    const size_t descriptors = queue.size();
    bool retryWithoutResponse = false;
    for (size_t index = 0; index < descriptors; ++index) {
        const uint64_t id = queue.front();
        queue.pop_front();
        const auto it = prOutstanding.find(id);
        if (it == prOutstanding.end())
            continue;
        retryWithoutResponse |= processPrDescriptor(*it->second);
        if (prOutstanding.count(id))
            queue.push_back(id);
    }
    if (retryWithoutResponse)
        schedulePr(targetCore, clockEdge(Cycles(1)));
}

bool
CIRA::processPrDescriptor(PrDescriptorState &state)
{
    while (true) {
        switch (state.stage) {
          case PrStage::StartRow:
            panic_if(state.pendingPackets != 0,
                     "CIRA PR row started with pending packets");
            state.nextRead = 0;
            state.neighbors.clear();
            state.contributions.clear();
            state.scoreData.fill(0);
            state.degreeData.fill(0);
            state.offsetsData.fill(0);
            state.stage = state.desc.phase == PR_ROW_CONTRIB ?
                PrStage::ContribRead : PrStage::PullOffsets;
            continue;

          case PrStage::ContribRead:
            while (state.nextRead < 2) {
                const bool score = state.nextRead == 0;
                const Addr base = score ? state.desc.scores_in_addr :
                                          state.desc.out_degree_addr;
                const uint64_t width = score ? sizeof(float) :
                                               sizeof(int64_t);
                if (!reservePrRead(
                        state, base + state.row * width, width,
                        score ? PrPacketRole::CoherentRead :
                                PrPacketRole::CsrRead,
                        score ? PrPayloadRole::Score :
                                PrPayloadRole::Degree,
                        0)) {
                    return state.pendingPackets == 0;
                }
                ++state.nextRead;
            }
            if (state.pendingPackets != 0)
                return false;
            state.stage = PrStage::ContribCompute;
            schedulePrCompute(state.id, prFpDivCycles);
            return false;

          case PrStage::PullOffsets:
            if (state.nextRead == 0) {
                const Addr addr = state.desc.in_offsets_addr +
                    state.row * sizeof(uint64_t);
                if (!reservePrRead(
                        state, addr, state.offsetsData.size(),
                        PrPacketRole::CsrRead, PrPayloadRole::Offsets, 0)) {
                    return state.pendingPackets == 0;
                }
                state.nextRead = 1;
            }
            if (state.pendingPackets != 0)
                return false;
            std::memcpy(&state.edgeBegin, state.offsetsData.data(),
                        sizeof(state.edgeBegin));
            std::memcpy(&state.edgeEnd,
                        state.offsetsData.data() + sizeof(state.edgeBegin),
                        sizeof(state.edgeEnd));
            panic_if(state.edgeEnd < state.edgeBegin ||
                     state.edgeEnd - state.edgeBegin >
                        std::numeric_limits<uint32_t>::max(),
                     "CIRA PR row has an invalid CSR edge interval");
            state.neighbors.assign(state.edgeEnd - state.edgeBegin, 0);
            state.contributions.assign(state.edgeEnd - state.edgeBegin,
                                       0.0f);
            state.nextRead = 0;
            state.stage = PrStage::PullNeighbors;
            continue;

          case PrStage::PullNeighbors:
            while (state.nextRead < state.neighbors.size()) {
                const uint64_t edge = state.edgeBegin + state.nextRead;
                panic_if(edge > std::numeric_limits<Addr>::max() /
                                    sizeof(int32_t) ||
                         state.desc.in_neighbors_addr >
                            std::numeric_limits<Addr>::max() -
                                edge * sizeof(int32_t),
                         "CIRA PR neighbor address overflow");
                if (!reservePrRead(
                        state,
                        state.desc.in_neighbors_addr + edge * sizeof(int32_t),
                        sizeof(int32_t), PrPacketRole::CsrRead,
                        PrPayloadRole::Neighbor, state.nextRead)) {
                    return state.pendingPackets == 0;
                }
                ++state.nextRead;
            }
            if (state.pendingPackets != 0)
                return false;
            for (int32_t neighbor : state.neighbors) {
                panic_if(neighbor < 0 ||
                         static_cast<uint64_t>(neighbor) >=
                            state.desc.node_count,
                         "CIRA PR neighbor %d is outside node count %llu",
                         neighbor, static_cast<unsigned long long>(
                             state.desc.node_count));
            }
            state.nextRead = 0;
            state.stage = PrStage::PullContributions;
            continue;

          case PrStage::PullContributions:
            while (state.nextRead < state.contributions.size()) {
                const uint64_t neighbor = static_cast<uint64_t>(
                    state.neighbors[state.nextRead]);
                if (!reservePrRead(
                        state,
                        state.desc.contributions_addr +
                            neighbor * sizeof(float),
                        sizeof(float), PrPacketRole::CoherentRead,
                        PrPayloadRole::Contribution, state.nextRead)) {
                    return state.pendingPackets == 0;
                }
                ++state.nextRead;
            }
            if (state.pendingPackets != 0)
                return false;
            state.stage = PrStage::PullCompute;
            schedulePrCompute(
                state.id,
                Cycles(static_cast<uint64_t>(prFpAddCycles) *
                           state.contributions.size() +
                       static_cast<uint64_t>(prFpMulCycles) +
                       static_cast<uint64_t>(prFpAddCycles)));
            return false;

          case PrStage::ContribCompute:
          case PrStage::PullCompute:
            return false;

          case PrStage::WriteResult:
            if (state.nextRead == 0) {
                const Addr base = state.desc.phase == PR_ROW_CONTRIB ?
                    state.desc.contributions_addr : state.desc.scores_out_addr;
                if (!reservePrWrite(
                        state, base + state.row * sizeof(float),
                        &state.result, sizeof(state.result))) {
                    return state.pendingPackets == 0;
                }
                state.nextRead = 1;
            }
            if (state.pendingPackets != 0)
                return false;
            const uint64_t id = state.id;
            advancePrRow(state);
            return prOutstanding.count(id) != 0;
        }
    }
}

void
CIRA::schedulePrCompute(uint64_t id, Cycles cycles)
{
    const auto it = prOutstanding.find(id);
    if (it == prOutstanding.end())
        return;
    const PortID core = it->second->targetCore;
    const Tick ticks = cyclesToTicks(cycles);
    stats.prComputeTicks += ticks;
    stats.prComputeTicksPerCore[core] += ticks;
    const uint64_t epoch = prEpoch;
    auto *event = new EventFunctionWrapper(
        [this, id, epoch] {
            if (epoch == prEpoch)
                finishPrCompute(id);
        },
        csprintf("%s.pr_compute_%llu", name(),
                 static_cast<unsigned long long>(id)), true);
    schedule(event, curTick() + ticks);
}

void
CIRA::finishPrCompute(uint64_t id)
{
    const auto it = prOutstanding.find(id);
    if (it == prOutstanding.end())
        return;
    PrDescriptorState &state = *it->second;
    if (state.stage == PrStage::ContribCompute) {
        float score = 0.0f;
        int64_t degree = 0;
        std::memcpy(&score, state.scoreData.data(), sizeof(score));
        std::memcpy(&degree, state.degreeData.data(), sizeof(degree));
        state.result = prF32Div(score, static_cast<float>(degree));
    } else {
        panic_if(state.stage != PrStage::PullCompute,
                 "CIRA PR compute event observed an invalid stage");
        float sum = 0.0f;
        for (float contribution : state.contributions)
            sum = prF32Add(sum, contribution);
        float damping = 0.0f;
        float base = 0.0f;
        std::memcpy(&damping, &state.desc.damping_bits, sizeof(damping));
        std::memcpy(&base, &state.desc.base_score_bits, sizeof(base));
        state.result = prF32Add(base, prF32Mul(damping, sum));
    }
    state.stage = PrStage::WriteResult;
    state.nextRead = 0;
    schedulePr(state.targetCore, curTick());
}

void
CIRA::advancePrRow(PrDescriptorState &state)
{
    ++stats.prRows;
    ++stats.prRowsPerCore[state.targetCore];
    ++stats.usefulHoists;
    ++stats.usefulHoistsPerCore[state.targetCore];
    ++state.row;
    if (state.row == state.desc.row_begin + state.desc.row_count) {
        completePrDescriptor(state.id);
        return;
    }
    state.stage = PrStage::StartRow;
    state.nextRead = 0;
}

void
CIRA::completePrDescriptor(uint64_t id)
{
    const auto it = prOutstanding.find(id);
    if (it == prOutstanding.end())
        return;
    PrDescriptorState &state = *it->second;
    panic_if(state.pendingPackets != 0 ||
             state.reservedInitialCsrPackets != 0 ||
             state.reservedInitialCoherentPackets != 0,
             "CIRA completed PR descriptor with outstanding credits");
    clearPrStall(state);
    const PortID core = state.targetCore;
    finished[state.tc].push_back(id);
    ++stats.completedPrDescriptors;
    ++stats.completedPrDescriptorsPerCore[core];
    --stats.prOutstandingWork;
    --stats.prOutstandingWorkPerCore[core];
    prOutstanding.erase(it);
}

void
CIRA::completePrReconfiguration(uint64_t id)
{
    const auto it = prReconfigurations.find(id);
    if (it == prReconfigurations.end())
        return;
    ThreadContext *tc = it->second;
    const PortID core = resolveTargetCore(tc);
    finished[tc].push_back(id);
    ++stats.completedPrReconfigurations;
    ++stats.completedPrReconfigurationsPerCore[core];
    --stats.prOutstandingWork;
    --stats.prOutstandingWorkPerCore[core];
    prReconfigurations.erase(it);
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
        (retryPkts[targetCore] ? 1 : 0) +
        reservedPrCoherentSlots[targetCore];
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
            auto *senderState = dynamic_cast<PacketSenderState *>(
                pkt->senderState);
            panic_if(!senderState, "CIRA coherent packet has no sender state");
            if (senderState->prPacket) {
                if (senderState->prRole == PrPacketRole::CoherentRead) {
                    ++stats.prCoherentReads;
                    ++stats.prCoherentReadsPerCore[targetCore];
                } else {
                    panic_if(senderState->prRole !=
                                PrPacketRole::CoherentWrite,
                             "CIRA PR packet used an invalid coherent route");
                    ++stats.prCoherentWrites;
                    ++stats.prCoherentWritesPerCore[targetCore];
                }
            } else {
                ++stats.readPackets;
                stats.readBytes += pkt->req->getSize();
            }
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
    if (senderState && senderState->prPacket)
        return recvPrTimingResp(targetCore, pkt, senderState);
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

bool
CIRA::recvPrTimingResp(PortID targetCore, PacketPtr pkt,
                       PacketSenderState *senderState)
{
    panic_if(senderState->targetCore != targetCore ||
             (senderState->prRole != PrPacketRole::CoherentRead &&
              senderState->prRole != PrPacketRole::CoherentWrite),
             "CIRA PR coherent response has invalid ownership");
    const auto it = prOutstanding.find(senderState->id);
    panic_if(it == prOutstanding.end(),
             "CIRA PR coherent response has no descriptor");
    PrDescriptorState &state = *it->second;
    panic_if(pkt->isError() || state.pendingPackets == 0 ||
             pendingPrCoherentPackets[targetCore] == 0,
             "CIRA PR coherent response failed its packet contract");

    if (senderState->prRole == PrPacketRole::CoherentRead) {
        uint8_t *target = nullptr;
        uint64_t targetSize = 0;
        switch (senderState->prPayload) {
          case PrPayloadRole::Score:
            target = state.scoreData.data();
            targetSize = state.scoreData.size();
            break;
          case PrPayloadRole::Contribution:
            panic_if(senderState->prIndex >= state.contributions.size(),
                     "CIRA PR contribution response index is invalid");
            target = reinterpret_cast<uint8_t *>(
                &state.contributions[senderState->prIndex]);
            targetSize = sizeof(float);
            break;
          default:
            panic("CIRA PR coherent read has an invalid payload role");
        }
        panic_if(senderState->dataOffset + pkt->getSize() > targetSize,
                 "CIRA PR coherent response exceeds destination");
        std::memcpy(target + senderState->dataOffset,
                    pkt->getConstPtr<uint8_t>(), pkt->getSize());
    } else {
        panic_if(senderState->prPayload != PrPayloadRole::Result,
                 "CIRA PR coherent write has an invalid payload role");
    }
    --pendingPrCoherentPackets[targetCore];
    --state.pendingPackets;
    pkt->senderState = nullptr;
    delete senderState;
    delete pkt;
    schedulePr(targetCore, curTick());
    return true;
}

bool
CIRA::recvPrCsrTimingResp(PacketPtr pkt,
                          PacketSenderState *senderState)
{
    panic_if(senderState->prRole != PrPacketRole::CsrRead,
             "CIRA PR CSR response has an invalid route");
    const auto it = prOutstanding.find(senderState->id);
    panic_if(it == prOutstanding.end(),
             "CIRA PR CSR response has no descriptor");
    PrDescriptorState &state = *it->second;
    panic_if(pkt->isError() || state.pendingPackets == 0 ||
             pendingPrCsrPackets == 0 ||
             state.targetCore != senderState->targetCore,
             "CIRA PR CSR response failed its packet contract");

    uint8_t *target = nullptr;
    uint64_t targetSize = 0;
    switch (senderState->prPayload) {
      case PrPayloadRole::Degree:
        target = state.degreeData.data();
        targetSize = state.degreeData.size();
        break;
      case PrPayloadRole::Offsets:
        target = state.offsetsData.data();
        targetSize = state.offsetsData.size();
        break;
      case PrPayloadRole::Neighbor:
        panic_if(senderState->prIndex >= state.neighbors.size(),
                 "CIRA PR neighbor response index is invalid");
        target = reinterpret_cast<uint8_t *>(
            &state.neighbors[senderState->prIndex]);
        targetSize = sizeof(int32_t);
        break;
      default:
        panic("CIRA PR CSR read has an invalid payload role");
    }
    panic_if(senderState->dataOffset + pkt->getSize() > targetSize,
             "CIRA PR CSR response exceeds destination");
    std::memcpy(target + senderState->dataOffset,
                pkt->getConstPtr<uint8_t>(), pkt->getSize());
    --pendingPrCsrPackets;
    --state.pendingPackets;
    const PortID core = state.targetCore;
    pkt->senderState = nullptr;
    delete senderState;
    delete pkt;
    schedulePr(core, curTick());
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
      case 4:
        if (value == 0)
            return 0;
        prThreadConfigs[tc].rowWindow = value;
        return 1;
      case 5:
        prThreadConfigs[tc].leadBlocks = value;
        return 1;
      case 6: {
        const PortID core = resolveTargetCore(tc);
        if (prReconfigurations.size() >= maxOutstanding) {
            ++stats.rejectedPrDescriptors;
            return 0;
        }
        const uint64_t id = nextId++;
        prReconfigurations.emplace(id, tc);
        ++stats.issuedPrReconfigurations;
        ++stats.issuedPrReconfigurationsPerCore[core];
        ++stats.prOutstandingWork;
        ++stats.prOutstandingWorkPerCore[core];
        stats.prHighWatermark = std::max(
            static_cast<uint64_t>(stats.prOutstandingWork.value()),
            static_cast<uint64_t>(stats.prHighWatermark.value()));
        stats.prHighWatermarkPerCore[core] = std::max(
            static_cast<uint64_t>(
                stats.prOutstandingWorkPerCore[core].value()),
            static_cast<uint64_t>(
                stats.prHighWatermarkPerCore[core].value()));
        const uint64_t epoch = prEpoch;
        auto *event = new EventFunctionWrapper(
            [this, id, epoch] {
                if (epoch == prEpoch)
                    completePrReconfiguration(id);
            },
            csprintf("%s.pr_reconfigure_%llu", name(),
                     static_cast<unsigned long long>(id)), true);
        schedule(event, curTick() + prReconfigurationLatency);
        return id;
      }
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
            pendingCsrIndexReads.size() + prOutstanding.size() +
            prReconfigurations.size();
      case 3: {
        const auto it = finished.find(tc);
        return it == finished.end() ? 0 : it->second.size();
      }
      case 4: {
        const auto it = prThreadConfigs.find(tc);
        return it == prThreadConfigs.end() ? 1 : it->second.rowWindow;
      }
      case 5: {
        const auto it = prThreadConfigs.find(tc);
        return it == prThreadConfigs.end() ? 0 : it->second.leadBlocks;
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
    ++prEpoch;
    if (sendEvent.scheduled())
        deschedule(sendEvent);
    if (csrWalkEvent.scheduled())
        deschedule(csrWalkEvent);
    if (csrIndexSendEvent.scheduled())
        deschedule(csrIndexSendEvent);
    for (auto &event : prEvents) {
        if (event->scheduled())
            deschedule(*event);
    }

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
    prOutstanding.clear();
    for (auto &queue : prDescriptors)
        queue.clear();
    std::fill(reservedPrCoherentSlots.begin(),
              reservedPrCoherentSlots.end(), 0);
    std::fill(pendingPrCoherentPackets.begin(),
              pendingPrCoherentPackets.end(), 0);
    reservedPrCsrSlots = 0;
    pendingPrCsrPackets = 0;
    prThreadConfigs.clear();
    prReconfigurations.clear();
    stats.prOutstandingWork = 0;
    for (PortID core = 0; core < memSidePorts.size(); ++core)
        stats.prOutstandingWorkPerCore[core] = 0;
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
