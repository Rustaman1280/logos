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
or handle is E0700. A pthread failure is R0005.
Everything else reports a diagnostic at the construct.

## Requirements

- Ubuntu 22.04, x86_64
- Python 3.10 or newer (3.10.12 ships with Ubuntu 22.04; 3.14 also works)
- clang 14 and lld: `apt-get install -y clang lld` (`lld` is unused so far;
  clang links through the system linker)
- GNU make

## Usage

```sh
python3 -m logos version                      # logos 0.1.0
python3 -m logos build examples/hello.logos   # build/hello, and build/hello.ll
./build/hello                                 # hello
python3 -m logos test FILE                    # not implemented yet
```

The IR is always kept beside the binary under `build/`: it is the artefact to
read when a compiled program misbehaves.

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
| `tests/` | `unittest` suite |
| `docs/superpowers/` | Design spec and implementation plan |

## License

Not chosen yet.
