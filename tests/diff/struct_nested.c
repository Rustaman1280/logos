/* C twin for struct_nested.logos. */

#include <stdint.h>
#include <stdio.h>

typedef struct {
    int16_t x;
    int16_t y;
} point;

typedef struct {
    uint8_t tag;
    point position;
    double weight;
    int32_t count;
} sample;

int main(void) {
    sample item = {7, {-3, 12}, 2.5, 42};

    printf("%u\n", (unsigned)item.tag);
    printf("%d\n", (int)item.position.x);
    printf("%d\n", (int)item.position.y);
    printf("%g\n", item.weight);
    printf("%d\n", (int)item.count);
    return 0;
}
