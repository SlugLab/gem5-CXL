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
#include "sim/system.hh"

namespace gem5
{

std::unordered_map<System *, ASMC *> ASMC::registry;

ASMC::MemoryPort::MemoryPort(const std::string &name, ASMC &owner,
                             PortID target_core)
    : RequestPort(name), owner(owner), targetCore(target_core)
{}

bool
ASMC::MemoryPort::recvTimingResp(PacketPtr pkt)
{
    return owner.recvTimingResp(targetCore, pkt);
}

void
ASMC::MemoryPort::recvReqRetry()
{
    owner.recvReqRetry(targetCore);
}

ASMC::ASMCStats::ASMCStats(ASMC &owner, size_t num_cores)
    : statistics::Group(&owner), owner(owner),
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
      ADD_STAT(farReadPackets, statistics::units::Count::get(),
               "Far-memory timing read packets sent by ASMC"),
      ADD_STAT(farWritePackets, statistics::units::Count::get(),
               "Far-memory timing write packets sent by ASMC"),
      ADD_STAT(farRetries, statistics::units::Count::get(),
               "Far-memory timing packets rejected and retried"),
      ADD_STAT(spmReadPackets, statistics::units::Count::get(),
               "Coherent SPM timing read packets sent per core"),
      ADD_STAT(spmWritePackets, statistics::units::Count::get(),
               "Coherent SPM timing write packets sent per core"),
      ADD_STAT(spmRetries, statistics::units::Count::get(),
               "Coherent SPM timing packets rejected per core"),
      ADD_STAT(totalLatency, statistics::units::Tick::get(),
               "Total AMU request latency from m5op issue to finish queue"),
      ADD_STAT(outstandingIntegral, statistics::units::Count::get(),
               "Integral of outstanding AMU requests over ticks"),
      ADD_STAT(occupancyTicks, statistics::units::Tick::get(),
               "Ticks represented by the outstanding-request integral"),
      ADD_STAT(maxObservedOutstanding, statistics::units::Count::get(),
               "Maximum simultaneously outstanding AMU requests"),
      ADD_STAT(pendingQueueFull, statistics::units::Count::get(),
               "Internal metadata/completion service backpressure events"),
      ADD_STAT(idBatchRefills, statistics::units::Count::get(),
               "AMART ID batch refills"),
      ADD_STAT(metadataAccesses, statistics::units::Count::get(),
               "Metadata services performed at issue and completion"),
      ADD_STAT(emptyGetfinPolls, statistics::units::Count::get(),
               "getfin calls that found no completed request"),
      ADD_STAT(successfulGetfin, statistics::units::Count::get(),
               "getfin calls that returned a completed request"),
      ADD_STAT(consumerWaitTicks, statistics::units::Tick::get(),
               "Ticks from first empty getfin poll to a successful poll"),
      ADD_STAT(avgLatency, statistics::units::Tick::get(),
               "Average AMU request latency",
               totalLatency / (completedLoads + completedStores)),
      ADD_STAT(avgOutstanding, statistics::units::Count::get(),
               "Time-weighted average outstanding AMU requests",
               outstandingIntegral / occupancyTicks)
{
    spmReadPackets.init(num_cores);
    spmWritePackets.init(num_cores);
    spmRetries.init(num_cores);
}

void
ASMC::ASMCStats::preDumpStats()
{
    statistics::Group::preDumpStats();
    owner.updateOccupancyIntegral();
}

ASMC::ASMC(const Params &p)
    : ClockedObject(p),
      system(p.system),
      memSidePort(name() + ".mem_side_port", *this),
      requestorId(system->getRequestorId(this)),
      spmSize(p.spm_size),
      cacheLineSize(p.cache_line_size),
      maxSendQueue(p.max_send_queue),
      spmSendQueueSize(p.spm_send_queue_size),
      pendingQueueEntries(p.pending_queue_entries),
      idBatchEntries(p.id_batch_entries),
      metadataLatency(p.metadata_latency),
      idRefillLatency(p.id_refill_latency),
      completionPublishLatency(p.completion_publish_latency),
      issueLatency(p.issue_latency),
      completionLatency(p.completion_latency),
      granularity(p.default_granularity ? p.default_granularity : 1),
      maxOutstanding(p.max_outstanding),
      configuredLatency(p.asmc_latency),
      farSendEvent([this] { tryFarSend(); }, name() + ".far_send"),
      stats(*this, p.port_spm_side_ports_connection_count)
{
    const size_t num_cores = p.port_spm_side_ports_connection_count;
    panic_if(num_cores == 0,
             "ASMC %s requires at least one coherent SPM port", name());
    panic_if(spmSendQueueSize == 0,
             "ASMC %s requires a nonzero SPM send queue", name());
    panic_if(pendingQueueEntries == 0,
             "ASMC pending_queue_entries must be non-zero");
    panic_if(idBatchEntries == 0,
             "ASMC id_batch_entries must be non-zero");
    spmSidePorts.reserve(num_cores);
    spmSendEvents.reserve(num_cores);
    for (PortID core = 0; core < num_cores; ++core) {
        spmSidePorts.emplace_back(std::make_unique<MemoryPort>(
            csprintf("%s.spm_side_ports[%d]", name(), core), *this, core));
        spmSendEvents.emplace_back(std::make_unique<EventFunctionWrapper>(
            [this, core] { trySpmSend(core); },
            csprintf("%s.spm_send[%d]", name(), core)));
    }
    spmSendQueues.resize(num_cores);
    spmRetryPkts.resize(num_cores, nullptr);
    spmRetryReady.resize(num_cores, false);
    reservedSpmSendSlots.resize(num_cores, 0);
    lastOccupancyTick = curTick();
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
    for (PortID core = 0; core < spmSidePorts.size(); ++core) {
        panic_if(!spmSidePorts[core]->isConnected(),
                 "ASMC SPM port %d of %s is not connected", core, name());
    }
    panic_if(registry.count(system) && registry[system] != this,
             "Only one ASMC instance per System is currently supported");
    registry[system] = this;
    ClockedObject::init();
}

void
ASMC::resetStats()
{
    updateOccupancyIntegral();
    ClockedObject::resetStats();
    lastOccupancyTick = curTick();
    pollWaitStart.clear();
    stats.maxObservedOutstanding = outstanding.size();
}

Port &
ASMC::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "mem_side_port")
        return memSidePort;
    if (if_name == "spm_side_ports") {
        panic_if(idx == InvalidPortID || idx >= spmSidePorts.size(),
                 "ASMC %s invalid SPM port index %d", name(), idx);
        return *spmSidePorts[idx];
    }
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

