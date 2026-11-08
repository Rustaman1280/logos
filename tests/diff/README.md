# Differential corpus

Each `X.logos` here has a hand-written `X.c` twin. `run_diff.sh` compiles the
Logos file through the project's own driver (LLVM IR, then clang) and the twin
with gcc, runs both at `-O0` and `-O2`, and requires the same stdout bytes and
the same exit status. gcc is the oracle on purpose: a twin built by clang would
share the backend under test, so a miscompilation the two agreed on would pass.

## What a case may use

Only operations C defines. That rules out:

- signed integer overflow, which is undefined in C (Logos defines it as wrap,
  but there is no C behaviour to compare that with);
- shifts by the operand width or more;
- float-to-integer conversion outside the target's range;
- division by zero, and `INT64_MIN / -1`.

Unsigned wrap, truncating division, the sign of `%`, string operations and
fixed-array reads and writes are all defined by C and belong here. Bounds
checks, panics and allocation are Logos's own additions and are covered by the
directive corpus under `tests/exec` and `tests/mem`, not here.

## Adding a case

1. Write `X.logos` with a `main` that prints what it computes.
2. Write `X.c` doing the same with the same types and return code.
3. Add `X` on its own line in `manifest.txt`.
4. Run `bash tests/diff/run_diff.sh --verbose`.

The manifest is the corpus's floor: the script fails when a named case is
missing either half, when a pair exists that the manifest does not name, or
when the manifest lists fewer than twelve cases. Deleting a case therefore
fails the run rather than shrinking it to a vacuously green one.

The files carry no `(test ...)` node, so the directive harness under
`python3 -m logos test` ignores them and `tests/diff` contributes no cases to
`tests/summary.txt`; this script is the oracle for the directory.
