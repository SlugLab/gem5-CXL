#include <stdint.h>

static uint64_t line[8] __attribute__((aligned(64))) = {
    0x0123456789abcdefULL,
    0xfedcba9876543210ULL,
    0x1122334455667788ULL,
    0x8877665544332211ULL,
    0xa5a5a5a5a5a5a5a5ULL,
    0x5a5a5a5a5a5a5a5aULL,
    0x0badf00ddeadbeefULL,
    0xcafebabedeadc0deULL,
};

void
_start(void)
{
    __asm__ volatile("clflush (%0)" : : "r"(line) : "memory");
    __asm__ volatile("mfence" : : : "memory");

    uint64_t bad = 0;
    bad |= line[0] ^ 0x0123456789abcdefULL;
    bad |= line[1] ^ 0xfedcba9876543210ULL;
    bad |= line[2] ^ 0x1122334455667788ULL;
    bad |= line[3] ^ 0x8877665544332211ULL;
    bad |= line[4] ^ 0xa5a5a5a5a5a5a5a5ULL;
    bad |= line[5] ^ 0x5a5a5a5a5a5a5a5aULL;
    bad |= line[6] ^ 0x0badf00ddeadbeefULL;
    bad |= line[7] ^ 0xcafebabedeadc0deULL;

    __asm__ volatile(
        "mov $60, %%rax\n\t"
        "syscall\n\t"
        :
        : "D"(bad ? 1ULL : 0ULL)
        : "rax", "rcx", "r11", "memory");
    __builtin_unreachable();
}
