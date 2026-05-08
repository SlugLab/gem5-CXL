#include <stdint.h>

static uint64_t src = 0x123456789abcdef0ULL;
static uint64_t spm;
static uint64_t dst;

static inline uint64_t
m5_amu_aload(void *spm_addr, const void *mem_addr)
{
    uint64_t ret;
    __asm__ volatile(
        ".byte 0x0F, 0x04\n\t"
        ".word 0x55"
        : "=a"(ret)
        : "D"(spm_addr), "S"(mem_addr)
        : "memory");
    return ret;
}

static inline uint64_t
m5_amu_astore(const void *spm_addr, void *mem_addr)
{
    uint64_t ret;
    __asm__ volatile(
        ".byte 0x0F, 0x04\n\t"
        ".word 0x56"
        : "=a"(ret)
        : "D"(spm_addr), "S"(mem_addr)
        : "memory");
    return ret;
}

static inline uint64_t
m5_amu_getfin(void)
{
    uint64_t ret;
    __asm__ volatile(
        ".byte 0x0F, 0x04\n\t"
        ".word 0x57"
        : "=a"(ret)
        :
        : "memory");
    return ret;
}

static inline uint64_t
m5_amu_cfgwr(uint64_t reg, uint64_t value)
{
    uint64_t ret;
    __asm__ volatile(
        ".byte 0x0F, 0x04\n\t"
        ".word 0x58"
        : "=a"(ret)
        : "D"(reg), "S"(value)
        : "memory");
    return ret;
}

static inline void
m5_exit(uint64_t delay)
{
    __asm__ volatile(
        ".byte 0x0F, 0x04\n\t"
        ".word 0x21"
        :
        : "D"(delay)
        : "rax", "memory");
}

static __attribute__((noreturn)) void
sys_exit(int status)
{
    __asm__ volatile(
        "movq $60, %%rax\n\t"
        "syscall"
        :
        : "D"((uint64_t)status)
        : "rax", "rcx", "r11", "memory");
    __builtin_unreachable();
}

void __attribute__((noreturn))
_start(void)
{
    m5_amu_cfgwr(0, sizeof(src));
    m5_amu_cfgwr(2, 10);

    uint64_t id = m5_amu_aload(&spm, &src);
    while (m5_amu_getfin() != id) {
    }

    id = m5_amu_astore(&spm, &dst);
    while (m5_amu_getfin() != id) {
    }

    int status = dst == src ? 0 : 1;
    m5_exit(status);
    sys_exit(status);
}
