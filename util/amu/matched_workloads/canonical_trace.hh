/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_CANONICAL_TRACE_HH__
#define __UTIL_AMU_MATCHED_WORKLOADS_CANONICAL_TRACE_HH__

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>

namespace matched_trace
{

enum class Opcode : uint16_t
{
    LOAD_U32 = 1,
    LOAD_U64 = 2,
    LOAD_F32 = 3,
    LOAD_F64 = 4,
    STORE_U32 = 5,
    STORE_U64 = 6,
    STORE_F32 = 7,
    STORE_F64 = 8,
    F32_ADD = 9,
    F32_MUL = 10,
    F32_DIV = 11,
    F64_ADD = 12,
    I64_ADD = 13,
    I64_MIN = 14,
    BARRIER = 15,
    COMMIT = 16,
    F64_MAX = 17,
    F64_MUL = 18,
    F64_SUB = 19,
    F64_DIV = 20,
    F64_SQRT = 21,
    F64_MOV = 22,
    F64_ABS = 23,
};

#pragma pack(push, 1)
struct TraceRecord
{
    uint16_t phase;
    uint16_t opcode;
    uint32_t reserved;
    uint64_t work_item;
    uint64_t sequence;
    uint64_t address;
    uint64_t operand0;
    uint64_t operand1;
    uint64_t result;
};
#pragma pack(pop)

static_assert(sizeof(TraceRecord) == 56, "trace ABI drift");

inline uint32_t
raw_bits(float value)
{
    uint32_t result;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

inline uint64_t
raw_bits(double value)
{
    uint64_t result;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

inline void
emit(std::FILE *stream, const TraceRecord &record)
{
    if (stream == nullptr || std::fwrite(&record, sizeof(record), 1, stream) != 1)
        throw std::runtime_error("canonical trace write failed");
}

} // namespace matched_trace

#endif // __UTIL_AMU_MATCHED_WORKLOADS_CANONICAL_TRACE_HH__
