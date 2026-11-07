/* C twin for arrays.logos.
 *
 * A fixed array with C layout, written and read in index order. The bounds are
 * the language's own guarantee, so this twin only has to agree on the values,
 * not on what an out-of-range index would do.
 */

#include <stdint.h>
#include <stdio.h>

int main(void) {
    int64_t values[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t total = 0;
    int64_t largest;

    for (int64_t i = 0; i < 8; i++) {
        values[i] = i * i - 3;
    }
    largest = values[0];
    for (int64_t j = 0; j < 8; j++) {
        int64_t value = values[j];
        total += value;
        if (value > largest) {
            largest = value;
        }
    }
    printf("%lld\n", (long long)total);
    printf("%lld\n", (long long)largest);
    printf("%lld\n", (long long)values[3]);
    printf("%lld\n", (long long)values[7]);
    return 0;
}
