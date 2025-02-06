from m5.objects.BaseXBar import BaseXBar
from m5.params import *
from m5.proxy import *


class CXLController(BaseXBar):
    type = "CXLController"
    cxx_header = "mem/cxl_controller.hh"
    cxx_class = "gem5::CXLController"

    # Ports
    cpu_side_ports = VectorResponsePort("CPU side ports")
    mem_side_ports = VectorRequestPort("Memory side ports")

    # Parameters
    width = Param.Unsigned(16, "Bus width in bytes")
    frontend_latency = Param.Cycles(2, "Frontend latency")
    forward_latency = Param.Cycles(3, "Forward latency")
    response_latency = Param.Cycles(3, "Response latency")
