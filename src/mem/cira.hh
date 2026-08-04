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
#include "mem/cache/cache_probe_arg.hh"
#include "mem/cira_usefulness_tracker.hh"
#include "mem/packet.hh"
#include "mem/port.hh"
#include "mem/request.hh"
#include "params/CIRA.hh"
#include "sim/clocked_object.hh"
#include "sim/probe/probe.hh"

namespace gem5
{

class ThreadContext;
class System;
class BaseCache;

class CIRA : public ClockedObject
{
  public:
    using Params = CIRAParams;

    CIRA(const Params &p);
    ~CIRA() override;

    void init() override;
    void regProbeListeners() override;
    void resetStats() override;
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
        PortID targetCore = InvalidPortID;
        ThreadContext *tc = nullptr;
        Addr vaddr = 0;
        uint64_t size = 0;
        Tick issueTick = 0;
        uint32_t pendingPackets = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        PacketSenderState(uint64_t request_id, PortID target_core)
            : id(request_id), targetCore(target_core)
        {}

        uint64_t id;
        PortID targetCore;
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
        PortID targetCore = InvalidPortID;
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
        MemoryPort(const std::string &name, CIRA &owner,
                   PortID target_core);

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;

      private:
        CIRA &owner;
        const PortID targetCore;
    };

    enum class CacheProbeEvent
    {
        Hit,
        Miss,
        Fill
    };

    class CacheProbeListener :
        public ProbeListenerArgBase<CacheAccessProbeArg>
    {
      public:
        CacheProbeListener(CIRA &owner, ProbeManager *manager,
                           const std::string &name, CacheProbeEvent event,
                           PortID target_core)
            : ProbeListenerArgBase(manager, name),
              owner(owner), event(event), targetCore(target_core)
        {}

        void notify(const CacheAccessProbeArg &arg) override;

      private:
        CIRA &owner;
        const CacheProbeEvent event;
        const PortID targetCore;
    };

    struct CIRAStats : public statistics::Group
    {
        CIRAStats(statistics::Group *parent, size_t num_cores);

        statistics::Scalar issuedPrefetches;
        statistics::Scalar issuedIndexedPrefetches;
        statistics::Scalar issuedCsrPrefetches;
        statistics::Scalar csrRowsVisited;
        statistics::Scalar droppedCsrDescriptors;
        statistics::Scalar csrQueueHighWatermark;
        statistics::Scalar completedPrefetches;
        statistics::Scalar coalescedPrefetches;
        statistics::Scalar usefulPrefetches;
        statistics::Scalar latePrefetches;
        statistics::Vector issuedCsrPrefetchesPerCore;
        statistics::Vector issuedPrefetchesPerCore;
        statistics::Vector completedPrefetchesPerCore;
        statistics::Vector coalescedPrefetchesPerCore;
        statistics::Vector usefulPrefetchesPerCore;
        statistics::Vector latePrefetchesPerCore;
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
    PortID resolveTargetCore(ThreadContext *tc) const;
    bool hasPrefetchSlot(PortID targetCore) const;
    bool hasCsrWalks() const;
    size_t queuedCsrWalks() const;
    void scheduleCsrWalk(Tick when);
    void processCsrWalk();
    void enqueuePacket(PortID targetCore, PacketPtr pkt);
    void scheduleSend(Tick when);
    void trySend();
    bool recvTimingResp(PortID targetCore, PacketPtr pkt);
    void recvReqRetry(PortID targetCore);
    void completeRequest(uint64_t id);
    void handleCacheProbe(PortID targetCore, CacheProbeEvent event,
                          const CacheAccessProbeArg &arg);
    bool isCpuDataDemand(const PacketPtr pkt) const;
    void reset();
    void deleteQueuedPacket(PacketPtr pkt);

    static std::unordered_map<System *, CIRA *> registry;

    System *system;
    std::vector<std::unique_ptr<MemoryPort>> memSidePorts;
    const RequestorID requestorId;
    const std::vector<SimObject *> demandProbeTargets;
    std::vector<BaseCache *> targetCaches;

    const uint64_t cacheLineSize;
    std::vector<CiraLineUsefulnessTracker> lineTrackers;
    const uint64_t maxSendQueue;
    const uint64_t maxCsrWalkQueue;
    const uint64_t csrLinesPerTurn;
    const Tick issueLatency;
    const Tick completionLatency;

    uint64_t maxOutstanding;
    bool enabled;
    uint64_t nextId = 1;

    std::unordered_map<uint64_t, std::unique_ptr<RequestState>> outstanding;
    std::unordered_map<ThreadContext *, std::deque<uint64_t>> finished;
    std::vector<std::deque<PacketPtr>> sendQueues;
    std::vector<PacketPtr> retryPkts;
    std::vector<bool> retryReady;
    PortID nextSendCore = 0;
    EventFunctionWrapper sendEvent;
    std::vector<std::deque<CsrWalkState>> csrWalkQueues;
    PortID nextCsrCore = 0;
    EventFunctionWrapper csrWalkEvent;
    std::vector<std::unique_ptr<CacheProbeListener>> probeListeners;

    CIRAStats stats;
};

} // namespace gem5

#endif // __MEM_CIRA_HH__