uint32_t
ASMC::countPackets(const std::vector<TranslationChunk> &chunks) const
{
    uint32_t count = 0;
    for (const auto &chunk : chunks) {
        Addr offset = 0;
        while (offset < chunk.size) {
            const uint64_t line_remaining =
                cacheLineSize - ((chunk.paddr + offset) % cacheLineSize);
            offset += std::min<uint64_t>(chunk.size - offset,
                                         line_remaining);
            ++count;
        }
    }
    return count;
}

void
ASMC::enqueueFarPackets(RequestState &state, MemCmd command,
                        RequestPhase phase)
{
    const bool is_read = command == MemCmd::ReadReq;
    for (const auto &chunk : state.memoryChunks) {
        Addr chunk_offset = 0;
        while (chunk_offset < chunk.size) {
            const uint64_t remaining = chunk.size - chunk_offset;
            const uint64_t line_remaining =
                cacheLineSize - ((chunk.paddr + chunk_offset) % cacheLineSize);
            const auto pkt_size = static_cast<unsigned>(
                std::min(remaining, line_remaining));

            RequestPtr req = std::make_shared<Request>(
                chunk.paddr + chunk_offset, pkt_size, chunk.flags,
                requestorId);
            panic_if(req->isSpmAccess(),
                     "ASMC far-memory packet carried the SPM flag");
            req->taskId(context_switch_task_id::DMA);

            PacketPtr pkt = new Packet(req, command);
            if (is_read)
                pkt->allocate();
            else
                pkt->dataStatic(state.data.data() + chunk.offset + chunk_offset);
            pkt->senderState = new PacketSenderState(
                state.id, phase, InvalidPortID,
                chunk.offset + chunk_offset, pkt_size, is_read);
            state.pendingPackets++;
            enqueueFarPacket(pkt);
            chunk_offset += pkt_size;
        }
    }
}

