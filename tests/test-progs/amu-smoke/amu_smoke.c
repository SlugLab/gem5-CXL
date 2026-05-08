#include <stdint.h>

#include "amu.h"
#include "gem5/m5ops.h"

int
main(void)
{
    uint64_t src = 0x123456789abcdef0ULL;
    uint64_t spm = 0;
    uint64_t dst = 0;

    amu_cfgwr(AMU_CFG_GRANULARITY, sizeof(src));
    amu_cfgwr(AMU_CFG_LATENCY_NS, 10);

    uint64_t id = amu_aload(&spm, &src);
    while (amu_getfin() != id) {
    }

    id = amu_astore(&spm, &dst);
    while (amu_getfin() != id) {
    }

    m5_exit(dst == src ? 0 : 1);
    return dst == src ? 0 : 1;
}
