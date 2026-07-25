/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include <immintrin.h>
#include <stdint.h>

#include <gem5/m5ops.h>

_Alignas(64) static volatile uint64_t line[8];

int
main(void)
{
    const uint64_t expected = UINT64_C(0x123456789abcdef0);

    line[0] = expected;
    _mm_clflush((const void *)&line[0]);
    _mm_mfence();

    m5_work_begin(0, 0);
    volatile uint64_t value = line[0];
    _mm_mfence();
    m5_work_end(0, 0);

    return value == expected ? 0 : 1;
}
