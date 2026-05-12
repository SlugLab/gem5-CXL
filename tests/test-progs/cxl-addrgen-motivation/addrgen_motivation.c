/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Microbenchmark for motivating CXL-side address generation.
 *
 * The modes issue remote-cacheline accesses with different address readiness:
 * known          - CPU can compute all data addresses locally.
 * indirect        - CPU must first fetch remote index metadata, then data.
 * double_indirect - CPU fetches two remote metadata levels before data.
 * chase           - next address is the data returned by the previous load.
 * chase_parallel  - multiple independent pointer chains expose more MLP.
 */

#define _POSIX_C_SOURCE 200112L

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "gem5/m5ops.h"

#define CACHE_LINE_BYTES 64
#define DEFAULT_NODES 32768ULL
#define DEFAULT_ACCESSES 4096ULL
#define DEFAULT_STREAMS 16ULL

typedef struct __attribute__((aligned(CACHE_LINE_BYTES))) {
    uint64_t value;
    uint8_t pad[CACHE_LINE_BYTES - sizeof(uint64_t)];
} LineRecord;

typedef struct __attribute__((aligned(CACHE_LINE_BYTES))) {
    uint64_t target;
    uint8_t pad[CACHE_LINE_BYTES - sizeof(uint64_t)];
} IndexRecord;

typedef enum {
    MODE_KNOWN,
    MODE_INDIRECT,
    MODE_DOUBLE_INDIRECT,
    MODE_CHASE,
    MODE_CHASE_PARALLEL,
} Mode;

typedef struct {
    Mode mode;
    uint64_t nodes;
    uint64_t accesses;
    uint64_t streams;
    uint64_t seed;
    int flush;
} Options;

static volatile uint64_t sink;

static uint64_t
parse_u64(const char *value, const char *name)
{
    char *end = NULL;
    errno = 0;
    unsigned long long parsed = strtoull(value, &end, 0);
    if (errno || end == value || *end != '\0') {
        fprintf(stderr, "invalid %s: %s\n", name, value);
        exit(2);
    }
    return (uint64_t)parsed;
}

static int
is_power_of_two(uint64_t value)
{
    return value && ((value & (value - 1)) == 0);
}

static uint64_t
mix64(uint64_t x)
{
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static void
flush_range(const void *base, size_t bytes)
{
    const char *ptr = (const char *)base;
    for (size_t offset = 0; offset < bytes; offset += CACHE_LINE_BYTES) {
        __asm__ volatile("clflush (%0)" : : "r"(ptr + offset) : "memory");
    }
    __asm__ volatile("mfence" : : : "memory");
}

static void *
aligned_zalloc(size_t bytes)
{
    void *ptr = NULL;
    if (posix_memalign(&ptr, CACHE_LINE_BYTES, bytes) != 0 || ptr == NULL) {
        perror("posix_memalign");
        exit(1);
    }
    memset(ptr, 0, bytes);
    return ptr;
}

static Mode
parse_mode(const char *value)
{
    if (!strcmp(value, "known"))
        return MODE_KNOWN;
    if (!strcmp(value, "indirect"))
        return MODE_INDIRECT;
    if (!strcmp(value, "double_indirect"))
        return MODE_DOUBLE_INDIRECT;
    if (!strcmp(value, "chase"))
        return MODE_CHASE;
    if (!strcmp(value, "chase_parallel"))
        return MODE_CHASE_PARALLEL;
    fprintf(stderr, "unknown mode: %s\n", value);
    exit(2);
}

static const char *
mode_name(Mode mode)
{
    switch (mode) {
      case MODE_KNOWN:
        return "known";
      case MODE_INDIRECT:
        return "indirect";
      case MODE_DOUBLE_INDIRECT:
        return "double_indirect";
      case MODE_CHASE:
        return "chase";
      case MODE_CHASE_PARALLEL:
        return "chase_parallel";
    }
    return "unknown";
}

static Options
parse_options(int argc, char **argv)
{
    Options opt = {
        .mode = MODE_KNOWN,
        .nodes = DEFAULT_NODES,
        .accesses = DEFAULT_ACCESSES,
        .streams = DEFAULT_STREAMS,
        .seed = 0x123456789abcdef0ULL,
        .flush = 1,
    };

    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            opt.mode = parse_mode(argv[++i]);
        } else if (!strcmp(argv[i], "--nodes") && i + 1 < argc) {
            opt.nodes = parse_u64(argv[++i], "nodes");
        } else if (!strcmp(argv[i], "--accesses") && i + 1 < argc) {
            opt.accesses = parse_u64(argv[++i], "accesses");
        } else if (!strcmp(argv[i], "--streams") && i + 1 < argc) {
            opt.streams = parse_u64(argv[++i], "streams");
        } else if (!strcmp(argv[i], "--seed") && i + 1 < argc) {
            opt.seed = parse_u64(argv[++i], "seed");
        } else if (!strcmp(argv[i], "--no-flush")) {
            opt.flush = 0;
        } else {
            fprintf(stderr,
                    "usage: %s [--mode "
                    "known|indirect|double_indirect|chase|chase_parallel] "
                    "[--nodes N] [--accesses N] [--streams N] [--seed N] "
                    "[--no-flush]\n",
                    argv[0]);
            exit(2);
        }
    }

    if (!is_power_of_two(opt.nodes)) {
        fprintf(stderr, "--nodes must be a power of two\n");
        exit(2);
    }
    if (opt.streams == 0 || opt.streams > 64) {
        fprintf(stderr, "--streams must be in [1, 64]\n");
        exit(2);
    }
    if (opt.mode == MODE_CHASE) {
        opt.streams = 1;
    }
    return opt;
}

