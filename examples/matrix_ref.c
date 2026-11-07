/* Reference build for examples/matrix.logos.
 *
 * It computes the same 200x200 product with the same data and prints the same
 * four quantities. It is deliberately single-threaded: the Logos program's
 * four workers own disjoint rows, so the only thing this file has to answer is
 * whether the arithmetic and the layout agree. A second threaded copy would
 * duplicate the program under test instead of checking it.
 */

#include <stdint.h>
#include <stdio.h>

enum { SIZE = 200, AREA = SIZE * SIZE };

int main(void) {
    static int64_t left[AREA];
    static int64_t right[AREA];
    static int64_t product[AREA];
    int64_t total = 0;
    int64_t weighted = 0;

    for (int64_t i = 0; i < AREA; i++) {
        int64_t row = i / SIZE;
        int64_t column = i % SIZE;
        left[i] = (row * 7 + column * 3) % 19 - 9;
        right[i] = (row * 5 + column * 11) % 17 - 8;
    }
    for (int64_t row = 0; row < SIZE; row++) {
        for (int64_t column = 0; column < SIZE; column++) {
            int64_t cell = 0;
            for (int64_t k = 0; k < SIZE; k++) {
                cell += left[row * SIZE + k] * right[k * SIZE + column];
            }
            product[row * SIZE + column] = cell;
        }
    }
    for (int64_t i = 0; i < AREA; i++) {
        total += product[i];
        weighted += (i + 1) * product[i];
    }
    printf("sum %lld\n", (long long)total);
    printf("weighted %lld\n", (long long)weighted);
    printf("center %lld\n", (long long)product[(SIZE / 2) * SIZE + SIZE / 2]);
    printf("last %lld\n", (long long)product[AREA - 1]);
    return 0;
}
