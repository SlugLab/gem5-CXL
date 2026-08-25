/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_HH__
#define __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_HH__

#include "mcfreg2_format.h"

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace mcfreg2
{

class Error : public std::runtime_error
{
  public:
    using std::runtime_error::runtime_error;
};

struct Package
{
    McfReg2Header header{};
    std::vector<McfReg2DirectoryEntry> directory;
    std::map<uint16_t, std::vector<uint8_t>> sections;
};

Package readPackage(const std::string &path);
std::string directoryJson(const Package &package);
std::string sha256Hex(std::string_view value);

} // namespace mcfreg2

#endif // __UTIL_AMU_MATCHED_WORKLOADS_MCFREG2_HH__
