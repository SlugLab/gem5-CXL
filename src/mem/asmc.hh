/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_ASMC_HH__
#define __MEM_ASMC_HH__

#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "arch/generic/mmu.hh"
#include "base/statistics.hh"
#include "base/types.hh"
#include "mem/packet.hh"
#include "mem/port.hh"
#include "mem/request.hh"
#include "params/ASMC.hh"
#include "sim/clocked_object.hh"

namespace gem5
{

class ThreadContext;
class System;

class ASMC : public ClockedObject
{
  public:
    using Params = ASMCParams;

    ASMC(const Params &p);
    ~ASMC() override;

    void init() override;
    void resetStats() override;
    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;

    static ASMC *get(System *system);

    uint64_t issueAload(ThreadContext *tc, Addr spm_addr, Addr mem_addr);
    uint64_t issueAstore(ThreadContext *tc, Addr spm_addr, Addr mem_addr);
    uint64_t getFinished(ThreadContext *tc);
    bool quiesceUntilCompletion(ThreadContext *tc);
    uint64_t cfgWrite(ThreadContext *tc, uint64_t reg, uint64_t value);
    uint64_t cfgRead(ThreadContext *tc, uint64_t reg) const;

  private:
    enum class ReqType
    {
        Load,
        Store,
    };

    struct TranslationChunk
    {
        Addr paddr = 0;
        Addr offset = 0;
        Addr size = 0;
        Request::Flags flags = 0;
    };

    enum class RequestPhase
    {
        SpmRead,
        MemoryAccess,
        SpmAcquire,
        SpmWriteback,
    };

    struct ThreadConfig
    {
        uint64_t granularity;
        uint64_t maxOutstanding;
        Tick configuredLatency;
    };

    struct RequestState
    {
        uint64_t id = 0;
        ReqType type = ReqType::Load;
        RequestPhase phase = RequestPhase::MemoryAccess;
        unsigned targetCore = 0;
        ThreadContext *tc = nullptr;
        Addr spmAddr = 0;
        Addr memAddr = 0;
        uint64_t size = 0;
        Tick configuredLatency = 0;
        Tick issueTick = 0;
        std::vector<uint8_t> data;
        std::vector<TranslationChunk> memoryChunks;
        std::vector<TranslationChunk> spmChunks;
        std::vector<PacketPtr> spmWritebacks;
        uint32_t pendingPackets = 0;
        uint32_t reservedFarPackets = 0;
        uint32_t reservedSpmPackets = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        PacketSenderState(uint64_t request_id, RequestPhase request_phase,
                          PortID target_core, Addr byte_offset,
                          unsigned packet_size, bool is_read,
                          unsigned fragment_offset = 0,
                          unsigned fragment_size = 0)
            : id(request_id), phase(request_phase), targetCore(target_core),
              byteOffset(byte_offset), size(packet_size), read(is_read),
              fragmentOffset(fragment_offset),
              fragmentSize(fragment_size ? fragment_size : packet_size)
        {}

        uint64_t id;
        RequestPhase phase;
        PortID targetCore;
        Addr byteOffset;
        unsigned size;
        bool read;
        unsigned fragmentOffset;
        unsigned fragmentSize;
    };

    class MemoryPort : public RequestPort
    {
      public:
        MemoryPort(const std::string &name, ASMC &owner,
                   PortID target_core = InvalidPortID);

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;

      private:
        ASMC &owner;
        const PortID targetCore;
    };

    struct ASMCStats : public statistics::Group
    {
        ASMCStats(ASMC &owner, size_t num_cores);

        void preDumpStats() override;

        ASMC &owner;

        statistics::Scalar issuedLoads;
        statistics::Scalar issuedStores;
        statistics::Scalar completedLoads;
        statistics::Scalar completedStores;
        statistics::Scalar rejectedQueueFull;
        statistics::Scalar rejectedSpmFull;
        statistics::Scalar translationFaults;
        statistics::Scalar farReadPackets;
        statistics::Scalar farWritePackets;
        statistics::Scalar farRetries;
        statistics::Scalar farSpmFlagPackets;
        statistics::Vector spmReadPackets;
        statistics::Vector spmWritePackets;
        statistics::Vector spmRetries;
        statistics::Scalar spmMissingFlagPackets;
        statistics::Scalar totalLatency;
        statistics::Scalar outstandingIntegral;
        statistics::Scalar occupancyTicks;
        statistics::Scalar maxObservedOutstanding;
        statistics::Scalar pendingQueueFull;
        statistics::Scalar idBatchRefills;
        statistics::Scalar metadataAccesses;
        statistics::Scalar emptyGetfinPolls;
        statistics::Scalar successfulGetfin;
        statistics::Scalar consumerWaitTicks;
        statistics::Formula avgLatency;
        statistics::Formula avgOutstanding;
    };

