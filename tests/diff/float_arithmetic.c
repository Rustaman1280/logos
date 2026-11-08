/* C twin for float_arithmetic.logos. */

#include <stdio.h>

int main(void) {
    float a = 1.5f;
    float b = 2.25f;

    printf("%g\n", (double)(a + b));
    printf("%g\n", (double)(a * b));
    printf("%g\n", 7.5 / 2.5);
    printf("%g\n", 7.5 - 1.25);
    return 0;
}