void
ASMC::enqueueSpmPackets(RequestState &state, MemCmd command,
                        RequestPhase phase)
{
    const bool is_read = command == MemCmd::ReadReq;
    for (const auto &chunk : state.spmChunks) {
        Addr chunk_offset = 0;
        while (chunk_offset < chunk.size) {
            const uint64_t remaining = chunk.size - chunk_offset;
            const uint64_t line_remaining =
                cacheLineSize - ((chunk.paddr + chunk_offset) % cacheLineSize);
            const auto pkt_size = static_cast<unsigned>(
                std::min(remaining, line_remaining));

            Request::Flags flags = chunk.flags;
            flags.set(Request::SPM_ACCESS);
            RequestPtr req = std::make_shared<Request>(
                chunk.paddr + chunk_offset, pkt_size, flags, requestorId);
            panic_if(!req->isSpmAccess(),
                     "ASMC SPM packet lost the partition flag");
            req->taskId(context_switch_task_id::DMA);

            PacketPtr pkt = new Packet(req, command);
            if (is_read)
                pkt->allocate();
            else
                pkt->dataStatic(state.data.data() + chunk.offset + chunk_offset);
            pkt->senderState = new PacketSenderState(
                state.id, phase, state.targetCore,
                chunk.offset + chunk_offset, pkt_size, is_read);
            state.pendingPackets++;
            enqueueSpmPacket(state.targetCore, pkt);
            chunk_offset += pkt_size;
        }
    }
}

void
ASMC::enqueueSpmAcquirePackets(RequestState &state)
{
    for (const auto &chunk : state.spmChunks) {
        Addr chunk_offset = 0;
        while (chunk_offset < chunk.size) {
            const Addr address = chunk.paddr + chunk_offset;
            const Addr line_address = address & ~(cacheLineSize - 1);
            const unsigned fragment_offset = address - line_address;
            const auto fragment_size = static_cast<unsigned>(std::min<uint64_t>(
                chunk.size - chunk_offset,
                cacheLineSize - fragment_offset));

            Request::Flags flags = chunk.flags;
            flags.set(Request::SPM_ACCESS);
            RequestPtr req = std::make_shared<Request>(
                line_address, cacheLineSize, flags, requestorId);
            panic_if(!req->isSpmAccess(),
                     "ASMC SPM acquire lost the partition flag");
            req->taskId(context_switch_task_id::DMA);

            PacketPtr pkt = new Packet(req, MemCmd::ReadExReq);
            pkt->allocate();
            pkt->senderState = new PacketSenderState(
                state.id, RequestPhase::SpmAcquire, state.targetCore,
                chunk.offset + chunk_offset, cacheLineSize, true,
                fragment_offset, fragment_size);
            state.pendingPackets++;
            enqueueSpmPacket(state.targetCore, pkt);
            chunk_offset += fragment_size;
        }
    }
}

