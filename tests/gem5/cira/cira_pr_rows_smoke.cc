/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <sched.h>

#include <gem5/m5ops.h>
#include <immintrin.h>

#include "cira.h"

namespace
{

constexpr int NumCores = 4;
constexpr int NumNodes = 8;
constexpr int EdgesPerNode = 2;
constexpr size_t WorkerStackSize = 1U << 20;

alignas(4096) uint64_t offsets[NumNodes + 1];
alignas(4096) int32_t neighbors[NumNodes * EdgesPerNode];
alignas(4096) int64_t degrees[NumNodes];
alignas(4096) float scores[NumNodes];
alignas(4096) float contributions[NumNodes];
alignas(4096) float nextScores[NumNodes];
alignas(4096) unsigned char workerStacks[NumCores - 1][WorkerStackSize];

int workersReady = 0;
int workersGo = 0;
int barrierCount = 0;
int barrierPhase = 0;
int workerDone = 0;
int workerIds[NumCores - 1] = {1, 2, 3};

float
f32Div(float left, float right)
{
    volatile float value = left / right;
    return value;
}

float
f32Add(float left, float right)
{
    volatile float value = left + right;
    return value;
}

float
f32Mul(float left, float right)
{
    volatile float value = left * right;
    return value;
}

uint32_t
bits(float value)
{
    uint32_t result = 0;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

void
flushRange(void *data, uint64_t size)
{
    auto *bytes = static_cast<unsigned char *>(data);
    for (uint64_t offset = 0; offset < size; offset += 64)
        _mm_clflush(bytes + offset);
}

void
barrier()
{
    const int phase = __atomic_load_n(&barrierPhase, __ATOMIC_ACQUIRE);
    if (__atomic_add_fetch(&barrierCount, 1, __ATOMIC_ACQ_REL) == NumCores) {
        __atomic_store_n(&barrierCount, 0, __ATOMIC_RELEASE);
        __atomic_store_n(&barrierPhase, phase + 1, __ATOMIC_RELEASE);
        return;
    }
    while (__atomic_load_n(&barrierPhase, __ATOMIC_ACQUIRE) == phase)
        asm volatile("pause" ::: "memory");
}

void
waitFor(uint64_t expected, uint64_t failure)
{
    for (;;) {
        const uint64_t completed = cira_getfin();
        if (completed == expected)
            return;
        if (completed != 0)
            m5_fail(0, failure);
        asm volatile("pause" ::: "memory");
    }
}

void
runPr(int core)
{
    cira_cfgwr(CIRA_CFG_PR_ROW_WINDOW, 2);
    cira_cfgwr(CIRA_CFG_PR_LEAD_BLOCKS, 1);
    const uint64_t reconfigure = cira_cfgwr(CIRA_CFG_PR_RECONFIGURE, 1);
    if (reconfigure == 0)
        m5_fail(0, 10 + core);
    if (cira_getfin() != 0)
        m5_fail(0, 20 + core);
    waitFor(reconfigure, 30 + core);

    pr_row_offload_desc desc = {};
    desc.in_offsets_addr = reinterpret_cast<uint64_t>(offsets);
    desc.in_neighbors_addr = reinterpret_cast<uint64_t>(neighbors);
    desc.out_degree_addr = reinterpret_cast<uint64_t>(degrees);
    desc.scores_in_addr = reinterpret_cast<uint64_t>(scores);
    desc.contributions_addr = reinterpret_cast<uint64_t>(contributions);
    desc.scores_out_addr = reinterpret_cast<uint64_t>(nextScores);
    desc.row_begin = core * 2;
    desc.row_count = 2;
    desc.node_count = NumNodes;
    desc.phase = PR_ROW_CONTRIB;
    const uint64_t contributionId = cira_pr_rows(&desc);
    if (contributionId == 0)
        m5_fail(0, 40 + core);
    waitFor(contributionId, 50 + core);
    barrier();

    for (uint64_t row = desc.row_begin;
         row < desc.row_begin + desc.row_count; ++row) {
        const float expected = f32Div(scores[row], float(degrees[row]));
        if (bits(contributions[row]) != bits(expected))
            m5_fail(0, 60 + row);
    }
    barrier();

    const float damping = 0.85f;
    const float base = 0.01875f;
    desc.phase = PR_ROW_PULL;
    desc.damping_bits = bits(damping);
    desc.base_score_bits = bits(base);
    const uint64_t pullId = cira_pr_rows(&desc);
    if (pullId == 0)
        m5_fail(0, 80 + core);
    waitFor(pullId, 90 + core);

    for (uint64_t row = desc.row_begin;
         row < desc.row_begin + desc.row_count; ++row) {
        float sum = 0.0f;
        for (uint64_t edge = offsets[row]; edge < offsets[row + 1]; ++edge)
            sum = f32Add(sum, contributions[neighbors[edge]]);
        const float expected = f32Add(base, f32Mul(damping, sum));
        if (bits(nextScores[row]) != bits(expected))
            m5_fail(0, 100 + row);
    }
    barrier();
}

int
runWorker(void *argument)
{
    const int core = *static_cast<int *>(argument);
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersGo, __ATOMIC_ACQUIRE) == 0)
        asm volatile("pause" ::: "memory");
    runPr(core);
    __atomic_fetch_add(&workerDone, 1, __ATOMIC_RELEASE);
    return 0;
}

} // anonymous namespace

int
main()
{
    for (int row = 0; row < NumNodes; ++row) {
        offsets[row] = row * EdgesPerNode;
        neighbors[row * EdgesPerNode] = (row + 3) % NumNodes;
        neighbors[row * EdgesPerNode + 1] = (row + 5) % NumNodes;
        degrees[row] = EdgesPerNode;
        scores[row] = 0.125f + float(row) * 0.03125f;
        contributions[row] = 0.0f;
        nextScores[row] = 0.0f;
    }
    offsets[NumNodes] = NumNodes * EdgesPerNode;
    flushRange(offsets, sizeof(offsets));
    flushRange(neighbors, sizeof(neighbors));
    flushRange(degrees, sizeof(degrees));
    flushRange(scores, sizeof(scores));
    flushRange(contributions, sizeof(contributions));
    flushRange(nextScores, sizeof(nextScores));
    _mm_mfence();

    cira_cfgwr(CIRA_CFG_MAX_OUTSTANDING, 256);
    cira_cfgwr(CIRA_CFG_ENABLE, 1);
    const int cloneFlags = CLONE_VM | CLONE_FS | CLONE_FILES |
        CLONE_SIGHAND | CLONE_THREAD | CLONE_SYSVSEM;
    for (int worker = 0; worker < NumCores - 1; ++worker) {
        const int tid = clone(
            runWorker, workerStacks[worker] + WorkerStackSize,
            cloneFlags, &workerIds[worker]);
        if (tid < 0)
            m5_fail(0, 1);
    }
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersReady, __ATOMIC_ACQUIRE) != NumCores)
        asm volatile("pause" ::: "memory");
    __atomic_store_n(&workersGo, 1, __ATOMIC_RELEASE);
    runPr(0);
    while (__atomic_load_n(&workerDone, __ATOMIC_ACQUIRE) != NumCores - 1)
        asm volatile("pause" ::: "memory");

    if (cira_cfgrd(CIRA_CFG_OUTSTANDING) != 0)
        m5_fail(0, 120);
    m5_exit(0);
    return 0;
}
