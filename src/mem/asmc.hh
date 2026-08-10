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
    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;

    static ASMC *get(System *system);

    uint64_t issueAload(ThreadContext *tc, Addr spm_addr, Addr mem_addr);
    uint64_t issueAstore(ThreadContext *tc, Addr spm_addr, Addr mem_addr);
    uint64_t getFinished(ThreadContext *tc);
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
        MemoryAccess,
        SpmWriteback,
    };

    struct RequestState
    {
        uint64_t id = 0;
        ReqType type = ReqType::Load;
        RequestPhase phase = RequestPhase::MemoryAccess;
        ThreadContext *tc = nullptr;
        Addr spmAddr = 0;
        Addr memAddr = 0;
        uint64_t size = 0;
        Tick issueTick = 0;
        std::vector<uint8_t> data;
        std::vector<TranslationChunk> spmChunks;
        uint32_t pendingPackets = 0;
        uint32_t reservedWritePackets = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        PacketSenderState(uint64_t request_id, RequestPhase request_phase,
                          bool is_read)
            : id(request_id), phase(request_phase), read(is_read)
        {}

        uint64_t id;
        RequestPhase phase;
        bool read;
    };

    class MemoryPort : public RequestPort
    {
      public:
        MemoryPort(const std::string &name, ASMC &owner);

      protected:
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;

      private:
        ASMC &owner;
    };

    struct ASMCStats : public statistics::Group
    {
        ASMCStats(statistics::Group *parent);

        statistics::Scalar issuedLoads;
        statistics::Scalar issuedStores;
        statistics::Scalar completedLoads;
        statistics::Scalar completedStores;
        statistics::Scalar rejectedQueueFull;
        statistics::Scalar rejectedSpmFull;
        statistics::Scalar translationFaults;
        statistics::Scalar readPackets;
        statistics::Scalar writePackets;
        statistics::Scalar readBytes;
        statistics::Scalar writeBytes;
        statistics::Scalar totalLatency;
        statistics::Formula avgLatency;
    };

    uint64_t issue(ThreadContext *tc, ReqType type, Addr spm_addr,
                   Addr mem_addr);
    bool translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                   BaseMMU::Mode mode,
                   std::vector<TranslationChunk> &chunks) const;
    bool readGuest(ThreadContext *tc, Addr addr, void *data, uint64_t size);
    bool readSpm(Addr addr, void *data, uint64_t size) const;
    void writeSpm(Addr addr, const void *data, uint64_t size);
    uint32_t countPackets(
        const std::vector<TranslationChunk> &chunks) const;
    void enqueuePackets(RequestState &state,
                        const std::vector<TranslationChunk> &chunks,
                        MemCmd command, RequestPhase phase);
    void startSpmWriteback(RequestState &state);
    void enqueuePacket(PacketPtr pkt);
    void scheduleSend(Tick when);
    void trySend();
    bool recvTimingResp(PacketPtr pkt);
    void recvReqRetry();
    void completeRequest(uint64_t id);
    void reset();
    void deleteQueuedPacket(PacketPtr pkt);

    static std::unordered_map<System *, ASMC *> registry;

    System *system;
    MemoryPort memSidePort;
    const RequestorID requestorId;

    const uint64_t spmSize;
    const uint64_t cacheLineSize;
    const uint64_t maxSendQueue;
    const Tick issueLatency;
    const Tick completionLatency;

    uint64_t granularity;
    uint64_t maxOutstanding;
    Tick configuredLatency;
    uint64_t nextId = 1;
    uint64_t spmUsed = 0;

    std::unordered_map<uint64_t, std::unique_ptr<RequestState>> outstanding;
    std::unordered_map<ThreadContext *, std::deque<uint64_t>> finished;
    std::unordered_map<Addr, uint8_t> spmData;
    std::deque<PacketPtr> sendQueue;
    PacketPtr retryPkt = nullptr;
    uint64_t reservedSendSlots = 0;
    EventFunctionWrapper sendEvent;

    ASMCStats stats;
};

} // namespace gem5

#endif // __MEM_ASMC_HH__
