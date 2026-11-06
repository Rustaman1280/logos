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

Incremental build. Task 1 provides the repository scaffold, the command line
skeleton and runtime placeholders. The compiler passes land in later tasks, so
every subcommand except `version` currently reports `not implemented`.

## Requirements

- Ubuntu 22.04, x86_64
- Python 3.14
- clang 14 and lld: `apt-get install -y clang lld`
- GNU make

## Usage

```sh
python3 -m logos version      # logos 0.1.0
python3 -m logos build FILE   # not implemented yet
python3 -m logos test FILE    # not implemented yet
```

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
