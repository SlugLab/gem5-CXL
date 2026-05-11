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
    void enqueuePacket(PacketPtr pkt);
    void scheduleSend(Tick when);
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

    CIRAStats stats;
};

} // namespace gem5

#endif // __MEM_CIRA_HH__
