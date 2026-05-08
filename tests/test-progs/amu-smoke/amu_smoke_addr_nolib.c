#include <stdint.h>

#define M5OP_EXIT 0x21
#define M5OP_AMU_ALOAD 0x55
#define M5OP_AMU_ASTORE 0x56
#define M5OP_AMU_GETFIN 0x57
#define M5OP_AMU_CFGWR 0x58
#define M5OP_VADDR 0xffffc90000000000ULL

static uint64_t src = 0x1122334455667788ULL;
static uint64_t dst = 0;

static inline uint64_t
m5op2(uint64_t func, uint64_t arg0, uint64_t arg1)
{
    uint64_t ret = func;
    __asm__ volatile(
        "shl $8, %%rax\n\t"
        "movabs %[base], %%r11\n\t"
        "mov (%%r11, %%rax, 1), %%rax\n\t"
        : "+a"(ret)
        : "D"(arg0), "S"(arg1), [base] "i"(M5OP_VADDR)
        : "r11", "memory");
    return ret;
}

static inline uint64_t
m5op0(uint64_t func)
{
    return m5op2(func, 0, 0);
}

static inline void
host_exit(uint64_t code)
{
    m5op2(M5OP_EXIT, code, 0);
    __asm__ volatile(
        "mov $60, %%rax\n\t"
        "syscall\n\t"
        :
        : "D"(code)
        : "rax", "rcx", "r11", "memory");
    __builtin_unreachable();
}

void
_start(void)
{
    m5op2(M5OP_AMU_CFGWR, 0, 8);
    m5op2(M5OP_AMU_CFGWR, 2, 10);

    uint64_t id = m5op2(M5OP_AMU_ALOAD, 0x1ff0, (uint64_t)&src);
    while (m5op0(M5OP_AMU_GETFIN) != id) {
    }

    id = m5op2(M5OP_AMU_ASTORE, 0x1ff0, (uint64_t)&dst);
    while (m5op0(M5OP_AMU_GETFIN) != id) {
    }

    host_exit(dst == src ? 0 : 1);
}
