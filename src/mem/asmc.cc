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
               totalLatency / (completedLoads + completedStores))
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
{}

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

uint64_t
ASMC::issue(ThreadContext *tc, ReqType type, Addr spm_addr, Addr mem_addr)
{
    if (outstanding.size() >= maxOutstanding ||
        sendQueue.size() >= maxSendQueue) {
        ++stats.rejectedQueueFull;
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

    uint64_t packets_needed = retryPkt ? 1 : 0;
    packets_needed += sendQueue.size();
    for (const auto &chunk : chunks) {
        Addr chunk_offset = 0;
        while (chunk_offset < chunk.size) {
            const uint64_t line_remaining =
                cacheLineSize - ((chunk.paddr + chunk_offset) % cacheLineSize);
            const uint64_t pkt_size = std::min(
                chunk.size - chunk_offset, line_remaining);
            ++packets_needed;
            chunk_offset += pkt_size;
        }
    }

    if (packets_needed > maxSendQueue) {
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

    if (type == ReqType::Store) {
        if (!readSpm(spm_addr, state->data.data(), state->size) &&
            !readGuest(tc, spm_addr, state->data.data(), state->size)) {
            ++stats.translationFaults;
            return 0;
        }
    }

    spmUsed += state->size;
    RequestState *raw_state = state.get();
    outstanding[id] = std::move(state);

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

            const MemCmd cmd = type == ReqType::Load ?
                MemCmd::ReadReq : MemCmd::WriteReq;
            PacketPtr pkt = new Packet(req, cmd);
            pkt->dataStatic(raw_state->data.data() + chunk.offset +
                            chunk_offset);
            pkt->senderState = new PacketSenderState(
                id, type == ReqType::Load);
            raw_state->pendingPackets++;
            enqueuePacket(pkt);
            chunk_offset += pkt_size;
        }
    }

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
            chunks.size());

    scheduleSend(curTick() + issueLatency);
    return id;
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
        panic_if(state.pendingPackets == 0,
                 "ASMC response underflow for request %#llx",
                 static_cast<unsigned long long>(id));
        state.pendingPackets--;

        if (state.pendingPackets == 0) {
            auto *event = new EventFunctionWrapper(
                [this, id] { completeRequest(id); },
                csprintf("%s.complete_%llu", name(),
                         static_cast<unsigned long long>(id)),
                true);
            schedule(event, curTick() + completionLatency + configuredLatency);
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
    scheduleSend(curTick());
}

void
ASMC::completeRequest(uint64_t id)
{
    const auto it = outstanding.find(id);
    if (it == outstanding.end())
        return;

    RequestState &state = *it->second;
    if (state.type == ReqType::Load) {
        writeSpm(state.spmAddr, state.data.data(), state.size);
        writeGuest(state.tc, state.spmAddr, state.data.data(), state.size);
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
