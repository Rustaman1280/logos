# AGENTS.md

Logos is a prefix-form language built for AI readers, where every allocation,
fallible call and release is visible in the source. This repository holds
`logosc`, a Python standard-library front end that type-checks Logos and lowers
it to LLVM IR, plus the C runtime that clang links into a native x86_64 Linux
binary.

Read `docs/LOGOS.md` first. It is the generated manual: a cheat sheet followed
by one executable chapter per feature, and it is the ground truth for what the
language accepts.

## Done means these five commands pass

Run them from the repository root, in this order:

```sh
make test
python3 -m logos test
bash tools/accept.sh
python3 -m logos guide --check
python3 -m logos fmt $(git diff --name-only main...HEAD -- '*.logos')
```

`bash tools/pr_gate.sh` runs exactly this list, prints PASS or FAIL for each
gate, and exits non-zero if any failed. Run it before you open a pull request.
Do not weaken a gate, skip a gate, or change its expected result to make a
change pass.

## A compiler change needs a regress case

A change to the compiler is not done until a case under `tests/regress/` fails
before the fix and passes after it. A positive case is `name.logos`; a negative
case is `name.logos` plus `name.expect` containing the expected diagnostic code
(for example `E0002`). Run `python3 -m logos test --only regress` with and
without your fix and record both outcomes in the pull request.

Deleting, weakening, renaming away or otherwise neutering an existing regress
case is grounds for rejection. The corpus is the contract; do not edit it to
match a regression.

## Invariants that are not negotiable

- No accepted program may read freed or uninitialised memory.
- No accepted program may behave differently at `-O0` and `-O2`.
- No form the checker accepts may be unimplemented in the backend.
- Every diagnostic carries a code and a node path.

If a change makes one of these false, the change is wrong even if every test
passes. Add the test that would have caught it, then fix the compiler.

## Adding a language feature

Work in this order; do not start with the code.

1. Change the specification first: `docs/superpowers/specs/`.
2. Add or update a numbered chapter in `docs/manual/`.
3. Add the corpus cases, including a negative case for each new diagnostic and
   a `tests/regress/` case for the defect the feature prevents.
4. Implement the front end and backend change.
5. Regenerate the manual and prove it current:
   `python3 -m logos guide > docs/LOGOS.md`, then
   `python3 -m logos guide --check`.

## The manual is generated

`docs/LOGOS.md` is generated from the compiler's own tables and the chapter
sources in `docs/manual/`. Never edit `docs/LOGOS.md` by hand. Change the
chapter or the compiler table, regenerate, and let `python3 -m logos guide
--check` prove the committed manual is current.

## Opening the pull request

- Branch from `main` with a `feat/`, `fix/` or `docs/` prefix, for example
  `fix/vec-get-place` or `docs/threads-chapter`.
- Keep one logical change per pull request. Do not mix a compiler fix with a
  refactor or a reformat.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md` completely: the result line for
  every gate, the regress case and its pre-fix failure, the reason for any
  `tests/summary.txt` baseline change, and a statement that the four invariants
  still hold.
- Run `bash tools/pr_gate.sh` immediately before opening the pull request.
- Do not reformat files you did not otherwise change. `logosc fmt` output is
  canonical, so a formatting-only diff means your change touched more than it
  should have.

The repository accepts pull requests from people and from AI agents acting
through their operator's account. The same gates apply to both.
