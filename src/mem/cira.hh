/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_CIRA_HH__
#define __MEM_CIRA_HH__

#include <array>
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

#include "../../util/pr_offload/pr_row_offload.h"

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
    uint64_t issuePrRows(ThreadContext *tc, Addr desc_addr);
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

    enum class PacketRole
    {
        PrefetchLine,
        CsrIndexRead
    };

    enum class PrStage
    {
        StartRow,
        ContribRead,
        ContribCompute,
        PullOffsets,
        PullNeighbors,
        PullContributions,
        PullCompute,
        WriteResult,
    };

    enum class PrPacketRole
    {
        CsrRead,
        CoherentRead,
        CoherentWrite,
    };

    enum class PrPayloadRole
    {
        Score,
        Degree,
        Offsets,
        Neighbor,
        Contribution,
        Result,
    };

    struct PrDescriptorState
    {
        uint64_t id = 0;
        PortID targetCore = InvalidPortID;
        ThreadContext *tc = nullptr;
        pr_row_offload_desc desc = {};
        PrStage stage = PrStage::StartRow;
        uint64_t row = 0;
        uint64_t edgeBegin = 0;
        uint64_t edgeEnd = 0;
        uint64_t nextRead = 0;
        uint32_t pendingPackets = 0;
        uint32_t reservedInitialCsrPackets = 0;
        uint32_t reservedInitialCoherentPackets = 0;
        Tick issueTick = 0;
        Tick policyReadyTick = 0;
        Tick stallStart = 0;
        bool queueStalled = false;
        std::array<uint8_t, 4> scoreData = {};
        std::array<uint8_t, 8> degreeData = {};
        std::array<uint8_t, 16> offsetsData = {};
        std::vector<int32_t> neighbors;
        std::vector<float> contributions;
        float result = 0.0f;
    };

    struct PrThreadConfig
    {
        uint64_t rowWindow = 1;
        uint64_t leadBlocks = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        PacketSenderState(PacketRole packet_role, uint64_t request_id,
                          PortID target_core, uint64_t walk_id = 0,
                          uint64_t entry_id = 0, uint64_t data_offset = 0)
            : role(packet_role), id(request_id), targetCore(target_core),
              walkId(walk_id), entry(entry_id), dataOffset(data_offset)
        {}

        PacketSenderState(PrPacketRole pr_role,
                          PrPayloadRole payload_role,
                          uint64_t request_id, PortID target_core,
                          uint64_t index, uint64_t data_offset)
            : role(PacketRole::PrefetchLine), id(request_id),
              targetCore(target_core), walkId(0), entry(0),
              dataOffset(data_offset), prPacket(true), prRole(pr_role),
              prPayload(payload_role), prIndex(index)
        {}

        PacketRole role;
        uint64_t id;
        PortID targetCore;
        uint64_t walkId;
        uint64_t entry;
        uint64_t dataOffset;
        bool prPacket = false;
        PrPacketRole prRole = PrPacketRole::CsrRead;
        PrPayloadRole prPayload = PrPayloadRole::Offsets;
        uint64_t prIndex = 0;
    };

    struct PendingCsrIndexRead
    {
        uint64_t walkId = 0;
        PortID targetCore = InvalidPortID;
        ThreadContext *tc = nullptr;
        Addr valuesAddr = 0;
        uint64_t valueSize = 0;
        uint64_t indexSize = 0;
        uint32_t pendingPackets = 0;
        std::array<uint8_t, 8> data = {};
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
        uint64_t walkId = 0;
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
        uint64_t pendingIndexReads = 0;
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

    class CsrMemoryPort : public RequestPort
    {
      public:
        CsrMemoryPort(const std::string &name, CIRA &owner);

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;

      private:
        CIRA &owner;
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
        statistics::Scalar csrIndexReadPackets;
        statistics::Scalar csrIndexReadBytes;
        statistics::Scalar completedCsrIndexReads;
        statistics::Scalar rejectedCsrIndexQueueFull;
        statistics::Scalar timingCsrTraversalEnabled;
        statistics::Scalar totalLatency;
        statistics::Scalar issuedPrDescriptors;
        statistics::Scalar completedPrDescriptors;
        statistics::Scalar rejectedPrDescriptors;
        statistics::Scalar prRows;
        statistics::Scalar prCsrReads;
        statistics::Scalar prCoherentReads;
        statistics::Scalar prCoherentWrites;
        statistics::Scalar prComputeTicks;
        statistics::Scalar prQueueStallTicks;
        statistics::Scalar prPolicyFormationTicks;
        statistics::Scalar issuedPrReconfigurations;
        statistics::Scalar completedPrReconfigurations;
        statistics::Scalar usefulHoists;
        statistics::Scalar ineffectiveHoists;
        statistics::Scalar prOutstandingWork;
        statistics::Scalar prHighWatermark;
        statistics::Vector issuedPrDescriptorsPerCore;
        statistics::Vector completedPrDescriptorsPerCore;
        statistics::Vector prRowsPerCore;
        statistics::Vector prCsrReadsPerCore;
        statistics::Vector prCoherentReadsPerCore;
        statistics::Vector prCoherentWritesPerCore;
        statistics::Vector prComputeTicksPerCore;
        statistics::Vector prQueueStallTicksPerCore;
        statistics::Vector prPolicyFormationTicksPerCore;
        statistics::Vector issuedPrReconfigurationsPerCore;
        statistics::Vector completedPrReconfigurationsPerCore;
        statistics::Vector usefulHoistsPerCore;
        statistics::Vector ineffectiveHoistsPerCore;
        statistics::Vector prOutstandingWorkPerCore;
        statistics::Vector prHighWatermarkPerCore;
        statistics::Formula avgLatency;
    };

    bool translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                   std::vector<TranslationChunk> &chunks) const;
    bool translatePr(ThreadContext *tc, Addr vaddr, uint64_t size,
                     BaseMMU::Mode mode, bool csr,
                     std::vector<TranslationChunk> &chunks) const;
    uint32_t countPackets(
        const std::vector<TranslationChunk> &chunks) const;
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
    bool issueCsrIndexRead(CsrWalkState &walk, uint64_t entry);
    bool finishCsrIndexRead(uint64_t id);
    CsrWalkState *findCsrWalk(PortID targetCore, uint64_t walkId);
    void enqueueCsrIndexPacket(PacketPtr pkt);
    void scheduleCsrIndexSend(Tick when);
    void tryCsrIndexSend();
    bool recvCsrTimingResp(PacketPtr pkt);
    void recvCsrReqRetry();
    void enqueuePacket(PortID targetCore, PacketPtr pkt);
    void scheduleSend(Tick when);
    void trySend();
    bool recvTimingResp(PortID targetCore, PacketPtr pkt);
    void recvReqRetry(PortID targetCore);
    bool validatePrDescriptor(ThreadContext *tc,
                              const pr_row_offload_desc &desc) const;
    uint64_t prPolicyCostPpm(uint64_t rowWindow,
                             uint64_t leadBlocks) const;
    bool reservePrRead(PrDescriptorState &state, Addr addr, uint64_t size,
                       PrPacketRole route, PrPayloadRole payload,
                       uint64_t index);
    bool reservePrWrite(PrDescriptorState &state, Addr addr,
                        const void *data, uint64_t size);
    void schedulePr(PortID targetCore, Tick when);
    void processPr(PortID targetCore);
    bool processPrDescriptor(PrDescriptorState &state);
    bool recvPrTimingResp(PortID targetCore, PacketPtr pkt,
                          PacketSenderState *senderState);
    bool recvPrCsrTimingResp(PacketPtr pkt,
                             PacketSenderState *senderState);
    void schedulePrCompute(uint64_t id, Cycles cycles);
    void finishPrCompute(uint64_t id);
    void advancePrRow(PrDescriptorState &state);
    void completePrDescriptor(uint64_t id);
    void completePrReconfiguration(uint64_t id);
    void notePrStall(PrDescriptorState &state);
    void clearPrStall(PrDescriptorState &state);
    void completeRequest(uint64_t id);
    void handleCacheProbe(PortID targetCore, CacheProbeEvent event,
                          const CacheAccessProbeArg &arg);
    bool isCpuDataDemand(const PacketPtr pkt) const;
    void reset();
    void deleteQueuedPacket(PacketPtr pkt);

    static std::unordered_map<System *, CIRA *> registry;

    System *system;
    std::vector<std::unique_ptr<MemoryPort>> memSidePorts;
    std::unique_ptr<CsrMemoryPort> csrMemoryPort;
    const RequestorID requestorId;
    const std::vector<SimObject *> demandProbeTargets;
    std::vector<BaseCache *> targetCaches;

    const uint64_t cacheLineSize;
    std::vector<CiraLineUsefulnessTracker> lineTrackers;
    const uint64_t maxSendQueue;
    const uint64_t maxCsrWalkQueue;
    const uint64_t maxCsrIndexReads;
    const uint64_t csrLinesPerTurn;
    const Tick issueLatency;
    const Tick completionLatency;
    const uint64_t prDescriptorEntries;
    const uint64_t prCsrReadEntries;
    const uint64_t prCoherentEntries;
    const Cycles prFpAddCycles;
    const Cycles prFpMulCycles;
    const Cycles prFpDivCycles;
    const Tick prReconfigurationLatency;
    const Cycles prPolicyBaseCycles;
    const uint64_t prPolicyACostPpm;
    const uint64_t prPolicyBCostPpm;
    const uint64_t prPolicyCCostPpm;

    uint64_t maxOutstanding;
    bool enabled;
    const bool timingCsrTraversal;
    uint64_t nextId = 1;
    uint64_t nextCsrWalkId = 1;
    uint64_t nextCsrIndexReadId = 1;

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
    std::unordered_map<uint64_t, PendingCsrIndexRead> pendingCsrIndexReads;
    std::deque<PacketPtr> csrIndexSendQueue;
    PacketPtr csrIndexRetryPkt = nullptr;
    bool csrIndexRetryReady = false;
    EventFunctionWrapper csrIndexSendEvent;
    std::vector<std::unique_ptr<CacheProbeListener>> probeListeners;
    std::unordered_map<uint64_t, std::unique_ptr<PrDescriptorState>>
        prOutstanding;
    std::vector<std::deque<uint64_t>> prDescriptors;
    std::vector<std::unique_ptr<EventFunctionWrapper>> prEvents;
    std::vector<uint64_t> reservedPrCoherentSlots;
    std::vector<uint64_t> pendingPrCoherentPackets;
    uint64_t reservedPrCsrSlots = 0;
    uint64_t pendingPrCsrPackets = 0;
    uint64_t prEpoch = 0;
    std::unordered_map<ThreadContext *, PrThreadConfig> prThreadConfigs;
    std::unordered_map<uint64_t, ThreadContext *> prReconfigurations;

    CIRAStats stats;
};

} // namespace gem5

#endif // __MEM_CIRA_HH__
