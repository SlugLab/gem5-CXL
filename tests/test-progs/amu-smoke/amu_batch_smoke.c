#include <stddef.h>
#include <stdint.h>

#include <immintrin.h>

#include "amu.h"
#include "gem5/m5ops.h"

#ifndef AMU_BATCH_SMOKE_ROUNDS
#define AMU_BATCH_SMOKE_ROUNDS 1
#endif

enum { BatchSize = 32, CacheLineSize = 64 };
enum { CompletionCapacity = 4, CompletionCountBits = 3 };
enum { CompletionTokenBits = 15 };
#define COMPLETION_TOKEN_MASK ((UINT64_C(1) << CompletionTokenBits) - 1)

struct cache_line
{
    uint64_t value;
    uint8_t padding[CacheLineSize - sizeof(uint64_t)];
} __attribute__((aligned(CacheLineSize)));

static uint64_t
wait_completion_batch(void)
{
    uint64_t packed;
    while (((packed = m5_amu_getfin_batch()) & 0x7) == 0)
        m5_amu_waitfin();
    return packed;
}

int
main(void)
{
    struct cache_line source[BatchSize];
    struct cache_line spm[BatchSize];
    uint64_t issued[BatchSize];
    uint8_t seen[BatchSize];

    for (size_t index = 0; index < BatchSize; ++index) {
        source[index].value = UINT64_C(0x1234567800000000) + index;
        spm[index].value = 0;
        _mm_clflush(&source[index]);
        _mm_clflush(&spm[index]);
    }
    _mm_mfence();

    if (amu_cfgwr(AMU_CFG_GRANULARITY, sizeof(uint64_t)) == 0 ||
        amu_cfgwr(AMU_CFG_MAX_OUTSTANDING, 256) == 0) {
        m5_fail(0, 1);
    }

    for (size_t round = 0; round < AMU_BATCH_SMOKE_ROUNDS; ++round) {
        for (size_t index = 0; index < BatchSize; ++index)
            seen[index] = 0;

        issued[0] = amu_aload(&spm[0].value, &source[0].value);
        if (issued[0] == 0)
            m5_fail(0, 2);
        const uint64_t first_packed = wait_completion_batch();
        if ((first_packed & 0x7) != 1 ||
            ((first_packed >> CompletionCountBits) &
             COMPLETION_TOKEN_MASK) !=
                (issued[0] & COMPLETION_TOKEN_MASK)) {
            m5_fail(0, 4);
        }
        seen[0] = 1;

        for (size_t index = 1; index < BatchSize; ++index) {
            issued[index] = amu_aload(&spm[index].value,
                                      &source[index].value);
            if (issued[index] == 0)
                m5_fail(0, 2);
        }

        size_t completed = 1;
        while (completed != BatchSize) {
            const uint64_t packed = wait_completion_batch();
            const uint64_t count = packed & 0x7;
            if (count > CompletionCapacity || count > BatchSize - completed)
                m5_fail(0, 3);
            for (size_t item = 0; item < count; ++item) {
                const uint64_t token =
                    (packed >> (CompletionCountBits +
                                item * CompletionTokenBits)) &
                    COMPLETION_TOKEN_MASK;
                (void)m5_sum(
                    (unsigned)token, (unsigned)round,
                    (unsigned)item, (unsigned)count, 0x42415443, 0);
                size_t owner = BatchSize;
                for (size_t index = 0; index < BatchSize; ++index) {
                    if ((issued[index] & COMPLETION_TOKEN_MASK) == token) {
                        owner = index;
                        break;
                    }
                }
                if (owner == BatchSize || seen[owner])
                    m5_fail(0, 4);
                seen[owner] = 1;
                ++completed;
            }
        }

        for (size_t index = 0; index < BatchSize; ++index) {
            if (!seen[index] || spm[index].value != source[index].value)
                m5_fail(0, 5);
        }
    }
    m5_exit(0);
    return 0;
}
