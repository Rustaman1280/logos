# Logos — a programming language written for AI, by AI

Design document. 2026-11-06. Status: for approval.

## Intent

Logos is a programming language whose only reader is a large language model.
A human never writes it and never reads it. Therefore the usual human
concerns do not apply: readability by people, familiar syntax, short
keystrokes for tired fingers. Instead the language optimises for the
things an AI does with code: understand a logic, change one part of it,
prove it still works, and find out why it broke.

Logos ships with its own compiler, `logosc`, which turns Logos source into
native ELF binaries. No hand translation to C at any stage.

## Goals

1. One canonical spelling for every construct. Two AIs writing the same
   logic produce byte-identical source.
2. Every definition carries its type, its contract and its tests in one
   place, so an AI reading one node needs no other file.
3. Every node is addressable by a stable path, so a change is a patch to
   one node, not a rewrite of the file.
4. The compiler checks everything it can before the program runs: types,
   mutability, moves, error handling, unused names.
5. Real binaries on Linux x86_64 in v1, produced by LLVM.
6. A test harness modelled on GCC's: directive-driven test files,
   categories, an execution oracle, stress runs and a regression baseline.

## Non-goals for v1

- Human-readable syntax, editor integration, syntax highlighting.
- Self-hosting (`logosc` is Python in v1). It is a later project.
- Garbage collection, borrow checking, ownership inference, macros,
  metaprogramming, inheritance, operator overloading.
- Targets other than Linux x86_64. The backend stays target-agnostic: the
  target triple is a compiler flag, so arm64 and Darwin remain one flag
  and one runtime build away.
- Optimisation of its own. clang optimises the emitted IR.

An anti-goal: a language that hides behaviour. Implicit conversion, hidden
allocation, hidden global state and silent error swallowing are forbidden,
because each one makes a model's reasoning wrong in a way tests may not
catch.

## Constraints

- Host: Ubuntu 22.04, x86_64, Python 3.14. No compiler is installed yet.
  clang and lld must be installed with apt.
- Compiler implementation language: Python, standard library only.
- Memory is libc malloc/free. Threads are pthreads.
- The boundary between runtime and library: allocator, strings,
  formatting, `vec`, `chan`, `mutex`, atomics and thread wrappers are
  written in C (`runtime/rt.c`) and exposed to Logos through `extern`.
  Library code written in Logos itself comes later, once raw pointers and
  extern calls are trusted.

## The language

### Form

Prefix form, fully parenthesised, one construct per node name. No sugar,
no alternative spellings, no significant whitespace. A file is one module.

```
(mod math)

(fn add :i64 ((a :i64) (b :i64))
  (ret (+ a b)))

(test add
  (expect (out "3\n") (code 0))
  (println (add 1 2)))
```

Rules:

- Every `fn` names its return type and every parameter names its type.
- Bindings are immutable unless declared `letmut`.
- A binding is visible only inside the block that declares it: a function
  body, an `if` branch, a `while` body, or a `match` arm. There is no
  conditional initialisation, so no read can reach an undefined slot.
- The compiler refuses any form it cannot lower. A checker that accepts a
  construct the backend cannot emit is a bug, not a gap.
- A definition may not take a name a form already spells: `fn` and `type`
  names are checked against the form vocabulary (`if`, `let`, `get`, `deref`,
  `alloc`, `spawn`, `ok`, the `vec-`, `str-`, `arr-` and `atomic-` families,
  and the rest), and a collision is E0003 `reserved form name`. The form wins
  the spelling, so the definition could never be called.
- Every function that returns a value ends every path in a `ret`: an `if`
  terminates only when both arms do, a `match` only when every arm does, and
  `while` with a literal `true` condition terminates unless its body can
  `break` out. `main` returning `:i64` obeys the same rule, so an exit status
  is never an accident.
- No implicit numeric conversion. `(+ 1 2.0)` is a type error.
- No statement without a node name. Nothing is implicit.
- There is one name per integer width and no alias. `:int` does not exist;
  source writes `:i64`.

### Types

Primitives: `:i8 :i16 :i32 :i64 :u8 :u16 :u32 :u64 :f32 :f64 :bool :str :void`.

