/*
 * Lightweight C/C++ helpers for the X86 AMU m5ops.
 */

#ifndef __UTIL_AMU_AMU_H__
#define __UTIL_AMU_AMU_H__

#include <stdint.h>

#include <gem5/m5ops.h>

#include "../pr_offload/pr_row_offload.h"

#ifdef __cplusplus
extern "C" {
#endif

enum amu_cfg_reg
{
    AMU_CFG_GRANULARITY = 0,
    AMU_CFG_MAX_OUTSTANDING = 1,
    AMU_CFG_LATENCY_NS = 2,
    /* Register 3 resets queues on cfgwr and reports outstanding on cfgrd. */
    AMU_CFG_RESET = 3,
    AMU_CFG_OUTSTANDING = 3,
    AMU_CFG_FINISHED = 4,
    AMU_CFG_FAR_SEND_QUEUE_PACKETS = 7,
    AMU_CFG_SPM_SEND_QUEUE_PACKETS = 8,
    AMU_CFG_CACHE_LINE_BYTES = 9,
};

static inline uint64_t
amu_aload(void *spm_addr, const void *mem_addr)
{
    return m5_amu_aload(spm_addr, mem_addr);
}

static inline uint64_t
amu_astore(const void *spm_addr, void *mem_addr)
{
    return m5_amu_astore(spm_addr, mem_addr);
}

static inline uint64_t
amu_pr_rows(const struct pr_row_offload_desc *desc)
{
    return m5_amu_pr_rows(desc);
}

static inline uint64_t
amu_getfin(void)
{
    return m5_amu_getfin();
}

static inline uint64_t
amu_cfgwr(uint64_t reg, uint64_t value)
{
    return m5_amu_cfgwr(reg, value);
}

static inline uint64_t
amu_cfgrd(uint64_t reg)
{
    return m5_amu_cfgrd(reg);
}

#ifdef __cplusplus
}
#endif

#endif // __UTIL_AMU_AMU_H__
