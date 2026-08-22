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

    struct RequestState
    {
        uint64_t id = 0;
        ReqType type = ReqType::Load;
        ThreadContext *tc = nullptr;
        Addr spmAddr = 0;
        Addr memAddr = 0;
        uint64_t size = 0;
        Tick issueTick = 0;
        std::vector<uint8_t> data;
        uint32_t pendingPackets = 0;
    };

    struct PacketSenderState : public Packet::SenderState
    {
        PacketSenderState(uint64_t request_id, bool is_read)
            : id(request_id), read(is_read)
        {}

        uint64_t id;
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
        statistics::Scalar translationCacheHits;
        statistics::Scalar translationCacheMisses;
    };

    uint64_t issue(ThreadContext *tc, ReqType type, Addr spm_addr,
                   Addr mem_addr);
    bool translate(ThreadContext *tc, Addr vaddr, uint64_t size,
                   BaseMMU::Mode mode,
                   std::vector<TranslationChunk> &chunks) const;
    bool readGuest(ThreadContext *tc, Addr addr, void *data, uint64_t size);
    bool writeGuest(ThreadContext *tc, Addr addr, const void *data,
                    uint64_t size);
    bool readSpm(Addr addr, void *data, uint64_t size) const;
    void writeSpm(Addr addr, const void *data, uint64_t size);
    void enqueuePacket(PacketPtr pkt) { /* Unused in fast path */ }
    void scheduleSend(Tick when) { /* Unused in fast path */ }
    void trySend() { /* Unused in fast path */ }
    bool recvTimingResp(PacketPtr pkt) { return true; /* Unused in fast path */ }
    void recvReqRetry() { /* Unused in fast path */ }
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
    std::vector<uint8_t> spmData;  // Changed from map to vector for efficiency
    std::deque<PacketPtr> sendQueue;
    PacketPtr retryPkt = nullptr;
    EventFunctionWrapper sendEvent;

    // Translation cache for hybrid optimization
    struct TranslationCacheEntry
    {
        Addr paddr;
        uint64_t size;
        Addr page_mask;
    };
    std::unordered_map<Addr, TranslationCacheEntry> translationCache;
    const uint64_t maxCacheEntries = 4096; // Cache up to 4K translations

    // Translation cache methods (must be after struct definition)
    bool tryCachedTranslation(Addr vaddr, uint64_t size,
                            TranslationCacheEntry &entry) const;
    void cacheTranslation(Addr vaddr, const TranslationCacheEntry &entry);

    ASMCStats stats;
};

} // namespace gem5

#endif // __MEM_ASMC_HH__
