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

#include <gem5/m5ops.h>

#include "cira.h"

namespace
{

constexpr int NumCores = 2;
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
alignas(4096) unsigned char workerStack[WorkerStackSize];

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

void
runCore(int core)
{
    cira_indexed_prefetch_desc indexed = {};
    indexed.base_addr = reinterpret_cast<uint64_t>(&values[core][0]);
    indexed.records_addr = reinterpret_cast<uint64_t>(&records[core][0]);
    indexed.count = NumRecords;
    indexed.record_stride = sizeof(Record);
    indexed.index_offset = 0;
    indexed.index_size = sizeof(records[core][0].index);
    indexed.value_size = sizeof(values[core][0]);

    // Repeated indices and repeated descriptors deliberately map many
    // candidates to the same physical cache line.
    for (int repeat = 0; repeat < 4; ++repeat)
        cira_prefetch_indexed(&indexed);

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
runWorker(void *)
{
    runCore(1);
    __atomic_store_n(&workerDone, 1, __ATOMIC_RELEASE);
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
            values[core][i] = (core + 1) * 1000 + i;
    }

    cira_cfgwr(CIRA_CFG_MAX_OUTSTANDING, 256);
    cira_cfgwr(CIRA_CFG_ENABLE, 1);

    const int cloneFlags = CLONE_VM | CLONE_FS | CLONE_FILES |
        CLONE_SIGHAND | CLONE_THREAD | CLONE_SYSVSEM;
    const int worker = clone(
        runWorker, workerStack + WorkerStackSize, cloneFlags, nullptr);
    if (worker < 0)
        m5_fail(0, 1);
    runCore(0);
    while (__atomic_load_n(&workerDone, __ATOMIC_ACQUIRE) == 0) {
        asm volatile("pause" ::: "memory");
    }

    if (failures != 0)
        m5_fail(0, static_cast<uint64_t>(failures));
    m5_exit(0);
    return 0;
}
