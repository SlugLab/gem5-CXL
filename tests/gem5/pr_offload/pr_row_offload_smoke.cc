/* Copyright (c) 2026 */
/* SPDX-License-Identifier: BSD-3-Clause */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sched.h>

#include <gem5/m5ops.h>
#include <immintrin.h>

#include "amu.h"
#include "cira.h"
#include "pr_row_offload.h"

#if (defined(PR_MODE_VANILLA) + defined(PR_MODE_AMU) + \
     defined(PR_MODE_CIRA)) != 1
#error "define exactly one PR smoke mode"
#endif

#ifndef PR_INJECT_BIT
#define PR_INJECT_BIT 0
#endif
#ifndef PR_INJECT_UNFINISHED
#define PR_INJECT_UNFINISHED 0
#endif
#ifndef PR_INJECT_QUEUE
#define PR_INJECT_QUEUE 0
#endif

namespace
{

constexpr int NumCores = 4;
constexpr int NumNodes = 6;
constexpr int NumIterations = 3;
constexpr int NumEdges = 23;
constexpr size_t WorkerStackSize = 1U << 20;
constexpr float Damping = 0.85f;
constexpr float Base = 0.025f;

alignas(4096) uint64_t offsets[NumNodes + 1] = {0, 0, 1, 3, 17, 19, 23};
alignas(4096) int32_t neighbors[NumEdges];
alignas(4096) int64_t degrees[NumNodes] = {1, 2, 3, 4, 2, 1};
alignas(4096) float scoresA[NumNodes];
alignas(4096) float scoresB[NumNodes];
alignas(4096) float contributions[NumNodes];
alignas(4096) float referenceA[NumNodes];
alignas(4096) float referenceB[NumNodes];
alignas(4096) float referenceContributions[NumNodes];
alignas(4096) unsigned char workerStacks[NumCores - 1][WorkerStackSize];

float *scoresIn = scoresA;
float *scoresOut = scoresB;
float *referenceIn = referenceA;
float *referenceOut = referenceB;
int workersReady = 0;
int workersGo = 0;
int workersDone = 0;
int barrierCount = 0;
int barrierPhase = 0;
int workerIds[NumCores - 1] = {1, 2, 3};

const char *
modeName()
{
#if defined(PR_MODE_VANILLA)
    return "vanilla";
#elif defined(PR_MODE_AMU)
    return "amu";
#else
    return "cira";
#endif
}

float f32Div(float left, float right)
{
    volatile float value = left / right;
    return value;
}

float f32Add(float left, float right)
{
    volatile float value = left + right;
    return value;
}

float f32Mul(float left, float right)
{
    volatile float value = left * right;
    return value;
}

uint32_t bits(float value)
{
    uint32_t result = 0;
    std::memcpy(&result, &value, sizeof(result));
    return result;
}

void flushRange(void *data, uint64_t size)
{
    auto *bytes = static_cast<unsigned char *>(data);
    for (uint64_t offset = 0; offset < size; offset += 64)
        _mm_clflush(bytes + offset);
    _mm_mfence();
}

void barrier()
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

void partition(int core, uint64_t &begin, uint64_t &end)
{
    const uint64_t base = NumNodes / NumCores;
    const uint64_t remainder = NumNodes % NumCores;
    begin = core * base + std::min<uint64_t>(core, remainder);
    end = begin + base + (static_cast<uint64_t>(core) < remainder);
}

uint64_t submit(const pr_row_offload_desc *desc)
{
#if defined(PR_MODE_AMU)
    return amu_pr_rows(desc);
#elif defined(PR_MODE_CIRA)
    return cira_pr_rows(desc);
#else
    (void)desc;
    return 1;
#endif
}

uint64_t getFinished()
{
#if defined(PR_MODE_AMU)
    return amu_getfin();
#elif defined(PR_MODE_CIRA)
    return cira_getfin();
#else
    return 1;
#endif
}

void waitFor(uint64_t expected, uint64_t failure)
{
#if defined(PR_MODE_VANILLA)
    (void)expected;
    (void)failure;
#else
    for (;;) {
        const uint64_t completed = getFinished();
        if (completed == expected)
            return;
        if (completed != 0)
            m5_fail(0, failure);
        asm volatile("pause" ::: "memory");
    }
#endif
}

pr_row_offload_desc descriptor(uint64_t begin, uint64_t end, int iteration,
                               uint32_t phase)
{
    pr_row_offload_desc desc = {};
    desc.in_offsets_addr = reinterpret_cast<uint64_t>(offsets);
    desc.in_neighbors_addr = reinterpret_cast<uint64_t>(neighbors);
    desc.out_degree_addr = reinterpret_cast<uint64_t>(degrees);
    desc.scores_in_addr = reinterpret_cast<uint64_t>(scoresIn);
    desc.contributions_addr = reinterpret_cast<uint64_t>(contributions);
    desc.scores_out_addr = reinterpret_cast<uint64_t>(scoresOut);
    desc.row_begin = begin;
    desc.row_count = end - begin;
    desc.node_count = NumNodes;
    desc.iteration = iteration;
    desc.phase = phase;
    desc.row_window = 2048;
    desc.lead_blocks = 32;
    desc.damping_bits = bits(Damping);
    desc.base_score_bits = bits(Base);
    return desc;
}

void executeContribution(int core, int iteration)
{
    uint64_t begin = 0, end = 0;
    partition(core, begin, end);
#if defined(PR_MODE_VANILLA)
    for (uint64_t row = begin; row < end; ++row)
        contributions[row] = f32Div(scoresIn[row], float(degrees[row]));
#else
    const auto desc = descriptor(begin, end, iteration, PR_ROW_CONTRIB);
#if PR_INJECT_QUEUE
    if (core == 0 && iteration == 0) {
        const uint64_t first = submit(&desc);
        const uint64_t rejected = submit(&desc);
        if (first == 0)
            m5_fail(0, 90);
        if (rejected == 0)
            m5_fail(0, 91);
        waitFor(first, 92);
        waitFor(rejected, 93);
        m5_fail(0, 94);
    }
#endif
    const uint64_t id = submit(&desc);
    if (id == 0)
        m5_fail(0, 100 + core);
    waitFor(id, 110 + core);
#if defined(PR_MODE_CIRA)
    flushRange(contributions + begin, (end - begin) * sizeof(float));
#endif
#endif
}

void executePull(int core, int iteration)
{
    uint64_t begin = 0, end = 0;
    partition(core, begin, end);
#if defined(PR_MODE_VANILLA)
    for (uint64_t row = begin; row < end; ++row) {
        float sum = 0.0f;
        for (uint64_t edge = offsets[row]; edge < offsets[row + 1]; ++edge)
            sum = f32Add(sum, contributions[neighbors[edge]]);
        scoresOut[row] = f32Add(Base, f32Mul(Damping, sum));
    }
#else
    const auto desc = descriptor(begin, end, iteration, PR_ROW_PULL);
    const uint64_t id = submit(&desc);
    if (id == 0)
        m5_fail(0, 120 + core);
    waitFor(id, 130 + core);
#if defined(PR_MODE_CIRA)
    flushRange(scoresOut + begin, (end - begin) * sizeof(float));
#endif
#endif
}

void updateReference()
{
    for (int row = 0; row < NumNodes; ++row)
        referenceContributions[row] =
            f32Div(referenceIn[row], float(degrees[row]));
    for (int row = 0; row < NumNodes; ++row) {
        float sum = 0.0f;
        for (uint64_t edge = offsets[row]; edge < offsets[row + 1]; ++edge)
            sum = f32Add(sum, referenceContributions[neighbors[edge]]);
        referenceOut[row] = f32Add(Base, f32Mul(Damping, sum));
    }
}

void verifyAndPrint(int iteration)
{
    updateReference();
    if (PR_INJECT_BIT && iteration == NumIterations - 1) {
        uint32_t changed = bits(scoresOut[NumNodes - 1]) ^ 1U;
        std::memcpy(&scoresOut[NumNodes - 1], &changed, sizeof(changed));
    }
    std::fprintf(stderr, "PR_ROW_ITER_BITS mode=%s iteration=%d words=",
                 modeName(), iteration);
    for (int row = 0; row < NumNodes; ++row) {
        std::fprintf(stderr, "%s%08x", row == 0 ? "" : ",",
                     bits(scoresOut[row]));
        if (bits(scoresOut[row]) != bits(referenceOut[row]))
            m5_fail(0, 140 + row);
    }
    std::fprintf(stderr, "\n");
    flushRange(scoresOut, sizeof(scoresA));
    std::swap(scoresIn, scoresOut);
    std::swap(referenceIn, referenceOut);
}

void runPr(int core)
{
    for (int iteration = 0; iteration < NumIterations; ++iteration) {
        executeContribution(core, iteration);
        barrier();
        executePull(core, iteration);
        barrier();
        if (core == 0)
            verifyAndPrint(iteration);
        barrier();
    }
}

int runWorker(void *argument)
{
    const int core = *static_cast<int *>(argument);
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersGo, __ATOMIC_ACQUIRE) == 0)
        asm volatile("pause" ::: "memory");
    runPr(core);
    __atomic_fetch_add(&workersDone, 1, __ATOMIC_RELEASE);
    return 0;
}

