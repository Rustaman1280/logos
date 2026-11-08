/* C twin for shifts_signed.logos. All signed shifts are positive and in range. */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t one = 1;
    int64_t wide = one << 40;

    printf("%lld\n", (long long)wide);
    printf("%lld\n", (long long)(wide >> 8));
    printf("%lld\n", (long long)(7LL << 5));
    printf("%lld\n", (long long)(768LL >> 3));
    return 0;
}