uint64_t
ASMC::issue(ThreadContext *tc, ReqType type, Addr spm_addr, Addr mem_addr)
{
    const auto context = tc->contextId();
    if (context < 0 || static_cast<size_t>(context) >= spmSidePorts.size()) {
        ++stats.rejectedQueueFull;
        return 0;
    }
    const unsigned target_core = static_cast<unsigned>(context);

    if (outstanding.size() >= maxOutstanding) {
        ++stats.rejectedQueueFull;
        return 0;
    }

    if (metadataPending >= pendingQueueEntries) {
        ++stats.pendingQueueFull;
        return 0;
    }

    if (spmUsed + granularity > spmSize) {
        ++stats.rejectedSpmFull;
        return 0;
    }

    std::vector<TranslationChunk> memory_chunks;
    const auto memory_mode = type == ReqType::Load ?
        BaseMMU::Read : BaseMMU::Write;
    if (!translate(tc, mem_addr, granularity, memory_mode, memory_chunks)) {
        ++stats.translationFaults;
        return 0;
    }

    std::vector<TranslationChunk> spm_chunks;
    const auto spm_mode = type == ReqType::Load ?
        BaseMMU::Write : BaseMMU::Read;
    if (!translate(tc, spm_addr, granularity, spm_mode, spm_chunks)) {
        ++stats.translationFaults;
        return 0;
    }

    const uint32_t memory_packets = countPackets(memory_chunks);
    const uint32_t spm_packets = countPackets(spm_chunks);
    const uint32_t reserved_spm_packets =
        type == ReqType::Load ? 2 * spm_packets : spm_packets;
    const uint64_t far_occupied = farSendQueue.size() +
        (farRetryPkt ? 1 : 0) + reservedFarSendSlots;
    const uint64_t spm_occupied = spmSendQueues[target_core].size() +
        (spmRetryPkts[target_core] ? 1 : 0) +
        reservedSpmSendSlots[target_core];
    if (far_occupied + memory_packets > maxSendQueue ||
        spm_occupied + reserved_spm_packets > spmSendQueueSize) {
        ++stats.rejectedQueueFull;
        return 0;
    }

    const uint64_t id = nextId++;
    auto state = std::make_unique<RequestState>();
    state->id = id;
    state->type = type;
    state->phase = type == ReqType::Load ?
        RequestPhase::MemoryAccess : RequestPhase::SpmRead;
    state->targetCore = target_core;
    state->tc = tc;
    state->spmAddr = spm_addr;
    state->memAddr = mem_addr;
    state->size = granularity;
    state->issueTick = curTick();
    state->data.resize(granularity);
    state->memoryChunks = std::move(memory_chunks);
    state->spmChunks = std::move(spm_chunks);
    state->reservedFarPackets = memory_packets;
    state->reservedSpmPackets = reserved_spm_packets;

    updateOccupancyIntegral();
    spmUsed += state->size;
    outstanding[id] = std::move(state);
    stats.maxObservedOutstanding = std::max(
        outstanding.size(),
        static_cast<size_t>(stats.maxObservedOutstanding.value()));
    reservedFarSendSlots += memory_packets;
    reservedSpmSendSlots[target_core] += reserved_spm_packets;
    metadataPending++;

    Tick service_delay = issueLatency + cyclesToTicks(metadataLatency);
    if (idsRemaining == 0) {
        idsRemaining = idBatchEntries;
        ++stats.idBatchRefills;
        service_delay += cyclesToTicks(idRefillLatency);
    }
    --idsRemaining;

    if (type == ReqType::Load)
        ++stats.issuedLoads;
    else
        ++stats.issuedStores;

    DPRINTF(ASMC,
            "issue id=%#llx type=%s spm=%#llx mem=%#llx size=%llu "
            "chunks=%zu\n",
            static_cast<unsigned long long>(id),
            type == ReqType::Load ? "aload" : "astore",
            static_cast<unsigned long long>(spm_addr),
            static_cast<unsigned long long>(mem_addr),
            static_cast<unsigned long long>(granularity),
            outstanding[id]->memoryChunks.size());

    auto *event = new EventFunctionWrapper(
        [this, id] { startInitialAccess(id); },
        csprintf("%s.metadata_%llu", name(),
                 static_cast<unsigned long long>(id)),
        true);
    schedule(event, curTick() + service_delay);
    return id;
}

void
ASMC::startInitialAccess(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    panic_if(metadataPending == 0,
             "ASMC invalid metadata service state for request %#llx",
             static_cast<unsigned long long>(id));
    metadataPending--;
    ++stats.metadataAccesses;
    if (state.type == ReqType::Load) {
        panic_if(state.phase != RequestPhase::MemoryAccess ||
                 reservedFarSendSlots < state.reservedFarPackets,
                 "ASMC invalid far-read reservation for request %#llx",
                 static_cast<unsigned long long>(id));
        reservedFarSendSlots -= state.reservedFarPackets;
        state.reservedFarPackets = 0;
        enqueueFarPackets(state, MemCmd::ReadReq,
                          RequestPhase::MemoryAccess);
        scheduleFarSend(curTick());
    } else {
        panic_if(state.phase != RequestPhase::SpmRead ||
                 state.targetCore >= reservedSpmSendSlots.size() ||
                 reservedSpmSendSlots[state.targetCore] <
                    state.reservedSpmPackets,
                 "ASMC invalid SPM-read reservation for request %#llx",
                 static_cast<unsigned long long>(id));
        reservedSpmSendSlots[state.targetCore] -= state.reservedSpmPackets;
        state.reservedSpmPackets = 0;
        enqueueSpmPackets(state, MemCmd::ReadReq, RequestPhase::SpmRead);
        scheduleSpmSend(state.targetCore, curTick());
    }
}

