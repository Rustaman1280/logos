/* Declarations of the Logos C runtime.
 *
 * Logos reaches these functions only through `extern`. Logos strings cross
 * this boundary as pointer and length pairs and are never NUL terminated.
 *
 * One deliberate exception: the arguments to rt_panic are not Logos strings.
 * The compiler emits them itself, as NUL-terminated C literals, so rt_panic
 * may and does call strlen on them; no length is passed and none is needed.
 * rt_alloc's third argument is the same kind of value: the node path of the
 * allocation, so the leak report can name it.
 *
 * The allocator and the leak tracker live here. `vec`, chan, mutex and thread
 * wrappers arrive in later tasks.
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
 * A panic is fatal: it never returns to the caller. */
_Noreturn void rt_panic(const char *path, const char *code, const char *msg);

/* ---------------------------------------------------------------------------
 * Allocation and the leak tracker
 *
 * The tracker is a hash map keyed by the address of every block the program
 * has ever allocated: open addressing with linear probing, one record per
 * distinct address, never removed. A record whose `live` flag is clear is a
 * block that has been freed; keeping it is what lets a second free name the
 * `alloc` that created the block instead of reporting nothing at all.
 *
 * It is a debug facility. It is not thread safe, and it is not compiled out in
 * this build: Tasks 9 and 12 reuse the same allocator, and Task 12 is where the
 * question of one tracker per process under threads is settled.
 * ------------------------------------------------------------------------- */

/* Allocate n bytes, aligned to `align` (a power of two), and record it.
 * `path` is the compiler-generated NUL-terminated node path of the `alloc`,
 * which the leak report prints. A zero-size request still returns a distinct
 * live block. Panics with R0003 if `align` is not a power of two or the
 * allocator cannot satisfy the request. */
void *rt_alloc(size_t n, size_t align, const char *path);

/* Release a block allocated by rt_alloc and mark its record dead.
 * Panics with R0002 and the message `double free` when the block has already
 * been released, naming the `alloc` path it was recorded under. A NULL pointer
 * is released silently, like free(NULL). */
void rt_free(void *p);

/* How many blocks are still live. */
size_t rt_leak_count(void);

/* Print one line per live block to stderr, in allocation order:
 *   LEAK <bytes> bytes at <path>
 * Nothing at all when every block was released. */
void rt_leak_report(void);

#ifdef __cplusplus
}
#endif

#endif /* LOGOS_RT_H */
