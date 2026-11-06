/* Declarations of the Logos C runtime.
 *
 * Logos reaches these functions only through `extern`. Strings passed across
 * the boundary are pointer and length pairs, never NUL terminated.
 * The allocator, leak tracker, vec, chan, mutex and thread wrappers arrive in
 * later tasks; this header starts with the two functions the first code
 * generator needs.
 */

#ifndef LOGOS_RT_H
#define LOGOS_RT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Write exactly len bytes from buf to fd.
 * Returns the number of bytes written, or -1 on error. */
long rt_write(int fd, const void *buf, size_t len);

/* Print the node path and message to stderr and exit with status 2.
 * A panic is fatal: it never returns to the caller. */
_Noreturn void rt_panic(const char *path, const char *msg);

#ifdef __cplusplus
}
#endif

#endif /* LOGOS_RT_H */