void
ASMC::startCompletionService(uint64_t id)
{
    if (completionPending >= pendingQueueEntries) {
        ++stats.pendingQueueFull;
        completionWaitQueue.push_back(id);
        return;
    }
    activateCompletionService(id);
}

void
ASMC::activateCompletionService(uint64_t id)
{
    completionPending++;
    auto *event = new EventFunctionWrapper(
        [this, id] { finishCompletionService(id); },
        csprintf("%s.completion_metadata_%llu", name(),
                 static_cast<unsigned long long>(id)),
        true);
    schedule(event, curTick() + cyclesToTicks(metadataLatency));
}

void
ASMC::finishCompletionService(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    panic_if(completionPending == 0,
             "ASMC completion service accounting underflow");
    completionPending--;
    ++stats.metadataAccesses;

    RequestState &state = *it->second;
    if (state.type == ReqType::Load &&
        state.phase == RequestPhase::MemoryAccess) {
        startSpmWriteback(state);
    } else if (state.type == ReqType::Store &&
               state.phase == RequestPhase::SpmRead) {
        startMemoryWrite(state);
    } else {
        auto *event = new EventFunctionWrapper(
            [this, id] { completeRequest(id); },
            csprintf("%s.complete_%llu", name(),
                     static_cast<unsigned long long>(id)),
            true);
        schedule(event, curTick() + completionLatency +
                        configuredLatency +
                        cyclesToTicks(completionPublishLatency));
    }

    if (!completionWaitQueue.empty()) {
        const uint64_t waiting_id = completionWaitQueue.front();
        completionWaitQueue.pop_front();
        activateCompletionService(waiting_id);
    }
}

void
ASMC::startMemoryWrite(RequestState &state)
{
    panic_if(state.type != ReqType::Store ||
             state.phase != RequestPhase::SpmRead,
             "ASMC invalid far-memory write transition for request %#llx",
             static_cast<unsigned long long>(state.id));
    panic_if(state.pendingPackets != 0,
             "ASMC far-memory write started with %u SPM packets pending",
             state.pendingPackets);
    panic_if(state.reservedFarPackets != countPackets(state.memoryChunks) ||
             reservedFarSendSlots < state.reservedFarPackets,
             "ASMC invalid far-memory write reservation for request %#llx",
             static_cast<unsigned long long>(state.id));

    state.phase = RequestPhase::MemoryAccess;
    reservedFarSendSlots -= state.reservedFarPackets;
    state.reservedFarPackets = 0;
    enqueueFarPackets(state, MemCmd::WriteReq,
                      RequestPhase::MemoryAccess);
    scheduleFarSend(curTick());
}

void
ASMC::startSpmWriteback(RequestState &state)
{
    panic_if(state.type != ReqType::Load ||
             state.phase != RequestPhase::MemoryAccess,
             "ASMC invalid SPM writeback transition for request %#llx",
             static_cast<unsigned long long>(state.id));
    panic_if(state.pendingPackets != 0,
             "ASMC SPM writeback started with %u memory packets pending",
             state.pendingPackets);
    const uint32_t acquire_packets = countPackets(state.spmChunks);
    panic_if(state.targetCore >= reservedSpmSendSlots.size() ||
             state.reservedSpmPackets != 2 * acquire_packets ||
             reservedSpmSendSlots[state.targetCore] <
                state.reservedSpmPackets,
             "ASMC invalid SPM writeback reservation for request %#llx",
             static_cast<unsigned long long>(state.id));

    state.phase = RequestPhase::SpmAcquire;
    reservedSpmSendSlots[state.targetCore] -= acquire_packets;
    state.reservedSpmPackets -= acquire_packets;
    enqueueSpmAcquirePackets(state);
    scheduleSpmSend(state.targetCore, curTick());
}

void
ASMC::startSpmLineWrites(RequestState &state)
{
    panic_if(state.type != ReqType::Load ||
             state.phase != RequestPhase::SpmAcquire ||
             state.pendingPackets != 0,
             "ASMC invalid SPM line-write transition for request %#llx",
             static_cast<unsigned long long>(state.id));
    panic_if(state.spmWritebacks.empty() ||
             state.reservedSpmPackets != state.spmWritebacks.size() ||
             reservedSpmSendSlots[state.targetCore] <
                state.reservedSpmPackets,
             "ASMC invalid SPM line-write reservation for request %#llx",
             static_cast<unsigned long long>(state.id));

    state.phase = RequestPhase::SpmWriteback;
    reservedSpmSendSlots[state.targetCore] -= state.reservedSpmPackets;
    state.reservedSpmPackets = 0;
    for (PacketPtr pkt : state.spmWritebacks) {
        state.pendingPackets++;
        enqueueSpmPacket(state.targetCore, pkt);
    }
    state.spmWritebacks.clear();
    scheduleSpmSend(state.targetCore, curTick());
}

