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
 * code and msg is the one-sentence message. A NULL path prints `<runtime>`,
 * for a check whose only location is the runtime itself.
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
 *
 * What R0002 guarantees, exactly. A double free is caught when the same
 * address is released twice, and a stale owner — a copy of the pointer carried
 * in an aggregate, or past the end of a function's knowledge — is caught as
 * long as its block's address has not been recycled. A debug build makes that
 * "as long as" watertight: with LOGOS_DEBUG defined, rt_free marks the record
 * dead and never hands the memory back to malloc for the life of the process,
 * so a stale owner whose address was never reused finds the dead record, and a
 * stale owner whose block was reused cannot exist because reuse cannot happen.
 * A release build does return blocks to malloc; there a recycled address
 * overwrites the old record in rt_alloc, and a stale owner of the old block
 * then frees the new live one without a panic. Release builds are not test
 * builds: the leak report and the exact double-free check belong to debug.
 * ------------------------------------------------------------------------- */

/* Allocate n bytes, aligned to `align` (a power of two), and record it.
 * `path` is the compiler-generated NUL-terminated node path of the `alloc`,
 * which the leak report prints. The record keeps the requested `n`, including
 * zero, so a zero-byte leak is reported as `LEAK 0 bytes`; a zero-byte request
 * still returns a distinct live block. Panics with R0003 if `align` is not a
 * power of two or the allocator cannot satisfy the request. */
void *rt_alloc(size_t n, size_t align, const char *path);

/* Release a block allocated by rt_alloc and mark its record dead.
 * Panics with R0002 and the message `double free` when the block has already
 * been released, naming the `alloc` path it was recorded under. Panics with
 * R0004 and the message `free of a pointer that was never allocated` when the
 * address was never handed out by rt_alloc; both name `<runtime>` as the
 * location when there is no node path to blame. A NULL pointer is released
 * silently, like free(NULL).
 *
 * In a debug build the released block is quarantined rather than returned to
 * malloc; see the limit above. */
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
