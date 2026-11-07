# Logos

Logos is a prefix-form language built for AI readers, with `logosc`, a compiler
that turns Logos source into native x86_64 Linux binaries.

The design goal is that a model can read and write the language without
guessing. There is no implicit conversion, no hidden allocation, no hidden
global state and no silent error swallowing. A call that allocates shows it. A
call that can fail declares it. Release is manual through `free`.

The compiler front end is Python, standard library only: lexer, parser,
resolver and type checker lower a typed syntax tree to a small flat IR, which is
printed as LLVM IR text. `clang` compiles that text and links it against the C
runtime in `runtime/`.

## Status

Logos v1 is complete for one target: x86_64 Linux. The language, the compiler
and the test harness are built, reviewed, and green.

Verified by execution on this machine:

- `python3 -m logos test` runs 184 cases across eight categories at `-O0` and
  `-O2` and compares the two optimisation levels with each other, not only with
  the expected output. 0 failures, 10 deliberate leaks, 0 baseline regressions.
- `bash tools/accept.sh` runs seven sections and all pass: the harness, a
  differential oracle against gcc, three example programs whose output matches
  `wc`, `jq` and a C reference byte for byte, formatter idempotency over 41
  files, and a compile-speed floor.
- 851 unit tests pass. `tools/verify_install.sh` installs the package into a
  throwaway prefix and builds a program from outside the repository.

The language has: integers, floats, booleans and strings; structs with C
layout; fixed arrays and growable vectors; enums with payloads; results and
optionals with `try`; generics by monomorphisation; closures confined to their
frame; manual memory with move checking and a leak tracker; threads, mutexes,
channels and atomics over pthreads; `extern` declarations for the C ABI; and
node-addressed tooling (`show`, `patch`, `fmt`) that treats one subtree as one
editable unit.

### What it refuses, on purpose

These are not gaps. Each one exists because the alternative was a program that
reads freed memory, uses an uninitialised value, or behaves differently at
`-O2` than at `-O0`:

- a vector, an owner, or any type containing one is never copied; binding it
  moves it, and a second use is E0300 or E0402;
- a value cannot be read out of a place (a vector element, an array element, a
  struct field, a dereference) when its type owns memory;
- an owner may not be stored in a struct, an array, an enum payload or another
  owner, so aggregate copies can never duplicate one;
- a binding declared outside a loop may not be moved or freed inside it,
  condition included;
- a capturing closure may not cross a call boundary; its environment lives in
  the creating frame;
- `alloc` refuses any type with an invariant (a vector, a result, an optional,
  a function value, a string, an enum tag), so nothing uninitialised is ever
  read as a typed value;
- a result that is never handled is an error, and `try` requires the exact
  error type;
- shared state must be marked `(shared T)` before it can reach another thread,
  and the marker is checked through aggregates;
- a form the checker accepts but the backend cannot emit is a bug; the backend
  never fails silently at run time.

### Known limits

- One target: x86_64 Linux. The target triple is a flag, but arm64 and Darwin
  have not been built or tested.
- Release builds do not quarantine freed blocks, so `R0002` in a release build
  is defence in depth. A stale double free cannot be constructed from Logos
  source, because the ownership rules above refuse it.
- Imports resolve at check time but only own-module functions are lowered, so a
  cross-module call is refused until linking exists. Test-local helper
  functions are refused for the same reason.
- A `:str` owns nothing: concatenation allocates a buffer that lives until the
  process exits, and it does not appear in the leak report.
- A mutex held by a thread that exited stays locked; an unjoined thread's
  allocations may or may not appear in the leak report.
- `examples/wc.logos` matches GNU `wc` through a pipe, which pads to seven
  columns; a regular-file redirect uses a different width.


## Requirements

- Ubuntu 22.04, x86_64
- Python 3.10 or newer (3.10.12 ships with Ubuntu 22.04; 3.14 also works)
- clang 14 and lld: `apt-get install -y clang lld` (`lld` is unused so far;
  clang links through the system linker)
- gcc 11 and jq 1.6 for `tools/accept.sh`: gcc builds the differential oracle's
  C twins, jq is the JSON example's reference
