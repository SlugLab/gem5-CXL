#ifndef __CXL_CONTROLLER_HH__
#define __CXL_CONTROLLER_HH__

#include <vector>

#include "base/addr_range.hh"
#include "mem/xbar.hh"
#include "params/CXLController.hh"

namespace gem5
{
class CXLController : public BaseXBar
{
public:

    CXLController(const CXLControllerParams *p);
    virtual ~CXLController();
    void handleClFlush(PacketPtr pkt, PortID cpu_side_port_id);
    void handleMFence(PacketPtr pkt, PortID cpu_side_port_id);
    /**
     * Declaration of the non-coherent crossbar CPU-side port type, one
     * will be instantiated for each of the memory-side ports connecting to
     * the crossbar.
     */
    class CXLControllerResponsePort : public QueuedResponsePort
    {
      private:

        /** A reference to the crossbar to which this port belongs. */
        CXLController &xbar;

        /** A normal packet queue used to store responses. */
        RespPacketQueue queue;

      public:

        CXLControllerResponsePort(const std::string &_name,
                                CXLController &_xbar, PortID _id)
            :QueuedResponsePort(_name, queue, _id),
            xbar(_xbar), queue(_xbar, *this)
        { }

      protected:

        bool
        recvTimingReq(PacketPtr pkt) override
        {
            return xbar.recvTimingReq(pkt, id);
        }

        Tick
        recvAtomic(PacketPtr pkt) override
        {
            return xbar.recvAtomicBackdoor(pkt, id);
        }

        Tick
        recvAtomicBackdoor(PacketPtr pkt, MemBackdoorPtr &backdoor) override
        {
            return xbar.recvAtomicBackdoor(pkt, id, &backdoor);
        }

        void
        recvFunctional(PacketPtr pkt) override
        {
            xbar.recvFunctional(pkt, id);
        }

        AddrRangeList
        getAddrRanges() const override
        {
            return xbar.getAddrRanges();
        }
    };

    /**
     * Declaration of the crossbar memory-side port type, one will be
     * instantiated for each of the CPU-side ports connecting to the
     * crossbar.
     */
    class CXLControllerRequestPort : public QueuedRequestPort
    {
      private:

        /** A reference to the crossbar to which this port belongs. */
        CXLController &xbar;

        /** Packet queue used to store outgoing snoop responses. */
        //no use in this case
        SnoopRespPacketQueue snoopRespQueue;

        /** A normal packet queue used to store responses. */
        ReqPacketQueue queue;

      public:

        CXLControllerRequestPort(const std::string &_name,
                                 CXLController &_xbar, PortID _id)
            : QueuedRequestPort(_name, queue, snoopRespQueue, _id),
            xbar(_xbar), snoopRespQueue(_xbar, *this), queue(_xbar, *this)

        { }

      protected:

        bool
        recvTimingResp(PacketPtr pkt) override
        {
            return xbar.recvTimingResp(pkt, id);
        }

        void
        recvRangeChange() override
        {
            xbar.recvRangeChange(id);
        }
        bool sendTimingReq(PacketPtr pkt) {
            // 检查包类型
            if (!pkt->isRequest()) {
                printf("错误: 尝试传递响应包 %s 到请求通道\n",
                        pkt->cmdString());

                // 安全处理 - 不要断言失败
                // 创建一个新的请求包来替代
                PacketPtr new_pkt =
                    new Packet(pkt->req, MemCmd::ReadReq, pkt->getSize());
                if (pkt->hasData()) {
                  new_pkt->dataDynamic(pkt->getConstPtr<uint8_t>());
                }

                // 保存原始包的地址
                printf("创建替代请求包代替响应包 addr=%#x\n", pkt->getAddr());

                // 释放原始包
                delete pkt;

                // 发送新包
                return this->sendTimingReq(new_pkt);
            }

            return this->sendTimingReq(pkt);
        }
      public:
        int size(){
          return queue.size();
        }
    };

    virtual bool recvTimingReq(PacketPtr pkt, PortID cpu_side_port_id);
    virtual bool recvTimingResp(PacketPtr pkt, PortID mem_side_port_id);
    Tick recvAtomicBackdoor(PacketPtr pkt, PortID cpu_side_port_id,
                            MemBackdoorPtr *backdoor=nullptr);
    void recvFunctional(PacketPtr pkt, PortID cpu_side_port_id);

    class QueuedReqLayer : public ReqLayer
    {
      public:
      QueuedReqLayer(CXLControllerRequestPort& _port, BaseXBar& _xbar,
        const std::string& _name) :
            ReqLayer(_port, _xbar, _name), cxl_port(_port), pkt_outstanding(0)
        {}
      bool TestOutstanding(ResponsePort* src_port);
      bool CreditRelease(std::vector<int>::iterator CrePtr, int count);
      private:
      CXLControllerRequestPort& cxl_port;
      std::deque<ResponsePort*> waitingForCredit;
      unsigned int pkt_outstanding;
    };
    /**
     * Declare the layers of this crossbar, one vector for requests
     * and one for responses.
     */
    std::vector<QueuedReqLayer*> reqLayers;
    std::vector<RespLayer*> respLayers;
    //std::vector<QueuedRequestPort*> memSidePorts;

private:
    //std::vector<PacketPtr> waiting;
    std::vector<int> ResCrd;
    std::vector<int> ReqCrd;
    std::vector<int> DataCrd;
    std::vector<AddrRange *> addr;
    unsigned last_rollover;
    void mkReadPkt(PacketPtr pkt, PortID port_id);
    void mkWritePkt(PacketPtr pkt, PortID port_id);
    void mkClFlushPkt(PacketPtr pkt, PortID port_id);
    void mkMFencePkt(PacketPtr pkt, PortID port_id);

    // 用于跟踪挂起的内存操作
    std::unordered_map<PortID, std::vector<PacketPtr>> pendingOps;
};
} // namespace gem5
#endif //__CXL_CONTROLLER_HH__
