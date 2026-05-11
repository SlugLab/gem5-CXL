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

static inline uint64_t
cira_prefetch(const void *addr, uint64_t size)
{
    return m5_cira_prefetch(addr, size);
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