- GNU make

## Usage

```sh
python3 -m logos version                      # logos 0.1.0
python3 -m logos build examples/hello.logos   # build/hello, and build/hello.ll
./build/hello                                 # hello
python3 -m logos test                         # run the directive-driven corpus
python3 -m logos test --only exec --opt -O0   # one category, one optimisation level
bash tools/accept.sh                          # corpus, gcc oracle, examples, fmt
```

`logosc test` reads the `(expect ...)` directive in each `(test ...)` node and
the module-level `(expect-error E0xxx)` of a negative case, runs every case at
`-O0` and `-O2`, and writes to `tests/summary.txt` with `--update-baseline`.
The baseline records the optimisation levels it ran (`OPTS -O0 -O2`), one line
per case and the suite's leak total; a case that passed in the baseline and
fails now is a regression and exits 1, as is a full run that leaks more than
the baseline recorded at the same levels. A baseline that has no `OPTS` line
cannot be compared and is refused with a request to regenerate it, rather than
silently leaving the leak total ungated. `--update-baseline` with `--only`
merges: it rewrites the cases it ran, drops the ones the category no longer
contains, and keeps the rest. A `.logos` file outside the known categories
(`parse`, `type`, `exec`, `mem`, `thread`, `generics`, `regress`, `diff`) is
reported and fails the run rather than being skipped.

`tools/accept.sh` is the end-to-end gate: the corpus, `tests/diff/run_diff.sh`
(Logos against gcc at `-O0` and `-O2`, floored by `tests/diff/manifest.txt`),
the three example programs against `wc`, `jq` and a C reference build,
`logosc fmt` idempotency over every `.logos` file in the tree, and a generated
200-function program with structs, a generic and a thread that must compile in
under five seconds. A section that examined zero items is a failure, and the
script exits 0 only when all seven sections print PASS.

The IR is always kept beside the binary under `build/`: it is the artefact to
read when a compiled program misbehaves.

## Examples

| Program | What it does | Reference |
| --- | --- | --- |
| `examples/wc.logos` | counts lines, words and bytes from standard input | `wc -c -w -l` |
| `examples/json.logos` | parses JSON and prints a structural summary | `jq` |
| `examples/matrix.logos` | multiplies two 200x200 matrices on four threads | `examples/matrix_ref.c` |

`wc` reads through `(read-all)` and prints GNU wc's three seven-wide columns in
GNU's order: lines, words, bytes. It splits words on the same set GNU does:
ASCII space and the C-locale controls, then the UTF-8 space separators
(U+00A0, U+1680, U+2000 to U+200A, U+202F, U+205F, U+3000); U+2028 and U+2029
are line and paragraph separators, not spaces, and GNU wc does not split there
either. `json` is a recursive-descent parser over `(res :i64 json-error)` that
validates every value, including string escapes and number grammar, counts
objects, arrays, members, scalars and nesting depth, and resolves duplicate
keys the way jq does (the last member wins). `matrix` fills two matrices from a
fixed integer pattern, hands each of four threads fifty rows through
`(shared (ptr :i64))`, and prints a checksum, a weighted checksum, the centre
cell and the last cell. `examples/sample.json` is the JSON fixture the
acceptance script feeds to both the program and jq; `examples/inputs/` holds
the Unicode word-separator inputs and the duplicate-key and malformed JSON
documents the script also compares.

## Development

```sh
make test    # python3 -m unittest discover -s tests -t . -v
make fmt     # python3 -m logos fmt .
make clean   # remove build output and caches
```

## Layout

| Path | Purpose |
| --- | --- |
| `logos/` | Python compiler front end and command line interface |
| `runtime/rt.h` | Declarations of the C runtime reached through `extern` |
| `runtime/rt.c` | C runtime implementation |
| `examples/` | Three real programs, their input fixture and the matrix reference |
| `tests/` | `unittest` suite and the directive corpus |
| `tests/diff/` | Programs with hand-written C twins, compared against gcc |
| `tools/accept.sh` | The end-to-end acceptance gate |
| `docs/superpowers/` | Design spec and implementation plan |

## License

Not chosen yet.