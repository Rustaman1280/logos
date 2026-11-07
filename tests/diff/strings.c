/* C twin for strings.logos.
 *
 * A Logos :str is bytes and a length, and these operations are the C ones:
 * concatenate, take a range, count bytes, compare. The output is written as
 * bytes, never through a format that could stop at a NUL the way %s would.
 */

#include <stdio.h>
#include <string.h>

int main(void) {
    const char *left = "logos";
    const char *right = " / differential";
    char joined[64];

    snprintf(joined, sizeof joined, "%s%s", left, right);
    printf("%s\n", joined);
    printf("%.5s\n", joined);
    printf("%zu\n", strlen("é→"));
    printf("%s\n", strcmp(left, "logos") == 0 ? "true" : "false");
    printf("%s\n", strcmp(left, "logo") == 0 ? "true" : "false");
    printf("%s\n", strncmp("abcdef" + 1, "bcd", 3) == 0 ? "true" : "false");
    printf("\n");
    fputs("no newline", stdout);
    fputc('\n', stdout);
    return 0;
}
