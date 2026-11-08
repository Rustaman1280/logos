/* C twin for vector_growth.logos. */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t values[65];
    int64_t total = 0;

    for (int64_t i = 0; i < 65; i++) {
        values[i] = i * 3 + 1;
        total += values[i];
    }
    printf("%lld\n", (long long)65);
    printf("%lld\n", (long long)values[0]);
    printf("%lld\n", (long long)values[64]);
    printf("%lld\n", (long long)total);
    return 0;
}
