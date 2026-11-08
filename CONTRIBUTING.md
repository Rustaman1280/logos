# Contributing to Logos

Logos is a prefix-form language for AI readers, compiled by `logosc` to native
x86_64 Linux binaries. `AGENTS.md` is the full contract; this page is the
shorter human route through it. Read `docs/LOGOS.md` for the language itself.

## Set up

Ubuntu 22.04, Python 3.10+, clang, lld, gcc, jq and GNU make:

```sh
sudo apt-get install -y clang lld gcc jq
make test
```

No install step is needed: `python3 -m logos` runs from the checkout.

## The five gates

```sh
make test
python3 -m logos test
bash tools/accept.sh
python3 -m logos guide --check
python3 -m logos fmt $(git diff --name-only main...HEAD -- '*.logos')
```

`bash tools/pr_gate.sh` runs all five and prints one PASS or FAIL line per
gate. A pull request must show all five passing.

To run one unit test file:

```sh
python3 -m unittest tests.test_guide -v
```

To run one test method, append it: `python3 -m unittest
tests.test_guide.ManifestTest.test_keys_are_exactly_the_documented_set -v`.
To run one corpus category at one optimisation level:

```sh
python3 -m logos test --only regress --opt -O0
```

## Compiler changes need a regress case

Add `tests/regress/name.logos`, plus `tests/regress/name.expect` with the
diagnostic code when the case is negative. It must fail before the fix and pass
after it. Do not delete or weaken an existing regress case; that is grounds for
rejection.

## The baseline

`tests/summary.txt` records every corpus case, the optimisation levels it ran
and the suite's leak total. Regenerate it only when you add a case or
intentionally change an existing one:

```sh
python3 -m logos test --update-baseline
```

Never regenerate it to hide a failure. A case that passed in the baseline and
fails now is a regression and exits 1; a higher leak total at the same
optimisation levels also exits 1. `--update-baseline --only CATEGORY` merges:
it rewrites that category and leaves the rest alone. Review
`git diff tests/summary.txt` before committing and say in the pull request why
the change is legitimate.

## The manual is generated

Never hand-edit `docs/LOGOS.md`. Edit the chapter in `docs/manual/` or the
compiler table it comes from, then:

```sh
python3 -m logos guide > docs/LOGOS.md
python3 -m logos guide --check
```

## Invariants

Every change must preserve all four:

- no accepted program reads freed or uninitialised memory;
- no accepted program behaves differently at `-O0` and `-O2`;
- no checker-accepted form is unimplemented in the backend;
- every diagnostic carries a code and a node path.

## Pull requests

Branch with a `feat/`, `fix/` or `docs/` prefix, keep one logical change per
pull request, and fill in `.github/PULL_REQUEST_TEMPLATE.md`. Run
`bash tools/pr_gate.sh` before you push and paste its result lines. Do not
include unrelated reformatting.

Bugs go through the issue form at `.github/ISSUE_TEMPLATE/bug.yml`; include the
smallest program that reproduces the problem, the exact command, the expected
output, the actual output or diagnostic code, and both `-O0` and `-O2`.

## Licence

MIT. By opening a pull request you agree to license your contribution under it.
