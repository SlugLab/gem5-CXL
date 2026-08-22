/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef PR_ROW_OFFLOAD_H
#define PR_ROW_OFFLOAD_H

#include <stdint.h>

enum pr_row_phase
{
    PR_ROW_CONTRIB = 1,
    PR_ROW_PULL = 2,
};

enum pr_row_flags
{
    PR_ROW_FLAG_SAMPLE = 1u << 0,
};

struct pr_row_offload_desc
{
    uint64_t in_offsets_addr;
    uint64_t in_neighbors_addr;
    uint64_t out_degree_addr;
    uint64_t scores_in_addr;
    uint64_t contributions_addr;
    uint64_t scores_out_addr;
    uint64_t row_begin;
    uint64_t row_count;
    uint64_t node_count;
    uint64_t iteration;
    uint32_t phase;
    uint32_t row_window;
    uint32_t lead_blocks;
    uint32_t flags;
    uint32_t damping_bits;
    uint32_t base_score_bits;
};

static inline void
pr_static_partition(uint64_t rows, uint32_t workers, uint32_t worker,
                    uint64_t *begin, uint64_t *end)
{
    const uint64_t quotient = rows / workers;
    const uint64_t remainder = rows % workers;
    *begin = worker * quotient + (worker < remainder ? worker : remainder);
    *end = *begin + quotient + (worker < remainder ? 1 : 0);
}

#ifdef __cplusplus
static_assert(sizeof(pr_row_offload_desc) == 104,
              "pr_row_offload_desc ABI changed");
#else
_Static_assert(sizeof(struct pr_row_offload_desc) == 104,
               "pr_row_offload_desc ABI changed");
#endif

#endif // PR_ROW_OFFLOAD_H
