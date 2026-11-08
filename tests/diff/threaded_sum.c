/* C twin for threaded_sum.logos. */

#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>

typedef struct {
    _Atomic int64_t *total;
    int64_t count;
} worker_args;

static void *add_values(void *opaque) {
    const worker_args *args = opaque;

    for (int64_t i = 0; i < args->count; i++) {
        atomic_fetch_add_explicit(args->total, 1, memory_order_relaxed);
    }
    return NULL;
}

int main(void) {
    _Atomic int64_t total = 0;
    worker_args args = {&total, 1000};
    pthread_t threads[4];

    for (size_t i = 0; i < 4; i++) {
        if (pthread_create(&threads[i], NULL, add_values, &args) != 0) {
            return 1;
        }
    }
    for (size_t i = 0; i < 4; i++) {
        if (pthread_join(threads[i], NULL) != 0) {
            return 1;
        }
    }

    printf("%lld\n", (long long)atomic_load_explicit(&total, memory_order_relaxed));
    return 0;
}
