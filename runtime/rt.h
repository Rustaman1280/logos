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

/* ---------------------------------------------------------------------------
 * Printing and formatting
 *
 * Every function here writes to standard output with rt_write, so a Logos
 * program's output is exactly the bytes it asked for. `rt_print_str` takes a
 * pointer and a length and never looks for a terminator: a Logos string is
 * bytes, and a NUL inside one is data.
 *
 * Integers are formatted by hand, in a fixed buffer, with no locale. A double
 * goes through snprintf, because the shortest text that reads back as the same
 * value is not a loop worth writing by hand; the process never calls
 * setlocale, so the C locale is the one in force. `2.0` prints as `2`, `0.1`
 * as `0.1`, `1e300` as `1e+300`, and `inf`/`nan` for the non-finite values.
 *
 * A boolean prints as `true` or `false`, because that is the spelling the
 * language has for it; its 0/1 representation never reaches the output.
 * ------------------------------------------------------------------------- */

/* Write n bytes of p to stdout. No NUL terminator is expected or added. */
void rt_print_str(const unsigned char *p, size_t n);

/* Write a signed 64-bit integer in decimal, `-` for a negative value. */
void rt_print_i64(long long v);

/* Write an unsigned 64-bit integer in decimal. A separate entry point
 * because the signed one would print the top half of the range negative. */
void rt_print_u64(unsigned long long v);

/* Write a double: the shortest decimal text that reads back as the same
 * value, with `inf`/`nan` for the non-finite ones. */
void rt_print_f64(double v);

/* Write one newline to stdout. */
void rt_println(void);

/* ---------------------------------------------------------------------------
 * Strings built by the runtime
 *
 * A Logos `:str` is a fat pointer and owns nothing: it borrows its bytes, so a
 * program has no way to release a buffer. A string the runtime builds (a
 * concatenation, a formatted number) is therefore allocated for the life of
 * the process and is deliberately NOT tracked by the leak tracker: reporting
 * it would blame every program that ever concatenated for a leak it cannot
 * fix. A block a program allocates itself still goes through rt_alloc and
 * rt_free, and is still tracked.
 *
 * `rt_str` is the buffer those builders use internally: `data` holds `len`
 * bytes in `cap` bytes of allocation.
 * ------------------------------------------------------------------------- */

typedef struct {
    unsigned char *data;
    size_t len;
    size_t cap;
} rt_str;

/* Build a new string holding a's bytes followed by b's, and store its byte
 * count in *out_len. Panics with R0003 if the two lengths do not fit a
 * size_t or the allocator cannot satisfy the request. */
unsigned char *rt_str_concat(const unsigned char *a, size_t an,
                             const unsigned char *b, size_t bn,
                             size_t *out_len);

/* Whether two byte ranges are the same string: equal length, then equal
 * bytes. NUL bytes inside either range are data, not terminators. */
int rt_str_eq(const unsigned char *a, size_t an,
              const unsigned char *b, size_t bn);

/* Format v as decimal text in a new string and store its byte count. */
unsigned char *rt_str_from_i64(long long v, size_t *out_len);

#ifdef __cplusplus
}
#endif

#endif /* LOGOS_RT_H */
