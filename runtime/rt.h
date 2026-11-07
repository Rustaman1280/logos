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
 * It is thread safe. One mutex guards every piece of its state — the table,
 * the counters and the report's snapshot — because the alternative, a tracker
 * per thread, would have to merge them at exit anyway, and the map is a debug
 * facility whose contention is nothing beside the malloc it records. Every
 * entry point takes the lock, so a thread allocating while another frees sees
 * one consistent table.
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
 * size_t or the allocator cannot satisfy the request.
 *
 * The returned buffer is malloc'ed and NEVER released: it lives for the life
 * of the process, and it is deliberately not passed to rt_alloc, so it does
 * not appear in the leak report. That is a recorded deviation from "every
 * allocation is tracked", not an oversight — a `:str` owns nothing and has no
 * `str-free`, so the alternatives are a process-lifetime buffer or garbage the
 * tracker blames on every program that ever concatenated. Do not add a
 * str-free: there is no owner it could be called on. */
unsigned char *rt_str_concat(const unsigned char *a, size_t an,
                             const unsigned char *b, size_t bn,
                             size_t *out_len);

/* Whether two byte ranges are the same string: equal length, then equal
 * bytes. NUL bytes inside either range are data, not terminators. */
int rt_str_eq(const unsigned char *a, size_t an,
              const unsigned char *b, size_t bn);

/* Format v as decimal text in a new string and store its byte count. */
unsigned char *rt_str_from_i64(long long v, size_t *out_len);

/* ---------------------------------------------------------------------------
 * Concurrency
 *
 * Threads, mutexes, channels and atomics are pthreads underneath and opaque
 * handles above: every type in this section crosses to Logos as one `void *`
 * and is never dereferenced by generated code. They are created here and live
 * for the life of the process. There is no destroy operation in the language,
 * so these objects are NOT tracked by the leak tracker: a mutex, a channel or
 * an atomic that a program cannot release would otherwise be reported as a
 * leak no program could fix. A thread is the exception, because `join` is its
 * release: its handle is allocated with rt_alloc, so one that is never joined
 * is reported at the `spawn` that made it.
 *
 * Failures call rt_panic with R0005, `thread operation failed`, at the node
 * that asked: a thread that could not be started, a join of a handle that is
 * not a live thread (including a second join), a mutex that could not be
 * created/locked/unlocked, or a condition variable that failed while a channel
 * blocked. A non-positive channel capacity is R0003, a runtime check.
 *
 * The channel functions copy exactly `elem` bytes per element, so an element
 * type must be one with a copy; the checker refuses a vector or an owner.
 * `send` blocks while the ring is full and `recv` while it is empty.
 * ------------------------------------------------------------------------- */

typedef struct rt_thread rt_thread;
typedef struct rt_mutex rt_mutex;
typedef struct rt_chan rt_chan;
typedef struct rt_atomic rt_atomic;

/* Start `entry(arg)` on a new thread and return its handle.
 * `path` is the `spawn` node path; the handle is allocated under it, so an
 * unjoined thread is named where it was started. The runtime owns `arg` from
 * the moment the thread starts: the entry function receives it, and rt_free
 * releases it when the entry returns. On a pthread_create failure the thread
 * never starts, so rt_thread_new releases both and panics R0005. */
rt_thread *rt_thread_new(const char *path, void *(*entry)(void *), void *arg);

/* Wait for `t` to finish and release its handle.
 * Panics R0005 with `thread is not running` when `t` is NULL or is not a live
 * thread handle — which includes a second join of one already joined. */
void rt_join(rt_thread *t, const char *path);

/* A new error-checking mutex. Relocking it from the thread that holds it is an
 * error (EDEADLK) rather than a hang, and so is unlocking it from another
 * thread; both panic R0005. This is the limit of that check: a thread that
 * exits while still holding the mutex cannot be seen, so the next lock of it
 * blocks forever. The mutex is untracked and lives for the process. */
rt_mutex *rt_mutex_new(const char *path);
void rt_mutex_lock(rt_mutex *m, const char *path);
void rt_mutex_unlock(rt_mutex *m, const char *path);

/* A channel of `elem`-byte elements with room for `capacity` of them.
 * A capacity that is not positive is R0003 at `path`; a capacity whose byte
 * size would overflow is R0003 too. The ring and its buffer are untracked. */
rt_chan *rt_chan_new(size_t elem, long long capacity, const char *path);

/* Copy one element into the ring, blocking while it is full. */
void rt_chan_send(rt_chan *c, const void *value, const char *path);

/* Copy one element out of the ring, blocking while it is empty. */
void rt_chan_recv(rt_chan *c, void *out, const char *path);

/* A 64-bit atomic cell. `:u64` uses the same bits; LLVM has no signedness.
 * Untracked and process-lifetime, like a mutex. */
rt_atomic *rt_atomic_new(long long value);
long long rt_atomic_add(rt_atomic *a, long long delta);
long long rt_atomic_load(rt_atomic *a);
void rt_atomic_store(rt_atomic *a, long long value);

#ifdef __cplusplus
}
#endif

#endif /* LOGOS_RT_H */
