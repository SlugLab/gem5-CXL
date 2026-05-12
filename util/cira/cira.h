/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_CIRA_CIRA_H__
#define __UTIL_CIRA_CIRA_H__

#include <stdint.h>

#include <gem5/m5ops.h>

#ifdef __cplusplus
extern "C" {
#endif

enum cira_cfg_reg
{
    CIRA_CFG_ENABLE = 0,
    CIRA_CFG_MAX_OUTSTANDING = 1,
    CIRA_CFG_RESET = 2,
    CIRA_CFG_OUTSTANDING = 2,
    CIRA_CFG_FINISHED = 3,
};

struct cira_indexed_prefetch_desc
{
    uint64_t base_addr;
    uint64_t records_addr;
    uint64_t count;
    uint64_t record_stride;
    uint64_t index_offset;
    uint64_t index_size;
    uint64_t value_size;
};

enum cira_csr_prefetch_flags
{
    CIRA_CSR_PREFETCH_RECORDS = 1ULL << 0,
    CIRA_CSR_PREFETCH_VALUES = 1ULL << 1,
    CIRA_CSR_OFFSETS_ARE_PTRS = 1ULL << 2,
    CIRA_CSR_RECORD_SPAN = 1ULL << 3,
};

struct cira_csr_prefetch_desc
{
    uint64_t offsets_addr;
    uint64_t records_addr;
    uint64_t values_addr;
    uint64_t row_start;
    uint64_t row_count;
    uint64_t offset_size;
    uint64_t record_stride;
    uint64_t index_offset;
    uint64_t index_size;
    uint64_t value_size;
    uint64_t flags;
};

static inline uint64_t
cira_prefetch(const void *addr, uint64_t size)
{
    return m5_cira_prefetch(addr, size);
}

static inline uint64_t
cira_prefetch_indexed(const struct cira_indexed_prefetch_desc *desc)
{
    const uint64_t packed_sizes =
        (desc->index_size & 0xffffffffULL) | (desc->value_size << 32);
    return m5_cira_prefetch_indexed(desc->base_addr, desc->records_addr,
                                    desc->count, desc->record_stride,
                                    desc->index_offset, packed_sizes);
}

static inline uint64_t
cira_prefetch_csr(const struct cira_csr_prefetch_desc *desc)
{
    const uint64_t packed =
        (desc->offset_size & 0xffULL) |
        ((desc->record_stride & 0xffffULL) << 8) |
        ((desc->index_offset & 0xffffULL) << 24) |
        ((desc->index_size & 0xffULL) << 40) |
        ((desc->value_size & 0xffULL) << 48) |
        ((desc->flags & 0xffULL) << 56);
    return m5_cira_prefetch_csr(desc->offsets_addr, desc->records_addr,
                                desc->values_addr, desc->row_start,
                                desc->row_count, packed);
}

static inline uint64_t
cira_getfin(void)
{
    return m5_cira_getfin();
}

static inline uint64_t
cira_cfgwr(uint64_t reg, uint64_t value)
{
    return m5_cira_cfgwr(reg, value);
}

static inline uint64_t
cira_cfgrd(uint64_t reg)
{
    return m5_cira_cfgrd(reg);
}

#ifdef __cplusplus
}
#endif

#endif // __UTIL_CIRA_CIRA_H__
