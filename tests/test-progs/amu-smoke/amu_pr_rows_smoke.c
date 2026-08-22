#include <stdint.h>
#include <string.h>

#include <immintrin.h>

#include "amu.h"
#include "gem5/m5ops.h"

static float
f32_div(float left, float right)
{
    volatile float value = left / right;
    return value;
}

static float
f32_add(float left, float right)
{
    volatile float value = left + right;
    return value;
}

static float
f32_mul(float left, float right)
{
    volatile float value = left * right;
    return value;
}

static uint32_t
bits(float value)
{
    uint32_t result;
    memcpy(&result, &value, sizeof(result));
    return result;
}

static void
flush_range(void *data, uint64_t size)
{
    uint8_t *bytes = data;
    for (uint64_t offset = 0; offset < size; offset += 64)
        _mm_clflush(bytes + offset);
}

static void
wait_for(uint64_t expected)
{
    uint64_t completed;
    while ((completed = amu_getfin()) == 0)
        m5_amu_waitfin();
    if (completed != expected)
        m5_fail(0, 20);
}

int
main(void)
{
    static uint64_t offsets[4] __attribute__((aligned(64))) = {0, 2, 3, 5};
    static int32_t neighbors[5] __attribute__((aligned(64))) = {1, 2, 0, 0, 1};
    static int64_t degrees[3] __attribute__((aligned(64))) = {2, 1, 1};
    static float scores[3] __attribute__((aligned(64))) = {0.2f, 0.3f, 0.5f};
    static float contributions[3] __attribute__((aligned(64))) = {0, 0, 0};
    static float next_scores[3] __attribute__((aligned(64))) = {0, 0, 0};
    struct pr_row_offload_desc desc = {0};

    flush_range(offsets, sizeof(offsets));
    flush_range(neighbors, sizeof(neighbors));
    flush_range(degrees, sizeof(degrees));
    flush_range(scores, sizeof(scores));
    flush_range(contributions, sizeof(contributions));
    flush_range(next_scores, sizeof(next_scores));
    _mm_mfence();

    desc.in_offsets_addr = (uintptr_t)offsets;
    desc.in_neighbors_addr = (uintptr_t)neighbors;
    desc.out_degree_addr = (uintptr_t)degrees;
    desc.scores_in_addr = (uintptr_t)scores;
    desc.contributions_addr = (uintptr_t)contributions;
    desc.scores_out_addr = (uintptr_t)next_scores;
    desc.row_begin = 0;
    desc.row_count = 1;
    desc.node_count = 3;
    desc.phase = PR_ROW_CONTRIB;
    uint64_t contribution_ids[2];
    contribution_ids[0] = amu_pr_rows(&desc);
    desc.row_begin = 1;
    desc.row_count = 2;
    contribution_ids[1] = amu_pr_rows(&desc);
    if (contribution_ids[0] == 0 || contribution_ids[1] == 0)
        m5_fail(0, 1);
    unsigned seen = 0;
    while (seen != 3) {
        const uint64_t completed = amu_getfin();
        if (completed == 0) {
            m5_amu_waitfin();
        } else if (completed == contribution_ids[0]) {
            if (seen & 1)
                m5_fail(0, 21);
            seen |= 1;
        } else if (completed == contribution_ids[1]) {
            if (seen & 2)
                m5_fail(0, 22);
            seen |= 2;
        } else {
            m5_fail(0, 23);
        }
    }

    for (uint64_t node = 0; node < 3; ++node) {
        const float expected = f32_div(scores[node], (float)degrees[node]);
        if (bits(contributions[node]) != bits(expected))
            m5_fail(0, 2 + node);
    }

    const float damping = 0.85f;
    const float base = 0.05f;
    desc.row_begin = 0;
    desc.row_count = 3;
    desc.phase = PR_ROW_PULL;
    desc.damping_bits = bits(damping);
    desc.base_score_bits = bits(base);
    uint64_t id = amu_pr_rows(&desc);
    if (id == 0)
        m5_fail(0, 10);
    wait_for(id);

    for (uint64_t row = 0; row < 3; ++row) {
        float sum = 0.0f;
        for (uint64_t edge = offsets[row]; edge < offsets[row + 1]; ++edge)
            sum = f32_add(sum, contributions[neighbors[edge]]);
        const float expected = f32_add(base, f32_mul(damping, sum));
        if (bits(next_scores[row]) != bits(expected))
            m5_fail(0, 11 + row);
    }

    m5_exit(0);
    return 0;
}
