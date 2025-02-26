#include "mem/cxl_controller.hh"

#include <cassert>

#include "base/logging.hh"
#include "base/random.hh"
#include "base/trace.hh"
#include "debug/CXLController.hh"
#include "debug/CXLPerf.hh"
#include "mem/cxl_protocol.hh"

namespace gem5
{
CXLController::CXLController(const CXLControllerParams* p) :
    BaseXBar(*p)
{
    // create the ports based on the size of the memory-side port and
    // CPU-side port vector ports, and the presence of the default port,
    // the ports are enumerated starting from zero
    for (int i = 0; i < p->port_mem_side_ports_connection_count; ++i) {
        std::string portName = csprintf("%s.mem_side_port[%d]", name(), i);
        CXLControllerRequestPort* bp = new CXLControllerRequestPort(portName,
                                *this, i);
        memSidePorts.push_back(bp);
        reqLayers.push_back(new QueuedReqLayer(*bp, *this,
                                         csprintf("reqLayer%d", i)));

    }

    // see if we have a default CPU-side-port device connected and if so add
    // our corresponding memory-side port
    if (p->port_default_connection_count) {
        defaultPortID = memSidePorts.size();
        std::string portName = name() + ".default";
        CXLControllerRequestPort* bp = new CXLControllerRequestPort(
            portName, *this, defaultPortID);
        memSidePorts.push_back(bp);
        reqLayers.push_back(new QueuedReqLayer(
            *bp, *this, csprintf("reqLayer%d", defaultPortID)));
    }

    // create the CPU-side ports, once again starting at zero
    for (int i = 0; i < p->port_cpu_side_ports_connection_count; ++i) {
        std::string portName = csprintf("%s.cpu_side_ports[%d]", name(), i);
        QueuedResponsePort* bp = new CXLControllerResponsePort(portName,
                                                                *this, i);
        cpuSidePorts.push_back(bp);
        respLayers.push_back(new RespLayer(*bp, *this,
                                           csprintf("respLayer%d", i)));
    }

    DPRINTF(CXLController, "hello world from cxl controller!\n");
    for (int i = 0; i < 2; i++)
    {
        ReqCrd.push_back(64);
        ResCrd.push_back(64);
        DataCrd.push_back(64);
        //addr.push_back(new AddrRange(
        //    0x20000000+i*0x20000000, 0x20000000+(i+1)*0x20000000));
    }

    //push back local credit
    ReqCrd.push_back(64);
    ResCrd.push_back(64);
    DataCrd.push_back(64);
}

CXLController::~CXLController()
{
    for (auto l: reqLayers)
        delete l;
    for (auto l: respLayers)
        delete l;
}

CXLController*
CXLControllerParams::create() const
{
    return new CXLController(this);
}

bool CXLController::recvTimingReq(PacketPtr pkt, PortID cpu_side_port_id){
    // determine the source port based on the id
    ResponsePort *src_port = cpuSidePorts[cpu_side_port_id];

    // we should never see express snoops on a non-coherent crossbar
    assert(!pkt->isExpressSnoop());

    // determine the destination based on the address
    PortID mem_side_port_id = findPort(pkt->getAddrRange());

    //we can decide if the sender needs to retry in one cycle after
    //recieve data valid signal from the sender.
    if (!reqLayers[mem_side_port_id]->TestOutstanding(src_port))
    {
        DPRINTF(CXLPerf,
                "CXLPerf: src %s %s 0x%x WAITING FOR CREDIT\n",
                src_port->name(), pkt->cmdString(), pkt->getAddr());
        return false;
    }

    // test if the layer should be considered occupied for the current
    // port
    //waiting.push_back(pkt);
    if (!reqLayers[mem_side_port_id]->tryTiming(src_port)) {
        DPRINTF(CXLController, "recvTimingReq: src %s %s 0x%x BUSY\n",
                src_port->name(), pkt->cmdString(), pkt->getAddr());
        return false;
    }

    //pkt = waiting.front();
    //waiting.erase(waiting.begin());
    DPRINTF(CXLController, "recvTimingReq: src %s %s 0x%x %d\n",
            src_port->name(), pkt->cmdString(),
            pkt->getAddr(), pkt->getSize());



    // store the old header delay so we can restore it if needed
    Tick old_header_delay = pkt->headerDelay;
    if (pkt->cmd == MemCmd::ClFlush) {
        // 直接处理CLFLUSH请求
        handleClFlush(pkt, cpu_side_port_id);
        return true;
    }
    if (pkt->cmd == MemCmd::MFence) {
        // 直接处理 MFENCE 请求
        handleMFence(pkt, cpu_side_port_id);
        return true;
    }
    if (pkt->isRead())
        mkReadPkt(pkt, mem_side_port_id);
    else if (pkt->isWriteback())
        mkWritePkt(pkt, mem_side_port_id);

    // store size and command as they might be modified when
    // forwarding the packet
    unsigned int pkt_size = pkt->getSize();
    unsigned int pkt_cmd = pkt->cmdToIndex();

    // a request sees the frontend and forward latency
    Tick xbar_delay = (frontendLatency + forwardLatency) * clockPeriod();

    // set the packet header and payload delay
    calcPacketTiming(pkt, xbar_delay);

    // determine how long the controller recieves whole packet
    Tick packetFinishTime = clockEdge(Cycles(1)) + pkt->payloadDelay;

    // send the packet through the destination mem-side port, and pay for
    // any outstanding latency
    //packet is put on queue after controller recieving packet header
    Tick latency = pkt->headerDelay;
    pkt->headerDelay = 0;
    //update packet size
    //we store CXL command in @cmd, CXL packet size in @size,
    //old size in @cxl_size, old command in @cxl_comm.
    //In this way, we do not need to update other Classes.
    pkt->update_size(pkt->cxl_size);
    pkt->cxl_size = pkt_size;
    pkt->cmd = pkt->cxl_comm;
    pkt->cxl_comm = MemCmd::Command(pkt_cmd);
    if (!pkt->isRequest()) {
        pkt->cmd = MemCmd::Command::MemRd; // 或其他适当的请求类型
    }
    //Send data flit if rollover equals to 4
    //after write request sent
    //Actually, we only update stats and packet size
    if (last_rollover == 4){
        last_rollover = 0;
        //PacketPtr data_flit = new Packet(pkt, false, true);
        //data_flit->cxl_comm = MemCmd::Command::DataFlit;
        //((CXLControllerRequestPort*)memSidePorts[mem_side_port_id])
        //->schedTimingReq(pkt, curTick() + latency +  nextCycle());
        // stats updates
        pkt->update_size(pkt->getSize() + DATA_FLIT);
        //pktCount[cpu_side_port_id][mem_side_port_id]++;
        //pktSize[cpu_side_port_id][mem_side_port_id] += (pkt->getSize());
        //transDist[(int)(MemCmd::Command::DataFlit)]++;
    }
    ((CXLControllerRequestPort*)memSidePorts[mem_side_port_id])
                            ->schedTimingReq(pkt, curTick() + latency);

    // before forwarding the packet (and possibly altering it),
    // remember if we are expecting a response
    if (pkt->needsResponse()) {
        // 安全检查req是否有效
        if (!pkt->req || pkt->req.use_count() < 1) {
            DPRINTF(CXLController, "警告: 无效的请求对象指针\n");
            // 创建新的请求对象
            RequestPtr new_req = std::make_shared<Request>(
                pkt->getAddr(), pkt->getSize(), 0, 0);
            pkt->req = new_req;
        }

        // 检查路由表大小，防止过度增长
        if (routeTo.size() > 1000) {
          DPRINTF(CXLController, "警告: 路由表过大 (%d 条目), 正在清理\n",
                  routeTo.size());
          routeTo.clear(); // 极端情况下清空路由表
        }

        // 使用安全的方式添加到路由表
        routeTo[pkt->req] = cpu_side_port_id;
    }
    reqLayers[mem_side_port_id]->succeededTiming(packetFinishTime);

    // stats updates
    pktCount[cpu_side_port_id][mem_side_port_id]++;
    pktSize[cpu_side_port_id][mem_side_port_id] += pkt_size;
    transDist[pkt_cmd]++;

    return true;
}

bool CXLController::recvTimingResp(PacketPtr pkt, PortID mem_side_port_id) {
    // determine the source port based on the id
    static Tick lastRespTick = 0;
    if (curTick() - lastRespTick > 100000 * clockPeriod()) {
        // 长时间没有响应，可能有死锁
        DPRINTF(CXLController, "检测到潜在死锁，清空路由表\n");
        routeTo.clear();
    }
    lastRespTick = curTick();
    RequestPort *src_port = memSidePorts[mem_side_port_id];

    // determine the destination
    const auto route_lookup = routeTo.find(pkt->req);
    if (route_lookup == routeTo.end()) {
        // 关键变更：创建一个新的包，而不是修改原始包
        // 这样可以避免CoherentXBar看到不一致的状态
        PacketPtr new_pkt = nullptr;

        if (pkt->isResponse()) {
            // 复制响应信息
            new_pkt = new Packet(pkt->req, pkt->cmd, pkt->getSize());
            if (pkt->hasData()) {
                new_pkt->dataDynamic(pkt->getConstPtr<uint8_t>());
            }

            // 复制必要的头部信息
            new_pkt->headerDelay = pkt->headerDelay;
            new_pkt->payloadDelay = pkt->payloadDelay;

            // 尝试发送到任何可用端口
            for (int i = 0; i < cpuSidePorts.size(); i++) {
                if (cpuSidePorts[i]->sendTimingResp(new_pkt)) {
                    DPRINTF(CXLController, "成功将克隆响应转发到端口 %d\n", i);
                    // 原始包已不需要了
                    delete pkt;
                    return true;
                }
            }

            // 发送失败，清理新包
            delete new_pkt;
        }

        // 重要：安全地丢弃原始包
        DPRINTF(CXLController, "无法转发响应，安全丢弃\n");
        return true;
    }

    // 正常的处理逻辑...
    // determine the destination
    assert(route_lookup != routeTo.end());
    const PortID cpu_side_port_id = route_lookup->second;
    assert(cpu_side_port_id != InvalidPortID);
    assert(cpu_side_port_id < respLayers.size());

    // test if the layer should be considered occupied for the current
    // port
    if (!respLayers[cpu_side_port_id]->tryTiming(src_port)) {
        DPRINTF(CXLController, "recvTimingResp: src %s %s 0x%x BUSY\n",
                src_port->name(), pkt->cmdString(), pkt->getAddr());
        return false;
    }

    DPRINTF(CXLController, "recvTimingResp: src %s %s 0x%x\n",
            src_port->name(), pkt->cmdString(), pkt->getAddr());

    // store size and command as they might be modified when
    // forwarding the packet
    unsigned int pkt_size = pkt->hasData() ? pkt->getSize() : 0;
    unsigned int pkt_cmd = pkt->cmdToIndex();

    // a response sees the response latency
    Tick xbar_delay = responseLatency * clockPeriod();

    // set the packet header and payload delay
    calcPacketTiming(pkt, xbar_delay);

    // determine how long to be crossbar layer is busy
    Tick packetFinishTime = clockEdge(Cycles(1)) + pkt->payloadDelay;

    // send the packet through the destination CPU-side port, and pay for
    // any outstanding latency
    //Drop CMP command.
    if (pkt->cmd != MemCmd::Command::Cmp) {
        Tick latency = pkt->headerDelay;
        pkt->headerDelay = 0;
        //restore old size
        pkt->update_size(pkt->cxl_size);
        pkt->cmd = MemCmd::Command::ReadResp;
        DPRINTF(CXLController, "recvTimingResp: send to cache %s 0x%x %d\n",
             pkt->cmdString(), pkt->getAddr(), pkt->getSize());
        cpuSidePorts[cpu_side_port_id]->schedTimingResp(pkt,
                                            curTick() + latency);
    }
    // remove the request from the routing table
    routeTo.erase(route_lookup);
    //port needs to receive all data, even for cmp command.
    respLayers[cpu_side_port_id]->succeededTiming(packetFinishTime);
    // stats updates
    pktCount[cpu_side_port_id][mem_side_port_id]++;
    pktSize[cpu_side_port_id][mem_side_port_id] += pkt_size;
    transDist[pkt_cmd]++;
    if (pkt->rollover == 4){
        // stats updates
        pktCount[cpu_side_port_id][mem_side_port_id]++;
        transDist[MemCmd::Command::DataFlit]++;
    }
    //if (pkt->is_combined)
    //    transDist[MemCmd::Command::DataFlit]++;
    ////credit update
    //if (pkt->is_combined){
    //    reqLayers[mem_side_port_id]->CreditRelease(ResCrd.begin() + 2, 1);
    //} else{
    reqLayers[mem_side_port_id]->CreditRelease(ResCrd.begin() + 2,
                    5 - pkt->reserved_for_more_DRS -
                    pkt->reserved_for_more_NDR);
    //}

    //ResCrd[2] ++;

    return true;
};

Tick CXLController::recvAtomicBackdoor(PacketPtr pkt, PortID cpu_side_port_id,
                            MemBackdoorPtr *backdoor){
    unsigned int pkt_size = pkt->hasData() ? pkt->getSize() : 0;
    unsigned int pkt_cmd = pkt->cmdToIndex();

    // determine the destination port
    PortID mem_side_port_id = findPort(pkt->getAddrRange());

    // stats updates for the request
    pktCount[cpu_side_port_id][mem_side_port_id]++;
    pktSize[cpu_side_port_id][mem_side_port_id] += pkt_size;
    transDist[pkt_cmd]++;

    // forward the request to the appropriate destination
    auto mem_side_port = memSidePorts[mem_side_port_id];
    Tick response_latency = backdoor ?
        mem_side_port->sendAtomicBackdoor(pkt, *backdoor) :
        mem_side_port->sendAtomic(pkt);

    // add the response data
    if (pkt->isResponse()) {
        pkt_size = pkt->hasData() ? pkt->getSize() : 0;
        pkt_cmd = pkt->cmdToIndex();

        // stats updates
        pktCount[cpu_side_port_id][mem_side_port_id]++;
        pktSize[cpu_side_port_id][mem_side_port_id] += pkt_size;
        transDist[pkt_cmd]++;
    }

    // @todo: Not setting first-word time
    pkt->payloadDelay = response_latency;
    return response_latency;
};

void CXLController::recvFunctional(PacketPtr pkt, PortID cpu_side_port_id){
    // since our CPU-side ports are queued ports we need to check them as well
    for (const auto& p : cpuSidePorts) {
        // if we find a response that has the data, then the
        // downstream caches/memories may be out of date, so simply stop
        // here
        if (p->trySatisfyFunctional(pkt)) {
            if (pkt->needsResponse())
                pkt->makeResponse();
            return;
        }
    }

    // determine the destination port
    PortID dest_id = findPort(pkt->getAddrRange());
    memSidePorts[dest_id]->sendFunctional(pkt);
};

void CXLController::mkReadPkt(PacketPtr pkt, PortID port_id){
    //we can recieve no more than 64 response.
    ResCrd[2] -- ;

    pkt->cxl_comm = MemCmd::Command::MemRd;
    pkt->cmd = MemCmd::Command::MemRd;
    //Currently, we think the device has infinite queue
    pkt->ReqCrd = 64;
    pkt->ResCrd = 64;
    pkt->DataCrd = 64;
    pkt->rollover = 0;
    pkt->cxl_size = FLIT_SIZE;
    last_rollover = 0;
}
void CXLController::mkWritePkt(PacketPtr pkt, PortID port_id){
    //generate MemWrPtl or MemWr instruction
    //currently, we do not see any write partial
    if (pkt->isWriteback()){
        //we can recieve no more than 64 response.
        ResCrd[2] -- ;

        pkt->cxl_comm = MemCmd::Command::MemWr;
        //Currently, we think the device has infinite queue
        pkt->ReqCrd = 64;
        pkt->ResCrd = 64;
        pkt->DataCrd = 64;
        pkt->cxl_size = FLIT_SIZE;
        //check rollover for the write instruction
        pkt->rollover = (last_rollover + 4) - 3;
        last_rollover = pkt->rollover;
    }else{

    }
};

bool CXLController::QueuedReqLayer::TestOutstanding(ResponsePort* src_port){
    if (cxl_port.size() > pkt_outstanding) {
        pkt_outstanding = cxl_port.size();
        DPRINTF(CXLPerf,
                "controller sends %d pkt in flight\n", pkt_outstanding);
    }
    if (cxl_port.size() == 64) {
      // the port should not be waiting already
      assert(std::find(waitingForCredit.begin(),
                      waitingForCredit.end(),
                      src_port) == waitingForCredit.end());

      // put the port at the end of the retry list waiting for the
      // layer to be freed up (and in the case of a busy peer, for
      // that transaction to go through, and then the layer to free
      // up)
      waitingForCredit.push_back(src_port);
      return false;
    }

    return true;
};

bool CXLController::QueuedReqLayer::CreditRelease(
    std::vector<int>::iterator CrePtr, int count) {
    if (*CrePtr == 0){
        for (int i = 0; i < count && !waitingForCredit.empty(); i++)
        {
            DPRINTF(CXLController, "recvTimingReq: src %s RETRY\n",
                waitingForCredit[0]->name());
            waitingForCredit[0]->sendRetryReq();
            waitingForCredit.pop_front();
        }
    }
    (*CrePtr)+=count;
    return true;
}
void CXLController::mkClFlushPkt(PacketPtr pkt, PortID port_id) {
    ResCrd[2]--; // 使用响应额度，因为会有响应返回

    // 保存原始命令为CXL命令
    pkt->cxl_comm = MemCmd::Command::ClFlush;

    // 设置必要的信用和控制字段
    pkt->ReqCrd = 64;
    pkt->ResCrd = 64;
    pkt->DataCrd = 64;
    pkt->rollover = 0;
    pkt->cxl_size = FLIT_SIZE; // 或适当的包大小
    last_rollover = 0;
}

void CXLController::handleClFlush(PacketPtr pkt, PortID cpu_side_port_id) {
    // 确定目标内存端口
    PortID mem_side_port_id = findPort(pkt->getAddrRange());

    // 准备CXL请求包
    mkClFlushPkt(pkt, mem_side_port_id);

    // 如果请求需要响应，添加到路由表
    if (pkt->needsResponse()) {
        routeTo[pkt->req] = cpu_side_port_id;
    }

    // 计算延迟
    Tick xbar_delay = (frontendLatency + forwardLatency) * clockPeriod();
    calcPacketTiming(pkt, xbar_delay);

    // 发送请求
    Tick latency = pkt->headerDelay;
    pkt->headerDelay = 0;

    // 发送CLFLUSH请求到内存侧
    ((CXLControllerRequestPort*)memSidePorts[mem_side_port_id])
        ->schedTimingReq(pkt, curTick() + latency);

    // 统计更新
    pktCount[cpu_side_port_id][mem_side_port_id]++;
    pktSize[cpu_side_port_id][mem_side_port_id] += pkt->getSize();
    transDist[pkt->cmdToIndex()]++;
}
    void CXLController::mkMFencePkt(PacketPtr pkt, PortID port_id) {
    // 保存原始命令
    pkt->cxl_comm = MemCmd::Command::MFence;

    // 设置必要的字段
    pkt->ReqCrd = 64;
    pkt->ResCrd = 64;
    pkt->DataCrd = 64;
    pkt->cxl_size = FLIT_SIZE; // 或适当的大小

    // MFENCE 不需要数据传输
    // pkt->setPadding(true);
}

void CXLController::handleMFence(PacketPtr pkt, PortID cpu_side_port_id) {
    // 检查是否有挂起的操作
    bool hasPendingOps = false;

    for (const auto& pair : pendingOps) {
        if (!pair.second.empty()) {
            hasPendingOps = true;
            break;
        }
    }

    if (!hasPendingOps) {
        // 如果没有挂起的操作，直接完成 MFENCE
        if (pkt->needsResponse()) {
            pkt->makeResponse();
            cpuSidePorts[cpu_side_port_id]->schedTimingResp(pkt, curTick());
        } else {
            // 如果不需要响应，直接删除
            delete pkt;
        }
        return;
    }

    // 如果有挂起的操作，我们需要等待它们完成
    // 这需要一个更复杂的实现，可能需要创建一个事件或回调

    // 创建一个 MFENCE 请求并发送到所有内存端口
    PortID mem_side_port_id = findPort(pkt->getAddrRange());

    // 准备 CXL 请求包
    mkMFencePkt(pkt, mem_side_port_id);

    // 如果请求需要响应，添加到路由表
    if (pkt->needsResponse()) {
        routeTo[pkt->req] = cpu_side_port_id;
    }

    // 计算延迟
    Tick xbar_delay = (frontendLatency + forwardLatency) * clockPeriod();
    calcPacketTiming(pkt, xbar_delay);

    // 发送 MFENCE 请求到内存侧
    Tick latency = pkt->headerDelay;
    pkt->headerDelay = 0;

    ((CXLControllerRequestPort*)memSidePorts[mem_side_port_id])
        ->schedTimingReq(pkt, curTick() + latency);

    // 统计更新
    pktCount[cpu_side_port_id][mem_side_port_id]++;
    transDist[pkt->cmdToIndex()]++;
}
} // namespace gem5
