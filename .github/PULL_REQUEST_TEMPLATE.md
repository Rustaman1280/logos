## What changed

<!-- One logical change, and why. Link the issue if there is one. -->

## Gates

Run `bash tools/pr_gate.sh` and paste the result line for each gate. All five
must pass.

| Gate | Command | Result |
| --- | --- | --- |
| unit tests | `make test` | |
| corpus | `python3 -m logos test` | |
| acceptance | `bash tools/accept.sh` | |
| manual drift | `python3 -m logos guide --check` | |
| formatting | `python3 -m logos fmt $(git diff --name-only main...HEAD -- '*.logos')` | |

## Checklist

- [ ] A regress case was added under `tests/regress/`: path
      `________________`; it failed before the fix with
      `________________` and passes after it.
- [ ] No existing regress case was deleted, weakened, renamed away, or had its
      expectation relaxed.
- [ ] Any change to `tests/summary.txt` is legitimate because
      `________________` (a new case, or an intentionally changed one — never
      to hide a failure).
- [ ] The four invariants still hold: no accepted program reads freed or
      uninitialised memory; no accepted program behaves differently at `-O0`
      and `-O2`; no checker-accepted form is unimplemented in the backend;
      every diagnostic carries a code and a node path.
- [ ] `docs/LOGOS.md` was regenerated with `python3 -m logos guide`, not
      hand-edited.
- [ ] The branch is named `feat/…`, `fix/…` or `docs/…` and contains one
      logical change.
- [ ] There is no unrelated reformatting.
