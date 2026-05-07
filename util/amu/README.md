# X86 AMU m5ops

This directory provides a small C/C++ header for driving the AMU pseudo
instructions added to the X86 m5op path.

Build the x86 m5 helper library:

```sh
scons -C util/m5 build/x86/out/libm5.a
```

Compile benchmark code with the gem5 include path and link against `libm5.a`:

```sh
gcc -Iinclude -Iutil/amu bench.c util/m5/build/x86/out/libm5.a -o bench
```

Minimal usage:

```c
#include <stdint.h>
#include <string.h>

#include "amu.h"

int main(void)
{
    uint8_t spm[64];
    uint8_t far_mem[64];

    memset(far_mem, 0x5a, sizeof(far_mem));

    amu_cfgwr(AMU_CFG_GRANULARITY, sizeof(spm));
    uint64_t id = amu_aload(spm, far_mem);

    while (amu_getfin() != id) {
    }

    return spm[0] == 0x5a ? 0 : 1;
}
```

The current model is functional and delayed: `amu_aload` copies from memory to
SPM when its completion event fires, `amu_astore` copies from SPM to memory,
and `amu_getfin` returns completed request IDs. `AMU_CFG_GRANULARITY`,
`AMU_CFG_MAX_OUTSTANDING`, and `AMU_CFG_LATENCY_NS` configure the per-thread
model.