    uint64_t issue(ThreadContext *tc, ReqType type, Addr spm_addr,
                   Addr mem_addr);
    ThreadConfig &configFor(ThreadContext *tc);
    const ThreadConfig &configFor(ThreadContext *tc) const;
    bool translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                   BaseMMU::Mode mode,
                   std::vector<TranslationChunk> &chunks) const;
    uint32_t countPackets(
        const std::vector<TranslationChunk> &chunks) const;
    void enqueueFarPackets(RequestState &state, MemCmd command,
                           RequestPhase phase);
    void enqueueSpmPackets(RequestState &state, MemCmd command,
                           RequestPhase phase);
    void enqueueSpmAcquirePackets(RequestState &state);
    void startInitialAccess(uint64_t id);
    void startCompletionService(uint64_t id);
    void activateCompletionService(uint64_t id);
    void finishCompletionService(uint64_t id);
    void startMemoryWrite(RequestState &state);
    void startSpmWriteback(RequestState &state);
    void startSpmLineWrites(RequestState &state);
    void enqueueFarPacket(PacketPtr pkt);
    void enqueueSpmPacket(PortID target_core, PacketPtr pkt);
    void scheduleFarSend(Tick when);
    void scheduleSpmSend(PortID target_core, Tick when);
    void tryFarSend();
    void trySpmSend(PortID target_core);
    bool recvTimingResp(PortID target_core, PacketPtr pkt);
    void recvReqRetry(PortID target_core);
    void completeRequest(uint64_t id);
    void updateOccupancyIntegral();
    void reset();
    void deleteQueuedPacket(PacketPtr pkt);

    static std::unordered_map<System *, ASMC *> registry;

    System *system;
    MemoryPort memSidePort;
    std::vector<std::unique_ptr<MemoryPort>> spmSidePorts;
    const RequestorID requestorId;

    const uint64_t spmSize;
    const uint64_t cacheLineSize;
    const uint64_t maxSendQueue;
    const uint64_t spmSendQueueSize;
    const uint64_t pendingQueueEntries;
    const uint64_t idBatchEntries;
    const Cycles metadataLatency;
    const Cycles idRefillLatency;
    const Cycles completionPublishLatency;
    const Tick issueLatency;
    const Tick completionLatency;

    const ThreadConfig defaultConfig;
    uint64_t nextId = 1;
    uint64_t spmUsed = 0;
    uint64_t metadataPending = 0;
    uint64_t completionPending = 0;
    uint64_t idsRemaining = 0;
    Tick lastOccupancyTick = 0;

    std::unordered_map<uint64_t, std::unique_ptr<RequestState>> outstanding;
    std::unordered_map<ThreadContext *, ThreadConfig> threadConfigs;
    std::unordered_map<ThreadContext *, uint64_t> outstandingPerThread;
    std::unordered_map<ThreadContext *, std::deque<uint64_t>> finished;
    std::unordered_map<ThreadContext *, Tick> pollWaitStart;
    std::unordered_set<ThreadContext *> completionWaiters;
    std::deque<uint64_t> completionWaitQueue;
    std::deque<PacketPtr> farSendQueue;
    PacketPtr farRetryPkt = nullptr;
    bool farRetryReady = false;
    uint64_t reservedFarSendSlots = 0;
    EventFunctionWrapper farSendEvent;
    std::vector<std::deque<PacketPtr>> spmSendQueues;
    std::vector<PacketPtr> spmRetryPkts;
    std::vector<bool> spmRetryReady;
    std::vector<uint64_t> reservedSpmSendSlots;
    std::vector<std::unique_ptr<EventFunctionWrapper>> spmSendEvents;

    ASMCStats stats;
};

} // namespace gem5

#endif // __MEM_ASMC_HH__
