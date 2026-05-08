#include <stdint.h>

#include "gem5/m5ops.h"

extern void *m5_mem;

static uint64_t src = 0x1122334455667788ULL;
static uint64_t dst = 0;

int
main(void)
{
    m5_mem = (void *)0xffffc90000007000ULL;

    m5_amu_cfgwr_addr(0, 8);
    m5_amu_cfgwr_addr(2, 10);

    uint64_t id = m5_amu_aload_addr((void *)0x1ff0, &src);
    while (m5_amu_getfin_addr() != id) {
    }

    id = m5_amu_astore_addr((void *)0x1ff0, &dst);
    while (m5_amu_getfin_addr() != id) {
    }

    m5_exit_addr(dst == src ? 0 : 1);
    return dst == src ? 0 : 1;
}
