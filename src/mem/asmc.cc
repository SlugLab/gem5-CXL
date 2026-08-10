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

ASMC::ASMCStats::ASMCStats(ASMC &owner)
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
{}

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
      sendEvent([this] { trySend(); }, name()),
      stats(*this)
{
    panic_if(pendingQueueEntries == 0,
             "ASMC pending_queue_entries must be non-zero");
    panic_if(idBatchEntries == 0,
             "ASMC id_batch_entries must be non-zero");
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
ASMC::readSpm(Addr addr, void *data, uint64_t size) const
{
    auto *bytes = static_cast<uint8_t *>(data);
    for (uint64_t offset = 0; offset < size; ++offset) {
        const auto it = spmData.find(addr + offset);
        if (it == spmData.end())
            return false;
        bytes[offset] = it->second;
    }
    return true;
}

void
ASMC::writeSpm(Addr addr, const void *data, uint64_t size)
{
    const auto *bytes = static_cast<const uint8_t *>(data);
    for (uint64_t offset = 0; offset < size; ++offset)
        spmData[addr + offset] = bytes[offset];
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
ASMC::enqueuePackets(RequestState &state,
                     const std::vector<TranslationChunk> &chunks,
                     MemCmd command, RequestPhase phase)
{
    const bool is_read = command == MemCmd::ReadReq;
    for (const auto &chunk : chunks) {
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
            req->taskId(context_switch_task_id::DMA);

            PacketPtr pkt = new Packet(req, command);
            pkt->dataStatic(state.data.data() + chunk.offset + chunk_offset);
            pkt->senderState = new PacketSenderState(
                state.id, phase, is_read);
            state.pendingPackets++;
            enqueuePacket(pkt);
            chunk_offset += pkt_size;
        }
    }
}

uint64_t
ASMC::issue(ThreadContext *tc, ReqType type, Addr spm_addr, Addr mem_addr)
{
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

    std::vector<TranslationChunk> chunks;
    const auto mode = type == ReqType::Load ? BaseMMU::Read : BaseMMU::Write;
    if (!translate(tc, mem_addr, granularity, mode, chunks)) {
        ++stats.translationFaults;
        return 0;
    }

    std::vector<TranslationChunk> spm_chunks;
    if (type == ReqType::Load &&
        !translate(tc, spm_addr, granularity, BaseMMU::Write, spm_chunks)) {
        ++stats.translationFaults;
        return 0;
    }

    const uint32_t memory_packets = countPackets(chunks);
    const uint32_t spm_packets = countPackets(spm_chunks);
    const uint64_t occupied = sendQueue.size() + (retryPkt ? 1 : 0) +
                              reservedSendSlots;
    if (occupied + memory_packets + spm_packets > maxSendQueue) {
        ++stats.rejectedQueueFull;
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
    state->memoryChunks = std::move(chunks);
    state->spmChunks = std::move(spm_chunks);
    state->reservedMemoryPackets = memory_packets;
    state->reservedWritePackets = spm_packets;

    if (type == ReqType::Store) {
        if (!readSpm(spm_addr, state->data.data(), state->size) &&
            !readGuest(tc, spm_addr, state->data.data(), state->size)) {
            ++stats.translationFaults;
            return 0;
        }
    }

    updateOccupancyIntegral();
    spmUsed += state->size;
    outstanding[id] = std::move(state);
    stats.maxObservedOutstanding = std::max(
        outstanding.size(),
        static_cast<size_t>(stats.maxObservedOutstanding.value()));
    reservedSendSlots += memory_packets + spm_packets;
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
        [this, id] { startMemoryAccess(id); },
        csprintf("%s.metadata_%llu", name(),
                 static_cast<unsigned long long>(id)),
        true);
    schedule(event, curTick() + service_delay);
    return id;
}

void
ASMC::startMemoryAccess(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    panic_if(metadataPending == 0 ||
             reservedSendSlots < state.reservedMemoryPackets,
             "ASMC invalid metadata service state for request %#llx",
             static_cast<unsigned long long>(id));
    metadataPending--;
    ++stats.metadataAccesses;
    reservedSendSlots -= state.reservedMemoryPackets;
    state.reservedMemoryPackets = 0;
    const MemCmd command = state.type == ReqType::Load ?
        MemCmd::ReadReq : MemCmd::WriteReq;
    enqueuePackets(state, state.memoryChunks, command,
                   RequestPhase::MemoryAccess);
    scheduleSend(curTick());
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
ASMC::startSpmWriteback(RequestState &state)
{
    panic_if(state.type != ReqType::Load ||
             state.phase != RequestPhase::MemoryAccess,
             "ASMC invalid SPM writeback transition for request %#llx",
             static_cast<unsigned long long>(state.id));
    panic_if(state.pendingPackets != 0,
             "ASMC SPM writeback started with %u memory packets pending",
             state.pendingPackets);
    panic_if(state.reservedWritePackets != countPackets(state.spmChunks) ||
             reservedSendSlots < state.reservedWritePackets,
             "ASMC invalid SPM writeback reservation for request %#llx",
             static_cast<unsigned long long>(state.id));

    state.phase = RequestPhase::SpmWriteback;
    reservedSendSlots -= state.reservedWritePackets;
    state.reservedWritePackets = 0;
    enqueuePackets(state, state.spmChunks, MemCmd::WriteReq,
                   RequestPhase::SpmWriteback);
    scheduleSend(curTick());
}

void
ASMC::enqueuePacket(PacketPtr pkt)
{
    panic_if(sendQueue.size() >= maxSendQueue,
             "ASMC send queue overflow after admission check");
    sendQueue.push_back(pkt);
}

void
ASMC::scheduleSend(Tick when)
{
    if (retryPkt)
        return;

    if (sendEvent.scheduled()) {
        if (when < sendEvent.when())
            reschedule(sendEvent, when);
        return;
    }
    schedule(sendEvent, when);
}

void
ASMC::trySend()
{
    while (retryPkt || !sendQueue.empty()) {
        PacketPtr pkt = retryPkt;
        if (!pkt) {
            pkt = sendQueue.front();
            sendQueue.pop_front();
        }

        if (memSidePort.sendTimingReq(pkt)) {
            auto *state = dynamic_cast<PacketSenderState *>(pkt->senderState);
            if (state && state->read) {
                ++stats.readPackets;
                stats.readBytes += pkt->req->getSize();
            } else {
                ++stats.writePackets;
                stats.writeBytes += pkt->req->getSize();
            }
            retryPkt = nullptr;
        } else {
            retryPkt = pkt;
            break;
        }
    }
}

bool
ASMC::recvTimingResp(PacketPtr pkt)
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
        panic_if(state.phase != sender_state->phase,
                 "ASMC stale phase response for request %#llx",
                 static_cast<unsigned long long>(id));
        panic_if(state.pendingPackets == 0,
                 "ASMC response underflow for request %#llx",
                 static_cast<unsigned long long>(id));
        state.pendingPackets--;

        if (state.size <= sizeof(uint64_t)) {
            uint64_t payload = 0;
            std::memcpy(&payload, state.data.data(), state.size);
            DPRINTF(ASMC,
                    "payload id=%#llx phase=%s value=%#llx pending=%u\n",
                    static_cast<unsigned long long>(id),
                    sender_state->phase == RequestPhase::MemoryAccess ?
                        "memory" : "spm-writeback",
                    static_cast<unsigned long long>(payload),
                    state.pendingPackets);
        }

        if (state.pendingPackets == 0 &&
            state.type == ReqType::Load &&
            state.phase == RequestPhase::MemoryAccess) {
            startCompletionService(id);
        } else if (state.pendingPackets == 0) {
            if (state.phase == RequestPhase::SpmWriteback) {
                auto *event = new EventFunctionWrapper(
                    [this, id] { completeRequest(id); },
                    csprintf("%s.complete_%llu", name(),
                             static_cast<unsigned long long>(id)),
                    true);
                schedule(event, curTick() + completionLatency +
                                configuredLatency +
                                cyclesToTicks(completionPublishLatency));
            } else {
                startCompletionService(id);
            }
        }
    }

    pkt->senderState = nullptr;
    delete sender_state;
    delete pkt;
    return true;
}

void
ASMC::recvReqRetry()
{
    panic_if(!retryPkt, "ASMC received a spurious request retry");
    if (sendEvent.scheduled())
        reschedule(sendEvent, curTick());
    else
        schedule(sendEvent, curTick());
}

void
ASMC::completeRequest(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    panic_if(state.pendingPackets != 0 ||
             state.reservedMemoryPackets != 0 ||
             state.reservedWritePackets != 0,
             "ASMC completed request %#llx with pending packet state",
             static_cast<unsigned long long>(id));
    panic_if(state.type == ReqType::Load &&
             state.phase != RequestPhase::SpmWriteback,
             "ASMC load request %#llx completed before SPM writeback",
             static_cast<unsigned long long>(id));
    if (state.type == ReqType::Load) {
        writeSpm(state.spmAddr, state.data.data(), state.size);
    }

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
    reservedSendSlots = 0;
    metadataPending = 0;
    completionPending = 0;
    idsRemaining = 0;
    completionWaitQueue.clear();
    pollWaitStart.clear();
    lastOccupancyTick = curTick();
    nextId = 1;
}

} // namespace gem5
