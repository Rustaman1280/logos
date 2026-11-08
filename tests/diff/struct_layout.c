/* C twin for struct_layout.logos. */

#include <stdint.h>
#include <stdio.h>

typedef struct {
    uint8_t small;
    int64_t wide;
    uint16_t medium;
    double fraction;
} record;

int main(void) {
    record item = {250, INT64_C(4294967297), 513, 2.5};

    printf("%u\n", (unsigned)item.small);
    printf("%lld\n", (long long)item.wide);
    printf("%u\n", (unsigned)item.medium);
    printf("%g\n", item.fraction);
    return 0;
}