void
ASMC::enqueueFarPacket(PacketPtr pkt)
{
    panic_if(farSendQueue.size() >= maxSendQueue,
             "ASMC far send queue overflow after admission check");
    farSendQueue.push_back(pkt);
}

void
ASMC::enqueueSpmPacket(PortID target_core, PacketPtr pkt)
{
    panic_if(target_core < 0 || target_core >= spmSendQueues.size(),
             "ASMC packet for invalid SPM core %d", target_core);
    auto &queue = spmSendQueues[target_core];
    panic_if(queue.size() >= spmSendQueueSize,
             "ASMC SPM send queue %d overflow after admission check",
             target_core);
    queue.push_back(pkt);
}

void
ASMC::scheduleFarSend(Tick when)
{
    if (farRetryPkt && !farRetryReady)
        return;

    if (farSendEvent.scheduled()) {
        if (when < farSendEvent.when())
            reschedule(farSendEvent, when);
        return;
    }
    schedule(farSendEvent, when);
}

void
ASMC::scheduleSpmSend(PortID target_core, Tick when)
{
    panic_if(target_core < 0 || target_core >= spmSendEvents.size(),
             "ASMC schedule for invalid SPM core %d", target_core);
    if (spmRetryPkts[target_core] && !spmRetryReady[target_core])
        return;

    EventFunctionWrapper &event = *spmSendEvents[target_core];
    if (event.scheduled()) {
        if (when < event.when())
            reschedule(event, when);
        return;
    }
    schedule(event, when);
}

void
ASMC::tryFarSend()
{
    while ((farRetryPkt && farRetryReady) ||
           (!farRetryPkt && !farSendQueue.empty())) {
        PacketPtr pkt = farRetryPkt;
        if (!pkt) {
            pkt = farSendQueue.front();
            farSendQueue.pop_front();
        }

        if (memSidePort.sendTimingReq(pkt)) {
            auto *sender_state =
                dynamic_cast<PacketSenderState *>(pkt->senderState);
            panic_if(!sender_state ||
                     sender_state->targetCore != InvalidPortID,
                     "ASMC far packet carried invalid sender state");
            if (sender_state->read)
                ++stats.farReadPackets;
            else
                ++stats.farWritePackets;
            farRetryPkt = nullptr;
            farRetryReady = false;
        } else {
            ++stats.farRetries;
            farRetryPkt = pkt;
            farRetryReady = false;
            break;
        }
    }
}

void
ASMC::trySpmSend(PortID target_core)
{
    panic_if(target_core < 0 || target_core >= spmSidePorts.size(),
             "ASMC send for invalid SPM core %d", target_core);
    auto &queue = spmSendQueues[target_core];
    PacketPtr &retry_pkt = spmRetryPkts[target_core];

    while ((retry_pkt && spmRetryReady[target_core]) ||
           (!retry_pkt && !queue.empty())) {
        PacketPtr pkt = retry_pkt;
        if (!pkt) {
            pkt = queue.front();
            queue.pop_front();
        }

        if (spmSidePorts[target_core]->sendTimingReq(pkt)) {
            auto *sender_state =
                dynamic_cast<PacketSenderState *>(pkt->senderState);
            panic_if(!sender_state || sender_state->targetCore != target_core,
                     "ASMC SPM packet/port ownership mismatch");
            if (sender_state->read)
                ++stats.spmReadPackets[target_core];
            else
                ++stats.spmWritePackets[target_core];
            retry_pkt = nullptr;
            spmRetryReady[target_core] = false;
        } else {
            ++stats.spmRetries[target_core];
            retry_pkt = pkt;
            spmRetryReady[target_core] = false;
            break;
        }
    }
}

