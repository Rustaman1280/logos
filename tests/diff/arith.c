/* C twin for arith.logos.
 *
 * Every operation here is one C defines: signed values stay inside int64_t's
 * range, the two unsigned operations wrap as uint64_t does, and no divisor is
 * zero or INT64_MIN over -1. That is the rule for the whole diff corpus; see
 * tests/diff/README.md.
 */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    uint64_t big = UINT64_MAX;

    printf("%lld\n", 17LL + 25LL);
    printf("%lld\n", 17LL - 25LL);
    printf("%lld\n", -6LL * 7LL);
    printf("%lld\n", 123456789LL / 1000LL);
    printf("%lld\n", -7LL / 2LL);
    printf("%lld\n", -7LL % 2LL);
    printf("%lld\n", 7LL % -2LL);
    printf("%lld\n", 9223372036854775807LL - 9223372036854775806LL);
    printf("%llu\n", (unsigned long long)(big + 2));
    printf("%llu\n", (unsigned long long)(big / 10));
    printf("%llu\n", (unsigned long long)(big % 10));
    return 3;
}
