/* C twin for float_f32_rounding.logos. */

#include <stdio.h>

int main(void) {
    float base = 16777216.0f;
    float step = 1.0f;

    printf("%s\n", base + step == base ? "true" : "false");
    printf("%s\n", base - step == base ? "true" : "false");
    return 0;
}