void initialize()
{
    for (int edge = 0; edge < NumEdges; ++edge)
        neighbors[edge] = (edge * 5 + 1) % NumNodes;
    neighbors[1] = 1;
    neighbors[2] = 1;
    for (int row = 0; row < NumNodes; ++row) {
        scoresA[row] = 0.125f + float(row) * 0.03125f;
        referenceA[row] = scoresA[row];
        scoresB[row] = referenceB[row] = 0.0f;
        contributions[row] = referenceContributions[row] = 0.0f;
    }
    flushRange(offsets, sizeof(offsets));
    flushRange(neighbors, sizeof(neighbors));
    flushRange(degrees, sizeof(degrees));
    flushRange(scoresA, sizeof(scoresA));
    flushRange(scoresB, sizeof(scoresB));
    flushRange(contributions, sizeof(contributions));
}

} // anonymous namespace

int main()
{
    initialize();
    m5_work_begin(0, 0);
#if PR_INJECT_UNFINISHED && !defined(PR_MODE_VANILLA)
    const auto desc = descriptor(0, NumNodes, 0, PR_ROW_CONTRIB);
    if (submit(&desc) == 0)
        m5_fail(0, 200);
    m5_work_end(0, 0);
    m5_exit(0);
    return 0;
#endif
    const int cloneFlags = CLONE_VM | CLONE_FS | CLONE_FILES |
        CLONE_SIGHAND | CLONE_THREAD | CLONE_SYSVSEM;
    for (int worker = 0; worker < NumCores - 1; ++worker) {
        if (clone(runWorker, workerStacks[worker] + WorkerStackSize,
                  cloneFlags, &workerIds[worker]) < 0)
            m5_fail(0, 1);
    }
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersReady, __ATOMIC_ACQUIRE) != NumCores)
        asm volatile("pause" ::: "memory");
    __atomic_store_n(&workersGo, 1, __ATOMIC_RELEASE);
    runPr(0);
    while (__atomic_load_n(&workersDone, __ATOMIC_ACQUIRE) != NumCores - 1)
        asm volatile("pause" ::: "memory");
    m5_work_end(0, 0);
    std::fprintf(stderr, "PR_ROW_VERIFY mode=%s status=PASS\n", modeName());
    m5_exit(0);
    return 0;
}
