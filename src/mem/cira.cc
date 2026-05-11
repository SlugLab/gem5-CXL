/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/cira.hh"

#include <algorithm>
#include <cassert>
#include <utility>

#include "arch/generic/mmu.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/thread_context.hh"
#include "debug/CIRA.hh"
#include "sim/system.hh"

namespace gem5
{

std::unordered_map<System *, CIRA *> CIRA::registry;

CIRA::MemoryPort::MemoryPort(const std::string &name, CIRA &owner)
    : RequestPort(name), owner(owner)
{}

bool
CIRA::MemoryPort::recvTimingResp(PacketPtr pkt)
{
    return owner.recvTimingResp(pkt);
}

void
CIRA::MemoryPort::recvReqRetry()
{
    owner.recvReqRetry();
}

CIRA::CIRAStats::CIRAStats(statistics::Group *parent)
    : statistics::Group(parent),
      ADD_STAT(issuedPrefetches, statistics::units::Count::get(),
               "CIRA cacheline install/prefetch requests issued"),
      ADD_STAT(completedPrefetches, statistics::units::Count::get(),
               "CIRA cacheline install/prefetch requests completed"),
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
      ADD_STAT(totalLatency, statistics::units::Tick::get(),
               "Total CIRA request latency from m5op issue to completion"),
      ADD_STAT(avgLatency, statistics::units::Tick::get(),
               "Average CIRA request latency",
               totalLatency / completedPrefetches)
{}

CIRA::CIRA(const Params &p)
    : ClockedObject(p),
      system(p.system),
      memSidePort(name() + ".mem_side_port", *this),
      requestorId(system->getRequestorId(this)),
      cacheLineSize(p.cache_line_size),
      maxSendQueue(p.max_send_queue),
      issueLatency(p.issue_latency),
      completionLatency(p.completion_latency),
      maxOutstanding(p.max_outstanding),
      enabled(p.enabled),
      sendEvent([this] { trySend(); }, name()),
      stats(this)
{}

CIRA::~CIRA()
{
    if (registry[system] == this)
        registry.erase(system);
    reset();
}

void
CIRA::init()
{
    panic_if(!memSidePort.isConnected(),
             "CIRA memory port of %s is not connected", name());
    panic_if(registry.count(system) && registry[system] != this,
             "Only one CIRA instance per System is currently supported");
    registry[system] = this;
    ClockedObject::init();
}

Port &
CIRA::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "mem_side_port")
        return memSidePort;
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

uint64_t
CIRA::issuePrefetch(ThreadContext *tc, Addr addr, uint64_t size)
{
    if (!enabled) {
        ++stats.rejectedDisabled;
        return 0;
    }

    if (outstanding.size() >= maxOutstanding ||
        sendQueue.size() >= maxSendQueue) {
        ++stats.rejectedQueueFull;
        return 0;
    }

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

    uint64_t packetsNeeded = retryPkt ? 1 : 0;
    packetsNeeded += sendQueue.size();
    for (const auto &chunk : chunks) {
        Addr chunkOffset = 0;
        while (chunkOffset < chunk.size) {
            const uint64_t lineRemaining =
                cacheLineSize - ((chunk.paddr + chunkOffset) %
                                 cacheLineSize);
            const uint64_t pktSize = std::min(
                chunk.size - chunkOffset, lineRemaining);
            ++packetsNeeded;
            chunkOffset += pktSize;
        }
    }

    if (packetsNeeded > maxSendQueue) {
        ++stats.rejectedQueueFull;
        return 0;
    }

    const uint64_t id = nextId++;
    auto state = std::make_unique<RequestState>();
    state->id = id;
    state->tc = tc;
    state->vaddr = addr;
    state->size = installSize;
    state->issueTick = curTick();
    RequestState *rawState = state.get();
    outstanding[id] = std::move(state);

    for (const auto &chunk : chunks) {
        Addr chunkOffset = 0;
        while (chunkOffset < chunk.size) {
            const uint64_t remaining = chunk.size - chunkOffset;
            const uint64_t lineRemaining =
                cacheLineSize - ((chunk.paddr + chunkOffset) %
                                 cacheLineSize);
            const auto pktSize = static_cast<unsigned>(
                std::min(remaining, lineRemaining));

            RequestPtr req = std::make_shared<Request>(
                chunk.paddr + chunkOffset, pktSize, chunk.flags,
                requestorId);
            req->taskId(context_switch_task_id::Prefetcher);

            PacketPtr pkt = new Packet(req, MemCmd::HardPFReq);
            pkt->allocate();
            pkt->senderState = new PacketSenderState(id);
            rawState->pendingPackets++;
            enqueuePacket(pkt);
            chunkOffset += pktSize;
        }
    }

    ++stats.issuedPrefetches;

    DPRINTF(CIRA,
            "issue id=%#llx vaddr=%#llx size=%llu install_size=%llu "
            "chunks=%zu\n",
            static_cast<unsigned long long>(id),
            static_cast<unsigned long long>(addr),
            static_cast<unsigned long long>(size),
            static_cast<unsigned long long>(installSize),
            chunks.size());

    scheduleSend(curTick() + issueLatency);
    return id;
}

void
CIRA::enqueuePacket(PacketPtr pkt)
{
    panic_if(sendQueue.size() >= maxSendQueue,
             "CIRA send queue overflow after admission check");
    sendQueue.push_back(pkt);
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
    while (retryPkt || !sendQueue.empty()) {
        PacketPtr pkt = retryPkt;
        if (!pkt) {
            pkt = sendQueue.front();
            sendQueue.pop_front();
        }

        if (memSidePort.sendTimingReq(pkt)) {
            ++stats.readPackets;
            stats.readBytes += pkt->req->getSize();
            retryPkt = nullptr;
        } else {
            retryPkt = pkt;
            break;
        }
    }
}

bool
CIRA::recvTimingResp(PacketPtr pkt)
{
    auto *senderState = dynamic_cast<PacketSenderState *>(pkt->senderState);
    panic_if(!senderState, "CIRA response without CIRA sender state");

    DPRINTF(CIRA, "response id=%#llx addr=%#llx size=%u error=%d\n",
            static_cast<unsigned long long>(senderState->id),
            static_cast<unsigned long long>(pkt->getAddr()),
            pkt->req->getSize(), pkt->isError());

    const uint64_t id = senderState->id;
    const auto it = outstanding.find(id);
    if (it != outstanding.end()) {
        RequestState &state = *it->second;
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
CIRA::recvReqRetry()
{
    scheduleSend(curTick());
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

    DPRINTF(CIRA, "complete id=%#llx latency=%llu\n",
            static_cast<unsigned long long>(id),
            static_cast<unsigned long long>(curTick() - state.issueTick));

    outstanding.erase(it);
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
        return outstanding.size();
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
    while (!sendQueue.empty()) {
        deleteQueuedPacket(sendQueue.front());
        sendQueue.pop_front();
    }

    deleteQueuedPacket(retryPkt);
    retryPkt = nullptr;
    outstanding.clear();
    finished.clear();
    nextId = 1;
}

} // namespace gem5
