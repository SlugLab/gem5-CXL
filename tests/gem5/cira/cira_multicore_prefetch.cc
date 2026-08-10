/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cstddef>
#include <cstdint>
#include <sched.h>

#include <immintrin.h>
#include <gem5/m5ops.h>

#include "cira.h"

namespace
{

constexpr int NumCores = 4;
constexpr int NumRecords = 256;
constexpr int NumValues = 128;
constexpr size_t WorkerStackSize = 1U << 20;

struct Record
{
    uint32_t index;
    uint32_t padding;
};

alignas(4096) Record records[NumCores][NumRecords];
alignas(4096) uint64_t values[NumCores][NumValues];
alignas(4096) unsigned char workerStacks[NumCores - 1][WorkerStackSize];

void
drainCira()
{
    unsigned stableEmpty = 0;
    for (unsigned poll = 0; poll < 4096; ++poll) {
        while (cira_getfin() != 0) {
        }
        if (cira_cfgrd(CIRA_CFG_OUTSTANDING) == 0) {
            // The CSR walker is event-driven, so require multiple empty
            // observations before declaring the shared engine drained.
            if (++stableEmpty == 64)
                return;
        } else {
            stableEmpty = 0;
        }
    }
}

int failures = 0;
int workerDone = 0;
int workersReady = 0;
int workersGo = 0;
int workerIds[NumCores - 1] = {1, 2, 3};

void
dirtyFutureValues(int core)
{
    // Dirty another core's future values and retain them in this core's host
    // cache.  The owning core's CIRA prefetch must observe these exact bytes
    // through coherence after its device-side timing index reads complete.
    const int target = (core + 1) % NumCores;
    for (int i = 0; i < NumValues; ++i)
        values[target][i] = (target + 1) * 1000 + i;
    _mm_mfence();
}

void
issueAndCheck(int core)
{
    cira_csr_prefetch_desc csr = {};
    csr.offsets_addr = reinterpret_cast<uint64_t>(&records[core][0]);
    csr.records_addr =
        reinterpret_cast<uint64_t>(&records[core][NumRecords]);
    csr.values_addr = reinterpret_cast<uint64_t>(&values[core][0]);
    csr.row_start = core;
    csr.row_count = 1;
    csr.record_stride = sizeof(Record);
    csr.index_offset = 0;
    csr.index_size = sizeof(records[core][0].index);
    csr.value_size = sizeof(values[core][0]);
    csr.flags = CIRA_CSR_PREFETCH_RECORDS |
        CIRA_CSR_PREFETCH_VALUES | CIRA_CSR_RECORD_SPAN;
    cira_prefetch_csr(&csr);

    drainCira();

    uint64_t observed = 0;
    for (int i = 0; i < 8; ++i)
        observed += values[core][i];
    const uint64_t expected =
        8ULL * static_cast<uint64_t>((core + 1) * 1000) + 28;
    if (observed != expected)
        __atomic_fetch_add(&failures, 1, __ATOMIC_RELAXED);
}

int
runWorker(void *argument)
{
    int *core = static_cast<int *>(argument);
    dirtyFutureValues(*core);
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersGo, __ATOMIC_ACQUIRE) == 0)
        asm volatile("pause" ::: "memory");
    issueAndCheck(*core);
    __atomic_fetch_add(&workerDone, 1, __ATOMIC_RELEASE);
    return 0;
}

} // anonymous namespace

int
main()
{
    for (int core = 0; core < NumCores; ++core) {
        for (int i = 0; i < NumRecords; ++i) {
            records[core][i].index = i % 8;
            records[core][i].padding = 0;
        }
        for (int i = 0; i < NumValues; ++i)
            values[core][i] = 0;
    }

    // Force the target lines out of all private cache hierarchies so this
    // routing test exercises real CIRA timing requests instead of the
    // resident-line suppression path.
    for (size_t offset = 0; offset < sizeof(records); offset += 64)
        _mm_clflush(reinterpret_cast<unsigned char *>(records) + offset);
    for (size_t offset = 0; offset < sizeof(values); offset += 64)
        _mm_clflush(reinterpret_cast<unsigned char *>(values) + offset);
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
    // Core 0 participates in the same dirty-data barrier without blocking
    // before all clone workers have reached it.
    dirtyFutureValues(0);
    __atomic_fetch_add(&workersReady, 1, __ATOMIC_RELEASE);
    while (__atomic_load_n(&workersReady, __ATOMIC_ACQUIRE) != NumCores)
        asm volatile("pause" ::: "memory");
    __atomic_store_n(&workersGo, 1, __ATOMIC_RELEASE);
    issueAndCheck(0);
    while (__atomic_load_n(&workerDone, __ATOMIC_ACQUIRE) != NumCores - 1) {
        asm volatile("pause" ::: "memory");
    }

    if (failures != 0)
        m5_fail(0, static_cast<uint64_t>(failures));
    m5_exit(0);
    return 0;
}
