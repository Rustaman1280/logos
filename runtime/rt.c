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
#include <stdint.h>
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
    static const char sep[] = ": ";
    static const char newline[] = "\n";
    static const char runtime[] = "<runtime>";
    const char *where = path != NULL ? path : runtime;

    /* One line, exactly `path: code: message`, so the node path leads and the
     * stable runtime code is machine-greppable between it and the prose. A
     * check with no node behind it still names a location: the runtime itself,
     * so the field is never empty and the line never starts with a colon. */
    (void)rt_write(STDERR_FILENO, where, strlen(where));
    (void)rt_write(STDERR_FILENO, sep, sizeof sep - 1);
    if (code != NULL) {
        (void)rt_write(STDERR_FILENO, code, strlen(code));
        (void)rt_write(STDERR_FILENO, sep, sizeof sep - 1);
    }
    if (msg != NULL) {
        (void)rt_write(STDERR_FILENO, msg, strlen(msg));
    }
    (void)rt_write(STDERR_FILENO, newline, sizeof newline - 1);

    exit(2);
}

/* ---------------------------------------------------------------------------
 * Allocation and the leak tracker
 *
 * One record per distinct address the program has allocated, in an
 * open-addressed table with linear probing. `ptr` is the key: a record with a
 * NULL key has never been used, so a probe stops there and every lookup is
 * finite. Records are never removed — a freed block keeps its record with
 * `live` clear — which is what lets the second free report where the block was
 * allocated rather than merely that it was. The table therefore holds one
 * record per address ever allocated, not per address live.
 *
 * LOGOS_DEBUG changes one thing and it is what makes the R0002 check exact:
 * rt_free marks the record dead but does not return the block to malloc, so a
 * freed address cannot be recycled into a fresh live block. A release build
 * frees normally; there a recycled address overwrites the old record and a
 * stale owner of the old block is indistinguishable from the owner of the new
 * one. See rt.h for the limit that leaves open.
 * ------------------------------------------------------------------------- */

typedef struct {
    void *ptr;          /* the key, NULL in an unused slot */
    size_t size;        /* bytes the caller asked for */
    const char *path;   /* the `alloc` node that asked for them */
    unsigned long long seq; /* allocation order, for the report */
    int live;           /* 1 while the block is allocated */
} rt_block;

static rt_block *rt_slots;
static size_t rt_cap;    /* power of two, or 0 before the first allocation */
static size_t rt_used;   /* slots holding a record */
static size_t rt_live;   /* records whose block is still allocated */
static unsigned long long rt_seq;

/* Mix an address into a bucket index. The low bits of a malloc'd address are
 * the ones worth spreading, so the multiply and the shift both use them. */
static size_t rt_hash(const void *p) {
    uintptr_t x = (uintptr_t)p;
    x ^= x >> 33;
    x *= (uintptr_t)0xff51afd7ed558ccdULL;
    x ^= x >> 33;
    return (size_t)x;
}

static void rt_place(void *ptr, size_t size, const char *path,
                     unsigned long long seq, int live) {
    size_t mask = rt_cap - 1;
    size_t i = rt_hash(ptr) & mask;

    while (rt_slots[i].ptr != NULL) {
        if (rt_slots[i].ptr == ptr) {
            break;
        }
        i = (i + 1) & mask;
    }
    if (rt_slots[i].ptr == NULL) {
        rt_used++;
    }
    rt_slots[i].ptr = ptr;
    rt_slots[i].size = size;
    rt_slots[i].path = path;
    rt_slots[i].seq = seq;
    rt_slots[i].live = live;
}

static void rt_grow(void) {
    size_t old_cap = rt_cap;
    rt_block *old = rt_slots;
    size_t cap = old_cap ? old_cap * 2 : 64;
    size_t i;

    rt_slots = calloc(cap, sizeof *rt_slots);
    if (rt_slots == NULL) {
        /* The tracker cannot record the block, so it says so rather than
         * pretending the allocation is tracked. */
        rt_panic(NULL, "R0003", "out of memory in the leak tracker");
    }
    rt_cap = cap;
    rt_used = 0;
    for (i = 0; i < old_cap; i++) {
        if (old[i].ptr != NULL) {
            rt_place(old[i].ptr, old[i].size, old[i].path, old[i].seq,
                     old[i].live);
        }
    }
    free(old);
}

static rt_block *rt_find(void *p) {
    size_t mask;
    size_t i;

    if (rt_cap == 0) {
        return NULL;
    }
    mask = rt_cap - 1;
    i = rt_hash(p) & mask;
    while (rt_slots[i].ptr != NULL) {
        if (rt_slots[i].ptr == p) {
            return &rt_slots[i];
        }
        i = (i + 1) & mask;
    }
    return NULL;
}

