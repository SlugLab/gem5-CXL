/*
 * CIRA BFS device-JIT template.
 *
 * The host installs the resulting plan into the gem5 CIRA CSR-prefetch model.
 * In a Vortex-hosted execution this exact image is uploaded through the
 * command processor and run by the Vortex SimObject; it is not merely a
 * compiler-cache artifact. The PGO values below are compile-time constants
 * supplied by the CIRA device JIT and affect both batching and look-ahead.
 */

#include <stdint.h>

#include <vx_intrinsics.h>
#include <vx_spawn.h>

#ifndef CIRA_JIT_BATCH_SIZE
#define CIRA_JIT_BATCH_SIZE 1
#endif
#ifndef CIRA_JIT_TRAVERSAL_DEPTH
#define CIRA_JIT_TRAVERSAL_DEPTH 1
#endif
#ifndef CIRA_JIT_PIPELINE_DISTANCE
#define CIRA_JIT_PIPELINE_DISTANCE 0
#endif

typedef struct {
    uint64_t edges_addr;
    uint64_t frontier_addr;
    uint64_t parent_addr;
    uint32_t edge_count;
    uint32_t frontier_count;
} cira_bfs_args_t;

static void
bfs_prefetch_kernel(cira_bfs_args_t *__UNIFORM__ args)
{
    volatile const uint32_t *edges =
        (volatile const uint32_t *)(uintptr_t)args->edges_addr;

    // The device template only stages the edge stream.  Parent ownership and
    // frontier construction remain on the host, preserving the sequential
    // Graph500 proxy's deterministic parent tie-breaking.
    const uint32_t group = CIRA_JIT_BATCH_SIZE * CIRA_JIT_TRAVERSAL_DEPTH;
    const uint32_t first = blockIdx.x * group;
    if (first >= args->edge_count)
        return;
    const uint32_t span =
        (args->edge_count - first < group) ? args->edge_count - first : group;
    const uint32_t shift = CIRA_JIT_PIPELINE_DISTANCE % span;
    uint32_t sink = 0;
    // blockIdx.x is the scalar-task index assigned by vx_spawn_threads.
    // Each task stages one non-overlapping JIT-selected batch; the cyclic
    // shift retains the plan's pipeline-distance specialization while every
    // edge is touched exactly once across the launch.
    for (uint32_t lane = 0; lane < span; ++lane)
        sink ^= edges[first + ((lane + shift) % span)];
    if (blockIdx.x == 0 && args->parent_addr != 0)
        *(volatile uint32_t *)(uintptr_t)args->parent_addr ^= sink & 0;
    // This dispatch uses the scalar (one-task-per-work-item) spawn mode:
    // vx_spawn_threads receives a null block_dim, hence only one warp is
    // active in each group. A four-warp barrier here waits forever on a
    // correctly functioning Vortex device. There is no shared state between
    // work-items, so no synchronization is required.
}

int
main()
{
    cira_bfs_args_t *args =
        (cira_bfs_args_t *)csr_read(VX_CSR_MSCRATCH);
    const uint32_t group = CIRA_JIT_BATCH_SIZE * CIRA_JIT_TRAVERSAL_DEPTH;
    const uint32_t task_count = (args->edge_count + group - 1) / group;
    return vx_spawn_threads(1, &task_count, 0,
                            (vx_kernel_func_cb)bfs_prefetch_kernel, args);
}
