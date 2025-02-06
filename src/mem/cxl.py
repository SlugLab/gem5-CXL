from m5.objects.XBar import *
from m5.params import *


class CXLController(BaseXBar):
    type = "CXLController"
    cxx_header = "mem/cxl_controller.hh"
    cxx_class = "gem5::CXLController"


class CXLDevice(BaseXBar):
    type = "CXLDevice"
    cxx_header = "mem/cxl_device.hh"
    cxx_class = "gem5::CXLDevice"


class CXLXBar(BaseXBar):
    type = "CXLXBar"
    cxx_header = "mem/cxlxbar.hh"
    cxx_class = "gem5::CXLXBar"
