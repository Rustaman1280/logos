/* C twin for shifts_unsigned.logos. */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    uint64_t high = UINT64_C(9223372036854775808);
    uint64_t all = UINT64_MAX;
    uint64_t one = UINT64_C(1);

    printf("%llu\n", (unsigned long long)(high >> 1));
    printf("%llu\n", (unsigned long long)(one << 63));
    printf("%llu\n", (unsigned long long)(all >> 60));
    return 0;
}
