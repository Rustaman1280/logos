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
and the driver, so `build` produces a real ELF for the part of the language the
lowerer covers — integer literals, `let`, `ret`, calls and `print`/`println`.
Everything else reports `not implemented` or a diagnostic at the construct.

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