Composite: `(arr T n)` fixed, `(vec T)` growable, `(ptr T)` raw,
`(own T)` owned, `(fn (T...) R)` function value, `(res T E)` result,
`(opt T)` optional, `(struct ...)` and `(enum ...)` named definitions.

`:str` is a fat pointer: 16 bytes, `{data :ptr :u8, len :u64}`. Bytes,
UTF-8, and `len` counts bytes, not characters. Comparison is byte
comparison. A string is passed to C as its data pointer plus its length,
never as a bare NUL-terminated pointer.

Generics: `(fn map :(vec U) ((a :(vec T)) (f :(fn (T) U))) ...)`. Type
variables are single capital letters. The compiler monomorphises: one
copy per concrete instantiation, and an error when instantiations pass 256.
After a type variable unifies, every type-dependent rule applies again:
a generic is never a way around a rule.

Structural equality for types. Named types are nominal. No implicit
conversion anywhere, including int to int of a different width; `cast` is
explicit and checked at compile time.

### Arithmetic, defined

Nothing here is undefined behaviour, so a C oracle can check it:

- Integer add, subtract and multiply wrap, two's complement, every width.
- Integer division truncates toward zero. Division by zero panics with the
  node path. `INT_MIN / -1` wraps to `INT_MIN`.
- Shift counts are masked to the operand width. `%` takes the sign of the
  dividend.
- Integer literals never promote to a float. `(+ 1 a)` with `a :f64` is a
  type error, exactly like `(+ 1.0 a)` with `a :i64`. Write `1.0`.
- Float to int out of range panics. It never yields a silent value.
- Float arithmetic follows IEEE 754 double precision, without fast-math.

### Memory

- `(alloc :T n)` gives `(own T)`. `(free p)` releases it. Release is
  manual: the compiler inserts no drops and no cleanup blocks. An allocated
  block is not initialised, so only a type whose every bit pattern is already
  a value may be allocated: a scalar, `:bool`, a struct or enum of those, or an
  array of those. A `(vec T)`, `(res ...)`, `(opt ...)`, `(fn ...)`, `:str` and
  any owner-containing type are E0002 `allocation of T is not supported: it has
  invariants`, because reading one uninitialised branches on a garbage tag or
  calls through a garbage pointer.
- A binding declared outside a loop may not be moved or released inside it:
  `(free p)` or any move of it inside `while` is E0303 `a binding declared
  outside a loop cannot be released or moved inside it`. The body is checked
  once, so a release on the first pass leaves the second reading freed memory.
  Release after the loop instead; a binding declared inside the body may be
  released there.
- Owning pointers move. Use after move is a compile error.
- Raw pointers exist for arrays and FFI but need an explicit `(ptr ...)`.
- In debug builds the runtime tracks every live block. At exit it prints
  `LEAK n bytes at module/fn/line` for each. `(expect-leak 0)` makes a
  leaking test fail.
- No hidden allocation. Every allocating call is visible in the source.
- A `(vec T)` is not copyable, and neither is a type that contains one.
  Binding such a value moves it; reading the source afterwards is E0300.
  Two bindings never share a buffer.
- The same holds for the two wrappers that are not places: an
  `(opt (own T))` or a `(res (own T) E)` in a binding has no copy either.
  Any transfer or construction moves the source: a `let`, a call argument, a
  `ret`, `ok`/`err`/`some`, `set-deref`, a `try` and a `match` scrutinee.
  Reading the source afterwards is E0300 and a second transfer of it is
  E0402, so two owners never exist.
- An owner lives only in a direct binding or a function parameter. It may
  not be a struct field, an array element, an enum payload, or an inner
  owner (E0002 where the type is spelled), and no expression may read one
  out of a place — `(get b p)` over an owning field, `(arr-get a i)`,
  `(vec-get v i)`, and `(deref p)` of an owning pointer are E0402 — because
  a place read copies the pointer and creates a second owner. The same rule
  covers every move-only value: reading a `(vec T)`, or any type containing
  one, out of `get`, `arr-get`, `vec-get` or `deref` is E0402, because the
  read copies the descriptor and two descriptors free one buffer twice.