bool
ASMC::recvTimingResp(PortID target_core, PacketPtr pkt)
{
    auto *sender_state = dynamic_cast<PacketSenderState *>(pkt->senderState);
    panic_if(!sender_state, "ASMC response without ASMC sender state");

    DPRINTF(ASMC, "response id=%#llx addr=%#llx size=%u error=%d\n",
            static_cast<unsigned long long>(sender_state->id),
            static_cast<unsigned long long>(pkt->getAddr()),
            pkt->req->getSize(), pkt->isError());

    const uint64_t id = sender_state->id;
    const auto it = outstanding.find(id);
    if (it != outstanding.end()) {
        RequestState &state = *it->second;
        panic_if(sender_state->targetCore != target_core,
                 "ASMC response returned on the wrong timing route");
        panic_if(target_core != InvalidPortID &&
                 static_cast<unsigned>(target_core) != state.targetCore,
                 "ASMC SPM response returned to the wrong core");
        panic_if(state.phase != sender_state->phase,
                 "ASMC stale phase response for request %#llx",
                 static_cast<unsigned long long>(id));
        panic_if(pkt->isError(),
                 "ASMC error response for request %#llx",
                 static_cast<unsigned long long>(id));
        panic_if(sender_state->size != pkt->getSize() ||
                 sender_state->fragmentOffset +
                    sender_state->fragmentSize > sender_state->size ||
                 sender_state->byteOffset +
                    sender_state->fragmentSize > state.data.size(),
                 "ASMC response payload bounds mismatch for request %#llx",
                 static_cast<unsigned long long>(id));
        panic_if(state.pendingPackets == 0,
                 "ASMC response underflow for request %#llx",
                 static_cast<unsigned long long>(id));

        if (state.phase == RequestPhase::SpmAcquire) {
            panic_if(!sender_state->read || target_core == InvalidPortID ||
                     sender_state->size != cacheLineSize,
                     "ASMC invalid SPM acquire response for request %#llx",
                     static_cast<unsigned long long>(id));

            PacketPtr writeback = new Packet(
                pkt->req, MemCmd::WriteLineReq);
            writeback->allocate();
            std::memcpy(writeback->getPtr<uint8_t>(),
                        pkt->getConstPtr<uint8_t>(), cacheLineSize);
            std::memcpy(
                writeback->getPtr<uint8_t>() + sender_state->fragmentOffset,
                state.data.data() + sender_state->byteOffset,
                sender_state->fragmentSize);
            writeback->senderState = new PacketSenderState(
                id, RequestPhase::SpmWriteback, target_core,
                sender_state->byteOffset, cacheLineSize, false,
                sender_state->fragmentOffset,
                sender_state->fragmentSize);
            state.spmWritebacks.push_back(writeback);
            state.pendingPackets--;
            const bool acquired_all = state.pendingPackets == 0;

            pkt->senderState = nullptr;
            delete sender_state;
            delete pkt;
            if (acquired_all)
                startSpmLineWrites(state);
            return true;
        }

        if (sender_state->read) {
            std::memcpy(state.data.data() + sender_state->byteOffset,
                        pkt->getConstPtr<uint8_t>() +
                            sender_state->fragmentOffset,
                        sender_state->fragmentSize);
        }
        state.pendingPackets--;

        if (state.size <= sizeof(uint64_t)) {
            uint64_t payload = 0;
            std::memcpy(&payload, state.data.data(), state.size);
            DPRINTF(ASMC,
                    "payload id=%#llx phase=%s value=%#llx pending=%u\n",
                    static_cast<unsigned long long>(id),
                    sender_state->phase == RequestPhase::SpmRead ?
                        "spm-read" :
                    sender_state->phase == RequestPhase::MemoryAccess ?
                        "memory" :
                    sender_state->phase == RequestPhase::SpmAcquire ?
                        "spm-acquire" : "spm-writeback",
                    static_cast<unsigned long long>(payload),
                    state.pendingPackets);
        }

        if (state.pendingPackets == 0) {
            const bool first_phase =
                (state.type == ReqType::Load &&
                 state.phase == RequestPhase::MemoryAccess) ||
                (state.type == ReqType::Store &&
                 state.phase == RequestPhase::SpmRead);
            if (first_phase) {
                startCompletionService(id);
            } else {
                panic_if((state.type == ReqType::Load &&
                          state.phase != RequestPhase::SpmWriteback) ||
                         (state.type == ReqType::Store &&
                          state.phase != RequestPhase::MemoryAccess),
                         "ASMC invalid final response phase for request %#llx",
                         static_cast<unsigned long long>(id));
                auto *event = new EventFunctionWrapper(
                    [this, id] { completeRequest(id); },
                    csprintf("%s.complete_%llu", name(),
                             static_cast<unsigned long long>(id)),
                    true);
                schedule(event, curTick() + completionLatency +
                                configuredLatency +
                                cyclesToTicks(completionPublishLatency));
            }
        }
    }

    pkt->senderState = nullptr;
    delete sender_state;
    delete pkt;
    return true;
}

