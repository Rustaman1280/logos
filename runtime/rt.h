/* Declarations of the Logos C runtime.
 *
 * Logos reaches these functions only through `extern`. Logos strings cross
 * this boundary as pointer and length pairs and are never NUL terminated.
 *
 * One deliberate exception: the arguments to rt_panic are not Logos strings.
 * The compiler emits them itself, as NUL-terminated C literals, so rt_panic
 * may and does call strlen on them; no length is passed and none is needed.
 *
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
 * Returns the number of bytes written, or -1 on error. A zero-length result
 * from the underlying write is an error, never a short success. */
long rt_write(int fd, const void *buf, size_t len);

/* Print a panic diagnostic to stderr and exit with status 2.
 * All three arguments are compiler-generated NUL-terminated C literals, never
 * Logos `:str` values: path is the node path, code is the stable diagnostic
 * code and msg is the one-sentence message.
 * All three are accepted now so later tasks can emit the code before it is
 * rendered; today the line carries the path and the message.
 * A panic is fatal: it never returns to the caller. */
_Noreturn void rt_panic(const char *path, const char *code, const char *msg);

#ifdef __cplusplus
}
#endif

#endif /* LOGOS_RT_H */
