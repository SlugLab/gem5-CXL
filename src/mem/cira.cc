/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "mem/cira.hh"

#include <algorithm>
#include <cassert>
#include <cstring>
#include <utility>
#include <vector>

#include "arch/generic/mmu.hh"
#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/thread_context.hh"
#include "debug/CIRA.hh"
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
      ADD_STAT(issuedIndexedPrefetches, statistics::units::Count::get(),
               "CIRA indexed prefetch descriptors issued"),
      ADD_STAT(issuedCsrPrefetches, statistics::units::Count::get(),
               "CIRA CSR region prefetch descriptors issued"),
      ADD_STAT(csrRowsVisited, statistics::units::Count::get(),
               "CSR rows visited by CIRA region prefetch descriptors"),
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
      csrWalkEvent([this] { processCsrWalk(); }, name() + ".csr_walk"),
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
CIRA::hasPrefetchSlot() const
{
    if (!enabled)
        return false;

    const uint64_t queuedPackets = sendQueue.size() + (retryPkt ? 1 : 0);
    return outstanding.size() < maxOutstanding &&
           queuedPackets < maxSendQueue;
}

void
CIRA::scheduleCsrWalk(Tick when)
{
    if (csrWalkQueue.empty())
        return;

    if (csrWalkEvent.scheduled()) {
        if (when < csrWalkEvent.when())
            reschedule(csrWalkEvent, when);
        return;
    }

    schedule(csrWalkEvent, when);
}

void
CIRA::processCsrWalk()
{
    while (!csrWalkQueue.empty()) {
        CsrWalkState &walk = csrWalkQueue.front();
        bool madeProgress = false;

        while (hasPrefetchSlot()) {
            bool consumedEntry = false;

            if (walk.prefetchRecords && walk.recordLine < walk.recordsEnd) {
                const Addr line = walk.recordLine;
                walk.recordLine += cacheLineSize;
                if (issuePrefetch(walk.tc, line, cacheLineSize) != 0)
                    madeProgress = true;
                consumedEntry = true;
            }

            if (walk.prefetchValues && walk.nextEntry < walk.entryCount &&
                hasPrefetchSlot()) {
                const uint64_t entry = walk.nextEntry++;
                const Addr indexAddr = walk.recordsBegin +
                    entry * walk.recordStride + walk.indexOffset;
                uint64_t index = 0;
                if (!readIndex(walk.tc, indexAddr, walk.indexSize, index)) {
                    ++stats.translationFaults;
                    consumedEntry = true;
                    continue;
                }

                const Addr target = walk.valuesAddr + index * walk.valueSize;
                if (issuePrefetch(walk.tc, target, walk.valueSize) != 0)
                    madeProgress = true;
                consumedEntry = true;
            }

            if (!consumedEntry)
                break;
        }

        const bool recordsDone =
            !walk.prefetchRecords || walk.recordLine >= walk.recordsEnd;
        const bool valuesDone =
            !walk.prefetchValues || walk.nextEntry >= walk.entryCount;
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
            csrWalkQueue.pop_front();
            continue;
        }

        if (!madeProgress || !hasPrefetchSlot()) {
            DPRINTF(CIRA,
                    "csr walk paused queued_walks=%llu outstanding=%llu "
                    "send_queue=%llu retry=%d\n",
                    static_cast<unsigned long long>(csrWalkQueue.size()),
                    static_cast<unsigned long long>(outstanding.size()),
                    static_cast<unsigned long long>(sendQueue.size()),
                    retryPkt != nullptr);
            return;
        }
    }
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

            PacketPtr pkt = new Packet(req, MemCmd::SoftPFReq);
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
            "chunks=%llu\n",
            static_cast<unsigned long long>(id),
            static_cast<unsigned long long>(addr),
            static_cast<unsigned long long>(size),
            static_cast<unsigned long long>(installSize),
            static_cast<unsigned long long>(chunks.size()));

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
        if (outstanding.size() >= maxOutstanding ||
            sendQueue.size() >= maxSendQueue) {
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

    ++stats.issuedCsrPrefetches;

    if (recordSpan) {
        if (desc.offsetsAddr == 0 || desc.recordsAddr <= desc.offsetsAddr ||
            desc.recordStride == 0) {
            DPRINTF(CIRA, "csr invalid record span descriptor\n");
            return 0;
        }

        const Addr rowRecordAddr = desc.offsetsAddr;
        const uint64_t recordBytes = desc.recordsAddr - desc.offsetsAddr;
        const uint64_t count = recordBytes / desc.recordStride;
        if (count == 0)
            return 0;

        stats.csrRowsVisited += desc.rowCount;

        CsrWalkState walk;
        walk.tc = tc;
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
        csrWalkQueue.push_back(walk);
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
                static_cast<unsigned long long>(csrWalkQueue.size()));

        return count;
    }

    uint64_t accepted = 0;
    const uint64_t rowEnd = desc.rowStart + desc.rowCount;
    for (uint64_t row = desc.rowStart; row < rowEnd; ++row) {
        if (outstanding.size() >= maxOutstanding ||
            sendQueue.size() >= maxSendQueue) {
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
            if (outstanding.size() >= maxOutstanding ||
                sendQueue.size() >= maxSendQueue) {
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
CIRA::enqueuePacket(PacketPtr pkt)
{
    panic_if(sendQueue.size() >= maxSendQueue,
             "CIRA send queue overflow after admission check");
    sendQueue.push_back(pkt);
}

void
CIRA::scheduleSend(Tick when, bool force_retry)
{
    if (retryPkt && !force_retry)
        return;

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
    PacketPtr pkt = retryPkt;
    if (!pkt) {
        if (sendQueue.empty())
            return;
        pkt = sendQueue.front();
        sendQueue.pop_front();
    }

    DPRINTF(CIRA, "send addr=%#llx size=%u cmd=%s retry=%d queued=%llu\n",
            static_cast<unsigned long long>(pkt->getAddr()),
            pkt->req->getSize(), pkt->cmd.toString(), retryPkt != nullptr,
            static_cast<unsigned long long>(sendQueue.size()));

    if (memSidePort.sendTimingReq(pkt)) {
        DPRINTF(CIRA, "send accepted addr=%#llx queued=%llu\n",
                static_cast<unsigned long long>(pkt->getAddr()),
                static_cast<unsigned long long>(sendQueue.size()));
        ++stats.readPackets;
        stats.readBytes += pkt->req->getSize();
        retryPkt = nullptr;
        if (!sendQueue.empty())
            scheduleSend(curTick() + 1);
        if (!csrWalkQueue.empty())
            scheduleCsrWalk(curTick() + 1);
    } else {
        DPRINTF(CIRA, "send blocked addr=%#llx queued=%llu\n",
                static_cast<unsigned long long>(pkt->getAddr()),
                static_cast<unsigned long long>(sendQueue.size()));
        retryPkt = pkt;
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
    DPRINTF(CIRA, "recvReqRetry queued=%llu retry=%d\n",
            static_cast<unsigned long long>(sendQueue.size()),
            retryPkt != nullptr);
    scheduleSend(curTick(), true);
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
    if (!csrWalkQueue.empty())
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
    if (sendEvent.scheduled())
        deschedule(sendEvent);
    if (csrWalkEvent.scheduled())
        deschedule(csrWalkEvent);

    while (!sendQueue.empty()) {
        deleteQueuedPacket(sendQueue.front());
        sendQueue.pop_front();
    }

    deleteQueuedPacket(retryPkt);
    retryPkt = nullptr;
    csrWalkQueue.clear();
    outstanding.clear();
    finished.clear();
    nextId = 1;
}

} // namespace gem5
