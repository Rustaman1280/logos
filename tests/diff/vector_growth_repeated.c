/* C twin for vector_growth_repeated.logos. */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t values[257];
    int64_t total = 0;

    for (int64_t i = 0; i < 257; i++) {
        values[i] = (i * 7 + 3) % 31;
        total += values[i];
    }
    printf("%lld\n", (long long)257);
    printf("%lld\n", (long long)values[256]);
    printf("%lld\n", (long long)total);
    return 0;
}
