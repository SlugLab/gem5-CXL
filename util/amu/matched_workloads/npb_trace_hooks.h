/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __UTIL_AMU_MATCHED_WORKLOADS_NPB_TRACE_HOOKS_H__
#define __UTIL_AMU_MATCHED_WORKLOADS_NPB_TRACE_HOOKS_H__

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void matched_phase_begin_(const int64_t *phase, const int64_t *iteration,
                          const int64_t *work_items);
void matched_phase_end_(const int64_t *phase, const int64_t *iteration);
void matched_array_image_(const int64_t *array_id,
                          const int64_t *element_bits,
                          const int64_t *logical_base, const void *data,
                          const int64_t *count);
void matched_array_image_u32_(const int64_t *array_id,
                              const int64_t *element_bits,
                              const int64_t *logical_base,
                              const int32_t *data, const int64_t *count);
void matched_array_image_f64_(const int64_t *array_id,
                              const int64_t *element_bits,
                              const int64_t *logical_base,
                              const double *data, const int64_t *count);
void matched_invocation_(const int64_t *ordinal, const int64_t *phase,
                         const int64_t *kernel, const int64_t *iteration,
                         const int64_t *work_items,
                         const int64_t *parameters,
                         const int64_t *parameter_count);
void matched_sparse_scalar_u64_(const int64_t *scalar_id,
                                const uint64_t *raw_word);
void matched_sparse_invocation_(const int64_t *ordinal);
void matched_boundary_sha256_(const int64_t *boundary,
                              const int64_t *iteration, const void *data,
                              const int64_t *element_bits,
                              const int64_t *count);
double matched_reduce_sum4_(const double lanes[4]);
double matched_reduce_max4_(const double lanes[4]);
void matched_require_four_threads_(const int64_t *threads);
void matched_allocation_probe_(const int64_t *workload,
                               const int64_t *allocated_bytes);

#ifdef __cplusplus
}
#endif

#endif
