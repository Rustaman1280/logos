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

Incremental build. Tasks 1-4 provide the scaffold, the command line skeleton,
lexing, parsing, node paths, canonical printing, diagnostics, name resolution
and the type checker. Task 5 adds the first backend: IR, the LLVM text emitter
and the driver, so `build` produces a real ELF. Task 6 adds control flow —
`if`, `while`, `match`, `break`/`continue` — comparisons and boolean logic, and
lowers `:str` as the 16-byte `{ i8*, i64 }` value the spec defines, so
`print`/`println` take any string value and not only a literal. Task 7 adds
structs with C layout (`make`, `get`, `set-field`) and fixed arrays
(`make (arr T n)`, `arr-get`, `arr-set`) whose indices are bounds-checked at run
time. Task 8 adds manual memory: `(alloc T n)` gives an `(own T)`, `free`
releases it, `(ptr p)` takes a raw view, `deref`/`set-deref`/`ptr-add` read and
walk it; an owning pointer moves, so a moved binding cannot be used again, and
a debug build reports every block still live when `main` returns. Task 9 adds
strings (`str-len`, `str-concat`, `str-eq`, `str-slice`, and `=` on `:str` as
byte equality) and growable vectors (`(vec T)` with `make`, `vec-push`,
`vec-len`, `vec-get`, `vec-set`, `vec-free`), and gives `print`/`println` a
formatter for every scalar it prints: `:i64`, `:u64`, `:f64`, `:bool` and
`:str`. A narrower integer still needs a cast, because nothing in the language
converts implicitly. Task 11 adds results and optionals: `(res T E)` with
`(ok v)`/`(err e)`, `(opt T)` with `(some v)`/`(none)`, `(try e)` to propagate
an error the caller declares, error types as declared enums with payloads
(`(type parse-error (enum (bad-digit :u64) (overflow)))`), and exhaustive
`match` over an enum, a result or an optional. A missing variant is E0600, a
`try` whose error types differ is E0601, and a result nobody handles is E0602.
Task 10 adds monomorphised generics and closures that capture by value. Task 12
adds threads and the synchronization that goes with them: `(spawn f args...)`,
`(join t)`, `(mutex)`/`lock`/`unlock`, `(chan T n)`/`send`/`recv`, and
`(atomic T v)`/`atomic-add`/`atomic-load`/`atomic-store`, over pthreads. Shared
mutable state is marked `(shared ...)`, and handing a thread an unmarked pointer
or handle — or making one a channel element — is E0700. A pthread failure is R0005.
Everything else reports a diagnostic at the construct. Task 14 closes the
surface: `(read-all)` returns every byte of standard input as a `:str`, so a
program can be a filter as well as a printer, and the repository ships the
proof: three real programs under `examples/`, a differential oracle under
`tests/diff/` whose C twins are built with gcc, and `tools/accept.sh`.

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
