/* C runtime for Logos. See runtime/rt.h for the contract.
 *
 * Three rules hold for every function here: write full lengths, never treat a
 * Logos string as NUL terminated, and retry short writes instead of dropping
 * bytes. A write that makes no progress is reported as an error, never
 * swallowed as a short success. Fatal conditions call rt_panic, which exits
 * with status 2.
 */

#include "rt.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

long rt_write(int fd, const void *buf, size_t len) {
    const unsigned char *p = buf;
    size_t done = 0;

    while (done < len) {
        ssize_t n = write(fd, p + done, len - done);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            /* A zero-length write would end the loop with a short count, which
             * callers must never mistake for success. */
            errno = EIO;
            return -1;
        }
        done += (size_t)n;
    }

    return (long)done;
}

_Noreturn void rt_panic(const char *path, const char *code, const char *msg) {
    static const char prefix[] = "panic: ";
    static const char sep[] = ": ";
    static const char newline[] = "\n";

    /* `code` is part of the fixed signature; rendering it into the line lands
     * with the panic tests in a later task. */
    (void)code;

    if (path != NULL) {
        (void)rt_write(STDERR_FILENO, path, strlen(path));
        (void)rt_write(STDERR_FILENO, sep, sizeof sep - 1);
    }
    (void)rt_write(STDERR_FILENO, prefix, sizeof prefix - 1);
    if (msg != NULL) {
        (void)rt_write(STDERR_FILENO, msg, strlen(msg));
    }
    (void)rt_write(STDERR_FILENO, newline, sizeof newline - 1);

    exit(2);
}
