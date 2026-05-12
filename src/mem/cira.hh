/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_CIRA_HH__
#define __MEM_CIRA_HH__

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#include "arch/generic/mmu.hh"
#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/packet.hh"
#include "mem/port.hh"
#include "mem/request.hh"
#include "params/CIRA.hh"
#include "sim/clocked_object.hh"

namespace gem5
{

class ThreadContext;
class System;

class CIRA : public ClockedObject
{
  public:
    using Params = CIRAParams;

    CIRA(const Params &p);
    ~CIRA() override;

    void init() override;
    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;

    static CIRA *get(System *system);

    uint64_t issuePrefetch(ThreadContext *tc, Addr addr, uint64_t size);
    uint64_t issueIndexedPrefetch(ThreadContext *tc, Addr base_addr,
                                  Addr records_addr, uint64_t count,
                                  uint64_t record_stride,
                                  uint64_t index_offset,
                                  uint64_t index_size,
                                  uint64_t value_size);
    uint64_t issueCsrPrefetch(ThreadContext *tc, Addr offsets_addr,
                              Addr records_addr, Addr values_addr,
                              uint64_t row_start, uint64_t row_count,
                              uint64_t packed);
    uint64_t getFinished(ThreadContext *tc);
    uint64_t cfgWrite(ThreadContext *tc, uint64_t reg, uint64_t value);
    uint64_t cfgRead(ThreadContext *tc, uint64_t reg) const;

  private:
    struct TranslationChunk
    {
        Addr paddr = 0;
        Addr offset = 0;
        Addr size = 0;
        Request::Flags flags = 0;
    };

    struct RequestState
    {
        uint64_t id = 0;
        ThreadContext *tc = nullptr;
        Addr vaddr = 0;
        uint64_t size = 0;
        Tick issueTick = 0;
        uint32_t pendingPackets = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        explicit PacketSenderState(uint64_t request_id)
            : id(request_id)
        {}

        uint64_t id;
    };

    struct IndexedPrefetchDesc
    {
        uint64_t baseAddr = 0;
        uint64_t recordsAddr = 0;
        uint64_t count = 0;
        uint64_t recordStride = 0;
        uint64_t indexOffset = 0;
        uint64_t indexSize = 0;
        uint64_t valueSize = 0;
    };

    struct CsrPrefetchDesc
    {
        uint64_t offsetsAddr = 0;
        uint64_t recordsAddr = 0;
        uint64_t valuesAddr = 0;
        uint64_t rowStart = 0;
        uint64_t rowCount = 0;
        uint64_t offsetSize = 0;
        uint64_t recordStride = 0;
        uint64_t indexOffset = 0;
        uint64_t indexSize = 0;
        uint64_t valueSize = 0;
        uint64_t flags = 0;
    };

    struct CsrWalkState
    {
        ThreadContext *tc = nullptr;
        Addr recordsBegin = 0;
        Addr recordsEnd = 0;
        Addr recordLine = 0;
        Addr valuesAddr = 0;
        uint64_t recordStride = 0;
        uint64_t indexOffset = 0;
        uint64_t indexSize = 0;
        uint64_t valueSize = 0;
        uint64_t entryCount = 0;
        uint64_t nextEntry = 0;
        uint64_t rowStart = 0;
        uint64_t rowCount = 0;
        bool prefetchRecords = false;
        bool prefetchValues = false;
    };

    class MemoryPort : public RequestPort
    {
      public:
        MemoryPort(const std::string &name, CIRA &owner);

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;

      private:
        CIRA &owner;
    };

    struct CIRAStats : public statistics::Group
    {
        CIRAStats(statistics::Group *parent);

        statistics::Scalar issuedPrefetches;
        statistics::Scalar issuedIndexedPrefetches;
        statistics::Scalar issuedCsrPrefetches;
        statistics::Scalar csrRowsVisited;
        statistics::Scalar completedPrefetches;
        statistics::Scalar rejectedDisabled;
        statistics::Scalar rejectedQueueFull;
        statistics::Scalar translationFaults;
        statistics::Scalar readPackets;
        statistics::Scalar readBytes;
        statistics::Scalar totalLatency;
        statistics::Formula avgLatency;
    };

    bool translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                   std::vector<TranslationChunk> &chunks) const;
    bool readGuest(ThreadContext *tc, Addr addr, void *data,
                   uint64_t size) const;
    bool readIndex(ThreadContext *tc, Addr addr, uint64_t index_size,
                   uint64_t &index) const;
    bool hasPrefetchSlot() const;
    void scheduleCsrWalk(Tick when);
    void processCsrWalk();
    void enqueuePacket(PacketPtr pkt);
    void scheduleSend(Tick when, bool force_retry = false);
    void trySend();
    bool recvTimingResp(PacketPtr pkt);
    void recvReqRetry();
    void completeRequest(uint64_t id);
    void reset();
    void deleteQueuedPacket(PacketPtr pkt);

    static std::unordered_map<System *, CIRA *> registry;

    System *system;
    MemoryPort memSidePort;
    const RequestorID requestorId;

    const uint64_t cacheLineSize;
    const uint64_t maxSendQueue;
    const Tick issueLatency;
    const Tick completionLatency;

    uint64_t maxOutstanding;
    bool enabled;
    uint64_t nextId = 1;

    std::unordered_map<uint64_t, std::unique_ptr<RequestState>> outstanding;
    std::unordered_map<ThreadContext *, std::deque<uint64_t>> finished;
    std::deque<PacketPtr> sendQueue;
    PacketPtr retryPkt = nullptr;
    EventFunctionWrapper sendEvent;
    std::deque<CsrWalkState> csrWalkQueue;
    EventFunctionWrapper csrWalkEvent;

    CIRAStats stats;
};

} // namespace gem5

#endif // __MEM_CIRA_HH__