- A container whose element type holds an owner at any depth is E0002, `a
  container of owners is not supported yet`, where the container is spelled:
  `(vec (own T))`, `(vec (opt (own T)))`, `(arr (own T) n)` — including
  `n = 0` — and their nested spellings. An element can be neither read nor
  released, so such a container leaks by construction and no program could
  free what it holds.
- A `(vec T)` may not be a struct field or a fixed-array element yet,
  because no place expression exists to release its buffer.
- A `:str` owns nothing. Concatenation produces a process-lifetime buffer,
  so a slice never frees and a string never appears in the leak report.

### Errors

No exceptions. A function that can fail declares it:

```
(type parse-error (enum (bad-digit :u64) (overflow)))

(fn parse-int :(res :i64 parse-error) ((s :str))
  ...)
```

The error slot names a declared enum type. Callers must handle it with
`match` or `try`. An unhandled `(res ...)` is a compile error. `try`
propagates only when the caller's error type is exactly the same type;
otherwise the caller maps it explicitly in a `match`. There is no error
widening and no union type in v1.

An enum may not contain itself, directly or through another enum: that
type has no finite size, so it is refused before any layout is computed.
An enum payload may not be an owning pointer or a vector, because payloads
are copied, not moved.

An enum is a closed set of named variants, each with no payload or one:

```
(type parse-error (enum (bad-digit :u64) (overflow)))
```

A variant is built by its bare name — `(bad-digit c)` with a value, `overflow`
without — and matched by name: `((bad-digit n) ...)` binds the payload to `n`
for that arm, `(overflow ...)` carries none. A match over an enum, a result or
an optional must name every variant, or end with `_`; a missing one is E0600.
`(res T E)` matches as `(ok v)`/`(err e)` and `(opt T)` as `(some v)`/`(none)`.
A `try` whose error type is not the enclosing function's is E0601, and a result
in statement position or in a binding nobody handles is E0602. A result may not
be stored in an aggregate yet — a struct field, an array or vector element, an
enum payload, another result or an optional — because the obligation is
discharged through a name and those slots have none: the storage is E0002 and a
result handed to `(ok ...)` or `(some ...)` is E0602.

In memory a result is `{ i1, T, E }`, an optional is `{ i1, T }` and an enum is
`{ i32, [K x i64] }` — a tag and a payload area wide enough for its widest
variant — so one type holds every variant and the tag is what a `match` tests.

### Closures

`(fn (x :T) body)` creates a function value that captures the names it
uses by value. The environment lives in the creating frame, so a closure
that captures anything may not leave that function: returning it, storing
it in a struct field, pushing it into a vector, or passing it to any
function, `fn*` parameters included, is E0502. A function type records
which kind it is: `(fn (T...) R)` is a plain function pointer,
`(fn* (T...) R)` is a capturing closure. A capturing closure may be bound
to a local name and called inside its own function. A closure that
captures nothing is a plain pointer and may go anywhere.

### Concurrency

`(spawn f args...)` starts a thread and returns a `:thread` handle. `f` is a
module function returning `:void`; its arguments are copied into one heap
record that the thread owns, so a value that owns memory (a vector, an owner)
cannot cross — E0402. A raw pointer or a synchronization handle crosses only
when its type is marked `(shared ...)` — E0700 — because two threads reaching
one object is exactly what the marker exists to show, and a `:thread` handle
is one of those objects: `join` releases it, so a handle handed to another
thread could be joined twice. The check descends into
aggregates the way the move check does: a bare `(ptr T)` in a struct field or
an array element is E0700 at the argument, while a `(shared (ptr T))` field is
already marked and stays legal. A channel's element type is the other crossing
and obeys the same rule: `(chan (ptr T) n)` and a channel of a struct holding a
bare pointer are E0700 at the element type, while `(shared (ptr T))` as the
element, or as a field of the element, stays legal. `(join t)` waits and
releases the handle; joining one twice is R0005.

`(mutex)` returns a `(shared :mutex)`; `lock`/`unlock` take it. The mutex is
error-checking, so relocking it in the thread that holds it, or unlocking it
from another thread, is R0005 rather than a hang.

