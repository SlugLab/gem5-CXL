/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __MEM_PR_ROW_MATH_HH__
#define __MEM_PR_ROW_MATH_HH__

namespace gem5
{

static inline float
prF32Div(float left, float right)
{
    volatile float value = left / right;
    return value;
}

static inline float
prF32Add(float left, float right)
{
    volatile float value = left + right;
    return value;
}

static inline float
prF32Mul(float left, float right)
{
    volatile float value = left * right;
    return value;
}

} // namespace gem5

#endif // __MEM_PR_ROW_MATH_HH__
