#!/usr/bin/env bash
# The differential oracle: every X.logos in tests/diff has a hand-written X.c.
#
# The Logos side is compiled by the project's own driver, which emits LLVM IR
# and hands it to clang. The C side is compiled by gcc, deliberately not by the
# same compiler family: an oracle that shares clang's optimiser would agree
# with a miscompilation that clang and the oracle both make. Both programs are
# run at -O0 and -O2, and both levels must print the same bytes and leave the
# same exit status as the C twin.
#
# The corpus only uses operations C defines. Signed overflow, shifting by the
# width or more, and float-to-integer conversion out of range are undefined in
# C, so no case may depend on them; a case that needs to pin Logos's defined
# wrap would be testing C's absence of a guarantee, not C's behaviour.
#
# The corpus has a floor. tests/diff/manifest.txt names every case the oracle
# promises: a named case must have both a .logos and a .c, a pair that exists
# must be named, and the manifest must list at least MIN_CASES. Deleting a case
# therefore fails the run instead of shrinking it to a vacuously green one.
#
# Usage: bash tests/diff/run_diff.sh [--verbose]
# Exit:  0 when every case agrees, 1 on a disagreement, a corpus below the
#        floor, or a missing twin, 2 on a missing tool.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="$ROOT/tests/diff"
OUT="$ROOT/build/diff"
PYTHON="${PYTHON:-python3}"
CC="${CC:-gcc}"
VERBOSE=0
if [[ "${1:-}" == "--verbose" ]]; then
    VERBOSE=1
fi

for tool in "$PYTHON" "$CC" timeout cmp; do
    if ! command -v "$tool" > /dev/null 2>&1; then
        echo "run_diff: required tool '$tool' is not installed" >&2
        exit 2
    fi
done

cd "$ROOT" || exit 2
mkdir -p "$OUT"

shopt -s nullglob

failures=0
cases=0
problems=""

fail() {
    failures=$((failures + 1))
    problems="${problems}FAIL $1: $2"$'\n'
    if [[ $VERBOSE -eq 1 ]]; then
        printf 'FAIL %s: %s\n' "$1" "$2"
    fi
}

# run_one NAME LABEL BINARY -> writes .out and .code, prints nothing.
run_one() {
    local name=$1 label=$2 binary=$3 code
    timeout 30 "$binary" > "$OUT/$name.$label.out" 2> "$OUT/$name.$label.err"
    code=$?
    printf '%s' "$code" > "$OUT/$name.$label.code"
}

sources=("$DIR"/*.logos)
if [[ ${#sources[@]} -eq 0 ]]; then
    echo "run_diff: no .logos case in $DIR" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# The floor: the manifest names the cases this oracle must run.
# ---------------------------------------------------------------------------
MANIFEST="$DIR/manifest.txt"
MIN_CASES=12
if [[ ! -f "$MANIFEST" ]]; then
    echo "run_diff: no manifest at tests/diff/manifest.txt" >&2
    exit 1
fi

expected=()
while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    expected+=("$line")
done < "$MANIFEST"

if [[ ${#expected[@]} -lt $MIN_CASES ]]; then
    printf 'run_diff: tests/diff/manifest.txt names %d case(s); at least %d are required\n' \
        "${#expected[@]}" "$MIN_CASES" >&2
    exit 1
fi

in_manifest() {
    local name=$1 candidate
    for candidate in "${expected[@]}"; do
        if [[ "$candidate" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

for name in "${expected[@]}"; do
    if [[ ! -f "$DIR/$name.logos" ]]; then
        fail "$name" "named in tests/diff/manifest.txt but tests/diff/$name.logos is missing"
    fi
    if [[ ! -f "$DIR/$name.c" ]]; then
        fail "$name" "named in tests/diff/manifest.txt but tests/diff/$name.c is missing"
    fi
done

for source in "${sources[@]}"; do
    name="$(basename "$source" .logos)"
    twin="$DIR/$name.c"
    cases=$((cases + 1))

    if ! in_manifest "$name"; then
        fail "$name" "tests/diff/$name.logos is not named in tests/diff/manifest.txt"
        continue
    fi
    if [[ ! -f "$twin" ]]; then
        fail "$name" "no C twin at tests/diff/$name.c"
        continue
    fi
    if ! "$CC" -std=c11 -O2 -Wall -Wextra -Werror -pthread "$twin" -o "$OUT/$name.c-bin" \
            > "$OUT/$name.c-build.log" 2>&1; then
        fail "$name" "the C twin did not build (see build/diff/$name.c-build.log)"
        continue
    fi
    run_one "$name" c "$OUT/$name.c-bin"

    for level in O0 O2; do
        if ! "$PYTHON" -m logos build "$source" -o "$OUT/$name.$level" --opt "-$level" \
                > "$OUT/$name.$level-build.log" 2>&1; then
            fail "$name" "Logos did not build at -$level (see build/diff/$name.$level-build.log)"
            continue
        fi
        run_one "$name" "$level" "$OUT/$name.$level"

        if ! cmp -s "$OUT/$name.c.out" "$OUT/$name.$level.out"; then
            fail "$name" "stdout differs at -$level from gcc"
            if [[ $VERBOSE -eq 1 ]]; then
                diff -u "$OUT/$name.c.out" "$OUT/$name.$level.out" || true
            fi
            continue
        fi
        if [[ "$(cat "$OUT/$name.c.code")" != "$(cat "$OUT/$name.$level.code")" ]]; then
            fail "$name" "exit code differs at -$level: gcc $(cat "$OUT/$name.c.code"), Logos $(cat "$OUT/$name.$level.code")"
            continue
        fi
        if [[ $VERBOSE -eq 1 ]]; then
            echo "PASS $name at -$level (exit $(cat "$OUT/$name.$level.code"))"
        fi
    done
done

for twin in "$DIR"/*.c; do
    name="$(basename "$twin" .c)"
    if [[ ! -f "$DIR/$name.logos" ]]; then
        fail "$name" "a C twin has no Logos case at tests/diff/$name.logos"
    fi
done

if [[ $failures -ne 0 ]]; then
    printf '%s' "$problems"
    echo "run_diff: $cases case(s), $failures failure(s)"
    exit 1
fi

echo "run_diff: $cases case(s), each matching gcc's stdout and exit code at -O0 and -O2"