`(chan T n)` returns a `(shared (chan T))`, a ring of `n` copies of T.
`(send c v)` blocks while it is full, `(recv c)` while it is empty. T must be
a type with a copy: a vector or an owner is refused at the channel type.
T must also occupy at least one byte (E0002 `a channel of an empty type is not
supported`), because the runtime copies an element into a slot the caller sized
as empty. Capacity must be positive (R0003 at the channel).

`(atomic :i64 v)` and `(atomic :u64 v)` return a `(shared (atomic T))`;
`atomic-add` returns the new value, `atomic-load` reads it, `atomic-store`
replaces it. Every operation is sequentially consistent.

The marker is spelled wherever a mutex, channel or atomic is named, and on any
pointer handed to a thread: `(shared (ptr T))` is the marked form of an
address, and `deref`/`set-deref`/`ptr-add` accept it. Only a pointer, a mutex,
a channel, an atomic or a thread handle may be shared; a scalar behind the
marker is E0002.

Data races are not prevented by the compiler in v1. Mutexes, channels and
atomics are runtime objects the language cannot destroy, so they are
process-lifetime and untracked by the leak report; a thread handle is tracked
and released by `join`, so a thread nobody joins is reported at its `spawn`.
The leak tracker itself is thread safe: one mutex guards its table, counters
and report snapshot.

Two limits a reader must know. A mutex held by a thread that exited stays
locked, so the next `lock` blocks forever. An unjoined thread's allocations
may or may not appear in the report, because the report is a snapshot taken
at exit.

`-O0` and `-O2` must agree for race-free programs only. A deliberate data
race on `(shared T)` has no defined result, so no test may pin its output.
The `(shared ...)` check descends through aggregates: a struct, array, enum
or optional that carries a bare pointer is refused as a thread argument, and
`(shared (ptr T))` is the marked spelling at any depth.

### Modules

One file, one module, file name equals module name. `(use other)` imports.
No circular imports: the resolver rejects them with the full cycle. v1 lowers
one module at a time: an imported definition is visible in the namespace
(its type can annotate a local signature) but a call to it is E0103
`imported functions are not lowered yet`, naming the function, because its
symbol lives in an object file the program is not built with.

### Tests are grammar

`(test ...)` and its directives are ordinary module-level nodes, not a
second grammar. `(test name checks...)` may hold `(expect ...)` nodes. A
test-local helper `fn` is E0002 `test-local helper functions are not lowered
yet` until a copy of one can be emitted; the refusal is at the node, not at a
call. `logosc build` compiles a file that contains
tests and drops every test node. `logosc test` compiles the same file with
tests kept and runs each one in its own process. Directive nodes:
`(expect (out S) (code N))`, `(expect-leak N)`, and module-level
`(expect-error E0xxx)` for negative cases.

Because tests are grammar, `fmt` round-trips them and the negative corpus
goes through the normal front end.

### ABI and FFI

Structs use C layout: fields in declaration order, natural alignment,
padding as C would. `:bool` is one byte. Enums are a tag plus payload and
never cross to C. `(extern fn name :ret ((p :type) ...))` declares a C
function; only C-layout types cross that boundary. File I/O, allocation,
threads and formatting all arrive this way.

### Addressing and tooling

Every node has a path built only from names and child indexes:
`module/fn-name/<index>/<index>`. A named definition inserts its own name
(`math`, `math/add`); every other node appends its position among its
parent's items, numbered from zero after the head (and after the name, for
a named definition). So the third child of `add` is
`math/add/2`, and its first child is `math/add/2/0`. Paths depend on
position alone, never on a node's shape, so editing one child never moves
another. A test node inserts `test/<name>`, so a test and a function may
share a name.

Every diagnostic prints a location: a node path when one exists, otherwise
`file:line:column`. The compiler prints that location in every diagnostic. `logosc show <path>` prints one
subtree. `logosc patch <path>` accepts a replacement subtree and rewrites
only that node, then reformats. `logosc fmt FILE...` rewrites each file to
the canonical form in place, and `logosc fmt -` prints it to stdout, so any
file an AI produces normalises to one shape.