void *rt_alloc(size_t n, size_t align, const char *path) {
    void *p;
    rt_block *b;

    if (align == 0 || (align & (align - 1)) != 0) {
        /* Every power of two up to sixteen is all a Logos type asks for, and
         * malloc satisfies all of them; a caller-supplied odd alignment would
         * be a promise this allocator does not keep. */
        rt_panic(path, "R0003", "allocation alignment is not a power of two");
    }
    if (align > 16) {
        /* malloc guarantees max_align_t (sixteen bytes on x86-64). A wider
         * alignment would need aligned_alloc and a matching free, which is a
         * later task's problem; refusing is better than misaligning. */
        rt_panic(path, "R0003", "allocation alignment above sixteen is not supported");
    }
    if (n == 0) {
        /* malloc(0) is allowed to hand back the same non-NULL pointer every
         * time, and two live blocks must have two addresses, so a zero-byte
         * request takes one byte of storage. The record keeps the requested
         * size, 0: the leak report says `LEAK 0 bytes`, which is what the
         * program asked for. */
        p = malloc(1);
    } else {
        p = malloc(n);
    }
    if (p == NULL) {
        rt_panic(path, "R0003", "out of memory");
    }
    /* Keep the table at most three quarters full, so a probe always reaches an
     * unused slot and rt_find/rt_place terminate. */
    if (rt_cap == 0 || (rt_used + 1) * 4 > rt_cap * 3) {
        rt_grow();
    }
    b = rt_find(p);
    if (b == NULL) {
        rt_place(p, n, path, rt_seq++, 1);
        rt_live++;
    } else {
        /* Only a release build can get here: it returns freed blocks to
         * malloc, so a new block may land on an address that had a dead
         * record. The record is reused and the old size and path go with it.
         * A debug build quarantines every freed block, so reuse cannot happen
         * and a stale owner always finds the original dead record. */
        if (!b->live) {
            rt_live++;
        }
        b->size = n;
        b->path = path;
        b->seq = rt_seq++;
        b->live = 1;
    }
    return p;
}

void rt_free(void *p) {
    rt_block *b;

    if (p == NULL) {
        return;
    }
    b = rt_find(p);
    if (b == NULL) {
        /* Nothing was ever allocated here — not a block this process created,
         * and not an address it released. It is not a double free: no block
         * exists to be released twice, so it gets its own code. The runtime is
         * the only location there is to name. */
        rt_panic(NULL, "R0004", "free of a pointer that was never allocated");
    }
    if (!b->live) {
        /* The pointer has been released already. Naming the `alloc` that made
         * it is the only location left: the free site is the caller's, and the
         * block itself no longer exists. */
        rt_panic(b->path, "R0002", "double free");
    }
    b->live = 0;
    rt_live--;
#ifdef LOGOS_DEBUG
    /* Quarantine: the block stays allocated for the life of the process. The
     * address can then never be recycled into a fresh live block, which is
     * what makes a stale owner's second free find this dead record instead of
     * somebody else's live one. */
    (void)p;
#else
    free(p);
#endif
}

size_t rt_leak_count(void) {
    return rt_live;
}

/* Write a non-negative decimal count, without printf: the runtime formats its
 * own numbers everywhere else, and a leak line must not depend on a locale. */
static void rt_write_size(int fd, size_t v) {
    char digits[20];
    size_t n = 0;

    do {
        digits[n++] = (char)('0' + (int)(v % 10));
        v /= 10;
    } while (v != 0);

    while (n > 0) {
        (void)rt_write(fd, &digits[--n], 1);
    }
}

void rt_leak_report(void) {
    rt_block **live_blocks;
    size_t count = 0;
    size_t i;
    size_t j;

    static const char prefix[] = "LEAK ";
    static const char middle[] = " bytes at ";
    static const char newline[] = "\n";

    if (rt_live == 0) {
        return;
    }
    live_blocks = calloc(rt_live, sizeof *live_blocks);
    if (live_blocks == NULL) {
        /* The report is diagnostics; failing to build it must not change the
         * program's exit status or pretend the blocks are gone. */
        return;
    }
    for (i = 0; i < rt_cap; i++) {
        if (rt_slots[i].ptr != NULL && rt_slots[i].live) {
            live_blocks[count++] = &rt_slots[i];
        }
    }
    /* Allocation order, so two runs of the same program report in the same
     * order even though the addresses differ. */
    for (i = 1; i < count; i++) {
        rt_block *held = live_blocks[i];
        j = i;
        while (j > 0 && live_blocks[j - 1]->seq > held->seq) {
            live_blocks[j] = live_blocks[j - 1];
            j--;
        }
        live_blocks[j] = held;
    }
    for (i = 0; i < count; i++) {
        (void)rt_write(STDERR_FILENO, prefix, sizeof prefix - 1);
        rt_write_size(STDERR_FILENO, live_blocks[i]->size);
        (void)rt_write(STDERR_FILENO, middle, sizeof middle - 1);
        if (live_blocks[i]->path != NULL) {
            (void)rt_write(STDERR_FILENO, live_blocks[i]->path,
                           strlen(live_blocks[i]->path));
        }
        (void)rt_write(STDERR_FILENO, newline, sizeof newline - 1);
    }
    free(live_blocks);
}