void
ASMC::recvReqRetry(PortID target_core)
{
    if (target_core == InvalidPortID) {
        panic_if(!farRetryPkt,
                 "ASMC received a spurious far-memory request retry");
        farRetryReady = true;
        scheduleFarSend(curTick());
        return;
    }

    panic_if(target_core < 0 || target_core >= spmRetryPkts.size(),
             "ASMC retry for invalid SPM core %d", target_core);
    panic_if(!spmRetryPkts[target_core],
             "ASMC retry for core %d without a blocked packet", target_core);
    spmRetryReady[target_core] = true;
    scheduleSpmSend(target_core, curTick());
}

void
ASMC::completeRequest(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    panic_if(state.pendingPackets != 0 ||
             state.reservedFarPackets != 0 ||
             state.reservedSpmPackets != 0 ||
             !state.spmWritebacks.empty(),
             "ASMC completed request %#llx with pending packet state",
             static_cast<unsigned long long>(id));
    panic_if(state.type == ReqType::Load &&
             state.phase != RequestPhase::SpmWriteback,
             "ASMC load request %#llx completed before SPM writeback",
             static_cast<unsigned long long>(id));
    panic_if(state.type == ReqType::Store &&
             state.phase != RequestPhase::MemoryAccess,
             "ASMC store request %#llx completed before far-memory write",
             static_cast<unsigned long long>(id));

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

    updateOccupancyIntegral();
    outstanding.erase(it);
}

void
ASMC::updateOccupancyIntegral()
{
    const Tick now = curTick();
    panic_if(now < lastOccupancyTick, "ASMC occupancy time moved backwards");
    const Tick elapsed = now - lastOccupancyTick;
    stats.outstandingIntegral += elapsed * outstanding.size();
    stats.occupancyTicks += elapsed;
    lastOccupancyTick = now;
}

uint64_t
ASMC::getFinished(ThreadContext *tc)
{
    auto &queue = finished[tc];
    if (queue.empty()) {
        ++stats.emptyGetfinPolls;
        if (!outstanding.empty() && !pollWaitStart.count(tc))
            pollWaitStart[tc] = curTick();
        return 0;
    }

    const uint64_t id = queue.front();
    queue.pop_front();
    ++stats.successfulGetfin;
    const auto wait = pollWaitStart.find(tc);
    if (wait != pollWaitStart.end()) {
        stats.consumerWaitTicks += curTick() - wait->second;
        pollWaitStart.erase(wait);
    }
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
    updateOccupancyIntegral();
    while (!farSendQueue.empty()) {
        deleteQueuedPacket(farSendQueue.front());
        farSendQueue.pop_front();
    }

    deleteQueuedPacket(farRetryPkt);
    farRetryPkt = nullptr;
    farRetryReady = false;
    for (PortID core = 0; core < spmSendQueues.size(); ++core) {
        auto &queue = spmSendQueues[core];
        while (!queue.empty()) {
            deleteQueuedPacket(queue.front());
            queue.pop_front();
        }
        deleteQueuedPacket(spmRetryPkts[core]);
        spmRetryPkts[core] = nullptr;
        spmRetryReady[core] = false;
        reservedSpmSendSlots[core] = 0;
    }
    for (auto &entry : outstanding) {
        auto &state = entry.second;
        for (PacketPtr pkt : state->spmWritebacks)
            deleteQueuedPacket(pkt);
        state->spmWritebacks.clear();
    }
    outstanding.clear();
    finished.clear();
    spmUsed = 0;
    reservedFarSendSlots = 0;
    metadataPending = 0;
    completionPending = 0;
    idsRemaining = 0;
    completionWaitQueue.clear();
    pollWaitStart.clear();
    lastOccupancyTick = curTick();
    nextId = 1;
}

} // namespace gem5