## Compiler

Pipeline:

```
source -> lex -> parse (sexpr tree, path assigned) -> resolve names
       -> typecheck (bidirectional, local inference only for let)
       -> lower to typed IR (flat, SSA-like, explicit stack slots)
       -> emit LLVM IR text -> clang -O2 -> link runtime + libc -> ELF
```

The IR is text, in LLVM's own form. An AI can read it when debugging the
compiler itself. No LLVM bindings, no C++ toolchain beyond clang.

Diagnostics carry: path, a code (`E0421`), one sentence, and the minimal
subtree. No advice, no prose.

Runtime (`runtime/rt.c`, about 400 lines): allocator with leak tracking,
panic with path and message, string builder, integer and float formatting,
`vec`, `chan`, `mutex`, thread wrappers.

The compiler is a Python package with one entry point, runnable as
`python3 -m logos` and installed as `logosc`.

## Testing (modelled on GCC)

Categories, one directory each under `tests/`:

| Directory | Checks |
|-----------|--------|
| `parse/` | accepted and rejected grammar, `(expect-error E0xxx)` |
| `type/` | type errors, mutability, moves, error-set handling |
| `exec/` | compiled and run, stdout and exit code compared |
| `mem/` | leak balance must be zero, double free must panic |
| `thread/` | 1000-iteration stress runs, join and channel ordering |
| `generics/` | monomorphisation, instantiation limit, type errors |
| `regress/` | one case per fixed bug, never deleted |

Three extra checks:

1. **Differential oracle.** For each program in `tests/diff/`, a hand
   written C twin exists. Both are compiled and run; stdout and exit code
   must match. This checks the semantics against a mature compiler, not
   against our own beliefs. The corpus covers only operations C defines:
   C's signed overflow and out-of-range float casts are undefined and
   cannot serve as an oracle.
2. **Optimisation parity.** The whole corpus runs at `-O0` and `-O2`.
   The two runs are compared with each other as well as with the directive:
   stdout, exit status and stderr must be identical, and a disagreement fails
   the case rather than being read off the directive twice.
3. **Real programs.** `examples/wc.logos` counts bytes, words and lines,
   `examples/json.logos` parses JSON and prints a summary, and
   `examples/matrix.logos` multiplies a matrix with threads. Their output
   is compared against `wc -c -w -l`, `jq` and a reference C build.

`logosc test` prints one line per test, a summary count, and writes
`tests/summary.txt`. A run is a regression if any test that passed before
now fails, or if a new test is added without a result.

## Acceptance criteria

With no manual step:

1. `logosc build examples/hello.logos` produces an ELF that prints
   `hello` and exits 0.
2. `logosc test` passes every test in all seven categories, at `-O0` and
   `-O2`, with zero leaks reported.
3. The differential suite passes against gcc for all its cases.
4. The three example programs produce output identical to the reference
   tools.
5. A program with 200 functions, structs, generics and threads compiles
   in under 5 seconds.
6. `logosc fmt` is idempotent on every file in the repo, tests included.

## Left to the builder

- Exact node vocabulary beyond the forms named here.
- The IR shape and the lowering strategy.
- Standard library contents beyond `vec`, `str`, `chan`, `mutex`, `atomic`.
- Test count per category, and an optional fuzz generator.
- The leak tracker: a hash map keyed by pointer is enough, in my judgement.

## Decisions taken after the builder review

1. `free` is manual and the compiler inserts no drops. Leak checking is a
   runtime check in debug builds plus a test directive, never static.
2. `:str` is a 16-byte fat pointer over UTF-8 bytes, length in bytes.
3. Runtime primitives are C, reached by `extern`. A Logos-source library
   comes later.
4. Arithmetic is fully defined (see above), and the C oracle is limited to
   operations C also defines.
5. Test nodes are ordinary grammar, compiled out by `build`.
6. Error types are declared enums, and `try` needs an exact type match.
7. Structs use C layout, enums stay out of C, and `extern` is in v1.
8. `:int` is dropped in favour of `:i64`.