static void
init_records(const Options *opt, LineRecord *values, IndexRecord *indices,
             IndexRecord *next)
{
    const uint64_t mask = opt->nodes - 1;
    const uint64_t step = 131071ULL;

    for (uint64_t i = 0; i < opt->nodes; ++i) {
        values[i].value = mix64(i ^ opt->seed);
        indices[i].target = mix64(i + opt->seed) & mask;
        next[i].target = (i + step) & mask;
    }
}

static void
flush_working_set(const Options *opt, LineRecord *values, IndexRecord *indices,
                  IndexRecord *next)
{
    flush_range(values, opt->nodes * sizeof(values[0]));
    flush_range(indices, opt->nodes * sizeof(indices[0]));
    flush_range(next, opt->nodes * sizeof(next[0]));
}

static uint64_t
run_known(const Options *opt, volatile LineRecord *values)
{
    uint64_t idx[64];
    uint64_t acc = 0;
    const uint64_t mask = opt->nodes - 1;

    for (uint64_t s = 0; s < opt->streams; ++s) {
        idx[s] = mix64(opt->seed + s * 0x9e3779b97f4a7c15ULL) & mask;
    }

    for (uint64_t done = 0; done < opt->accesses;) {
        for (uint64_t s = 0; s < opt->streams && done < opt->accesses;
             ++s, ++done) {
            idx[s] = (idx[s] + 131071ULL) & mask;
            acc ^= values[idx[s]].value;
        }
    }
    return acc;
}

static uint64_t
run_indirect(const Options *opt, volatile LineRecord *values,
             volatile IndexRecord *indices)
{
    uint64_t idx[64];
    uint64_t acc = 0;
    const uint64_t mask = opt->nodes - 1;

    for (uint64_t s = 0; s < opt->streams; ++s) {
        idx[s] = mix64(opt->seed + s * 0xd1b54a32d192ed03ULL) & mask;
    }

    for (uint64_t done = 0; done < opt->accesses;) {
        for (uint64_t s = 0; s < opt->streams && done < opt->accesses;
             ++s, ++done) {
            idx[s] = (idx[s] + 131071ULL) & mask;
            uint64_t target = indices[idx[s]].target;
            acc ^= values[target].value;
        }
    }
    return acc;
}

static uint64_t
run_double_indirect(const Options *opt, volatile LineRecord *values,
                    volatile IndexRecord *indices)
{
    uint64_t idx[64];
    uint64_t acc = 0;
    const uint64_t mask = opt->nodes - 1;

    for (uint64_t s = 0; s < opt->streams; ++s) {
        idx[s] = mix64(opt->seed + s * 0x8cb92ba72f3d8dd7ULL) & mask;
    }

    for (uint64_t done = 0; done < opt->accesses;) {
        for (uint64_t s = 0; s < opt->streams && done < opt->accesses;
             ++s, ++done) {
            idx[s] = (idx[s] + 131071ULL) & mask;
            uint64_t mid = indices[idx[s]].target;
            uint64_t target = indices[mid].target;
            acc ^= values[target].value;
        }
    }
    return acc;
}

static uint64_t
run_chase(const Options *opt, volatile IndexRecord *next)
{
    uint64_t pos = mix64(opt->seed) & (opt->nodes - 1);
    uint64_t acc = 0;

    for (uint64_t i = 0; i < opt->accesses; ++i) {
        pos = next[pos].target;
        acc ^= pos;
    }
    return acc;
}

static uint64_t
run_chase_parallel(const Options *opt, volatile IndexRecord *next)
{
    uint64_t pos[64];
    uint64_t acc = 0;
    const uint64_t mask = opt->nodes - 1;

    for (uint64_t s = 0; s < opt->streams; ++s) {
        pos[s] = mix64(opt->seed + s * 0x94d049bb133111ebULL) & mask;
    }

    for (uint64_t done = 0; done < opt->accesses;) {
        for (uint64_t s = 0; s < opt->streams && done < opt->accesses;
             ++s, ++done) {
            pos[s] = next[pos[s]].target;
            acc ^= pos[s];
        }
    }
    return acc;
}

int
main(int argc, char **argv)
{
    Options opt = parse_options(argc, argv);
    LineRecord *values = aligned_zalloc(opt.nodes * sizeof(values[0]));
    IndexRecord *indices = aligned_zalloc(opt.nodes * sizeof(indices[0]));
    IndexRecord *next = aligned_zalloc(opt.nodes * sizeof(next[0]));

    init_records(&opt, values, indices, next);
    if (opt.flush) {
        flush_working_set(&opt, values, indices, next);
    }

    printf("addrgen mode=%s nodes=%" PRIu64 " accesses=%" PRIu64
           " streams=%" PRIu64 " flush=%d\n",
           mode_name(opt.mode), opt.nodes, opt.accesses, opt.streams,
           opt.flush);
    fflush(stdout);

    m5_work_begin((uint64_t)opt.mode, 0);
    uint64_t acc = 0;
    switch (opt.mode) {
      case MODE_KNOWN:
        acc = run_known(&opt, values);
        break;
      case MODE_INDIRECT:
        acc = run_indirect(&opt, values, indices);
        break;
      case MODE_DOUBLE_INDIRECT:
        acc = run_double_indirect(&opt, values, indices);
        break;
      case MODE_CHASE:
        acc = run_chase(&opt, next);
        break;
      case MODE_CHASE_PARALLEL:
        acc = run_chase_parallel(&opt, next);
        break;
    }
    sink = acc;
    m5_work_end((uint64_t)opt.mode, 0);

    printf("addrgen checksum=%" PRIx64 "\n", sink);
    free(next);
    free(indices);
    free(values);
    return 0;
}
