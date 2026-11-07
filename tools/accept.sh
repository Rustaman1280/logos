#!/usr/bin/env bash
# Acceptance for the Logos compiler, end to end.
#
# It runs, in order:
#   1. the directive corpus at -O0 and -O2 against its baseline;
#   2. the differential oracle against gcc;
#   3. examples/wc.logos   against wc -c -w -l;
#   4. examples/json.logos against jq;
#   5. examples/matrix.logos against a C reference build at -O0 and -O2;
#   6. logosc fmt idempotency over every .logos file in the repository.
#
# Every section prints PASS or FAIL and the script exits 0 only when all six
# pass. Build products and captured output live under build/accept/, which is
# ignored; the working tree is never modified.
#
# Requirements: python3, clang, gcc, jq, GNU wc, timeout, cmp.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/build/accept"
PYTHON="${PYTHON:-python3}"
CC="${CC:-gcc}"
FAILURES=0
SECTIONS=0

cd "$ROOT" || exit 2
mkdir -p "$OUT"

pass() {
    printf 'PASS %s\n' "$1"
}

fail() {
    FAILURES=$((FAILURES + 1))
    printf 'FAIL %s: %s\n' "$1" "$2"
}

section() {
    SECTIONS=$((SECTIONS + 1))
}

missing=""
for tool in "$PYTHON" clang "$CC" jq wc timeout cmp find; do
    if ! command -v "$tool" > /dev/null 2>&1; then
        missing="$missing $tool"
    fi
done
if [[ -n "$missing" ]]; then
    echo "accept: required tool(s) not installed:$missing" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# 1. The directive corpus
# ---------------------------------------------------------------------------
section
if "$PYTHON" -m logos test > "$OUT/test.log" 2>&1; then
    pass "test corpus ($(grep -c '^PASS ' "$OUT/test.log") cases)"
else
    fail "test corpus" "see build/accept/test.log"
    tail -n 20 "$OUT/test.log" | sed 's/^/  /'
fi

# ---------------------------------------------------------------------------
# 2. The differential oracle
# ---------------------------------------------------------------------------
section
if bash tests/diff/run_diff.sh > "$OUT/diff.log" 2>&1; then
    pass "differential oracle ($(tail -n 1 "$OUT/diff.log"))"
else
    fail "differential oracle" "see build/accept/diff.log"
    cat "$OUT/diff.log" | sed 's/^/  /'
fi

# ---------------------------------------------------------------------------
# 3. examples/wc.logos against wc -c -w -l
# ---------------------------------------------------------------------------
section
if "$PYTHON" -m logos build examples/wc.logos -o "$OUT/wc-O2" --opt -O2 \
        > "$OUT/wc-build.log" 2>&1 \
        && "$PYTHON" -m logos build examples/wc.logos -o "$OUT/wc-O0" --opt -O0 \
        >> "$OUT/wc-build.log" 2>&1; then
    cat README.md | "$OUT/wc-O2" > "$OUT/wc-O2.out"
    cat README.md | "$OUT/wc-O0" > "$OUT/wc-O0.out"
    cat README.md | wc -c -w -l > "$OUT/wc.ref"
    if cmp -s "$OUT/wc-O2.out" "$OUT/wc.ref" \
            && cmp -s "$OUT/wc-O0.out" "$OUT/wc.ref"; then
        pass "examples/wc.logos ($(cat "$OUT/wc.ref"))"
    else
        fail "examples/wc.logos" "output differs from wc -c -w -l"
        diff -u "$OUT/wc.ref" "$OUT/wc-O2.out" | sed 's/^/  /' || true
    fi
else
    fail "examples/wc.logos" "did not build: see build/accept/wc-build.log"
fi

# ---------------------------------------------------------------------------
# 4. examples/json.logos against jq
# ---------------------------------------------------------------------------
section
cat > "$OUT/summary.jq" <<'JQ'
def depth:
  if type == "object" or type == "array"
  then 1 + (([.[] | depth] | max) // 0)
  else 0
  end;
{objects: ([.. | objects] | length),
 arrays: ([.. | arrays] | length),
 members: ([.. | objects | length] | add // 0),
 strings: ([.. | strings] | length),
 numbers: ([.. | numbers] | length),
 booleans: ([.. | booleans] | length),
 nulls: ([.. | nulls] | length),
 depth: depth}
JQ
if "$PYTHON" -m logos build examples/json.logos -o "$OUT/json-O2" --opt -O2 \
        > "$OUT/json-build.log" 2>&1 \
        && "$PYTHON" -m logos build examples/json.logos -o "$OUT/json-O0" --opt -O0 \
        >> "$OUT/json-build.log" 2>&1; then
    jq -c -f "$OUT/summary.jq" examples/sample.json > "$OUT/json.ref"
    "$OUT/json-O2" < examples/sample.json > "$OUT/json-O2.out"
    "$OUT/json-O0" < examples/sample.json > "$OUT/json-O0.out"
    if cmp -s "$OUT/json-O2.out" "$OUT/json.ref" \
            && cmp -s "$OUT/json-O0.out" "$OUT/json.ref"; then
        pass "examples/json.logos ($(cat "$OUT/json.ref"))"
    else
        fail "examples/json.logos" "output differs from jq"
        diff -u "$OUT/json.ref" "$OUT/json-O2.out" | sed 's/^/  /' || true
    fi
else
    fail "examples/json.logos" "did not build: see build/accept/json-build.log"
fi

# ---------------------------------------------------------------------------
# 5. examples/matrix.logos against a C reference build
# ---------------------------------------------------------------------------
section
if "$PYTHON" -m logos build examples/matrix.logos -o "$OUT/matrix-O2" --opt -O2 \
        > "$OUT/matrix-build.log" 2>&1 \
        && "$PYTHON" -m logos build examples/matrix.logos -o "$OUT/matrix-O0" --opt -O0 \
        >> "$OUT/matrix-build.log" 2>&1 \
        && "$CC" -std=c11 -O2 -Wall -Wextra -Werror examples/matrix_ref.c \
        -o "$OUT/matrix-ref" >> "$OUT/matrix-build.log" 2>&1; then
    "$OUT/matrix-ref" > "$OUT/matrix.ref"
    "$OUT/matrix-O2" > "$OUT/matrix-O2.out"
    "$OUT/matrix-O0" > "$OUT/matrix-O0.out"
    if cmp -s "$OUT/matrix-O2.out" "$OUT/matrix.ref" \
            && cmp -s "$OUT/matrix-O0.out" "$OUT/matrix.ref"; then
        pass "examples/matrix.logos ($(tr '\n' ' ' < "$OUT/matrix.ref" | sed 's/ $//'))"
    else
        fail "examples/matrix.logos" "output differs from the C reference"
        diff -u "$OUT/matrix.ref" "$OUT/matrix-O2.out" | sed 's/^/  /' || true
    fi
else
    fail "examples/matrix.logos" "did not build: see build/accept/matrix-build.log"
fi

# ---------------------------------------------------------------------------
# 6. fmt idempotency over every .logos file in the repository
# ---------------------------------------------------------------------------
section
FORMATTED=0
FMT_FAILURES=0
while IFS= read -r -d '' source; do
    name="${source#./}"
    FORMATTED=$((FORMATTED + 1))
    if ! "$PYTHON" -m logos fmt - < "$source" > "$OUT/fmt-once" 2> "$OUT/fmt.err"; then
        fail "fmt $name" "$(tr '\n' ' ' < "$OUT/fmt.err")"
        FMT_FAILURES=$((FMT_FAILURES + 1))
        continue
    fi
    if ! "$PYTHON" -m logos fmt - < "$OUT/fmt-once" > "$OUT/fmt-twice" 2> "$OUT/fmt.err"; then
        fail "fmt $name" "second pass failed: $(tr '\n' ' ' < "$OUT/fmt.err")"
        FMT_FAILURES=$((FMT_FAILURES + 1))
        continue
    fi
    if ! cmp -s "$OUT/fmt-once" "$OUT/fmt-twice"; then
        fail "fmt $name" "formatting is not idempotent"
        FMT_FAILURES=$((FMT_FAILURES + 1))
    fi
done < <(find . -path ./.git -prune -o -path ./build -prune -o -name '*.logos' -print0)
if [[ $FMT_FAILURES -eq 0 ]]; then
    pass "fmt idempotency ($FORMATTED files)"
fi

# ---------------------------------------------------------------------------
# 7. Compile speed: 200 functions with structs, a generic and a thread
#
# This program is generated into build/accept/ rather than checked in: it
# exists to put a number on the compiler's speed, not to be read. The limit is
# the specification's: five seconds for exactly this shape.
# ---------------------------------------------------------------------------
section
{
    echo '(mod speed)'
    echo
    echo '(type point (struct (x :i64) (y :i64)))'
    echo
    echo '(fn identity :T ((v :T))'
    echo '  (ret v))'
    echo
    echo '(fn worker :void ((flag (shared (ptr :i64))))'
    echo '  (set-deref flag 1))'
    echo
    for i in $(seq 0 199); do
        printf '(fn f%s :i64 ()\n  (ret (+ %s 1)))\n\n' "$i" "$i"
    done
    echo '(fn main :void ()'
    echo '  (letmut total :i64 0)'
    for i in $(seq 0 199); do
        printf '  (set total (+ total (f%s)))\n' "$i"
    done
    echo '  (let p point (make point 1 2))'
    echo '  (let copy point (identity p))'
    echo '  (let flag (alloc :i64 1))'
    echo '  (set-deref (ptr flag) 0)'
    echo '  (let shared-flag (shared (ptr :i64)) (ptr flag))'
    echo '  (let t (spawn worker shared-flag))'
    echo '  (join t)'
    echo '  (println (+ (get copy x) (get copy y)))'
    echo '  (println total)'
    echo '  (free flag))'
} > "$OUT/speed.logos"
SPEED_START=$(date +%s%N)
if "$PYTHON" -m logos build "$OUT/speed.logos" -o "$OUT/speed" --opt -O2 \
        > "$OUT/speed-build.log" 2>&1; then
    SPEED_END=$(date +%s%N)
    SPEED_MS=$(( (SPEED_END - SPEED_START) / 1000000 ))
    "$OUT/speed" > "$OUT/speed.out" 2> "$OUT/speed.err"
    if [[ "$(cat "$OUT/speed.out")" != $'3\n20100' ]]; then
        fail "compile speed" "the 200-function program printed unexpected output"
    elif [[ $SPEED_MS -gt 5000 ]]; then
        fail "compile speed" "200 functions took ${SPEED_MS} ms, over the 5000 ms limit"
    else
        pass "compile speed (200 functions, ${SPEED_MS} ms)"
    fi
else
    fail "compile speed" "the 200-function program did not build: see build/accept/speed-build.log"
fi

# ---------------------------------------------------------------------------
echo
if [[ $FAILURES -eq 0 ]]; then
    echo "accept: $SECTIONS section(s), every section PASS"
    exit 0
fi
echo "accept: $SECTIONS section(s), $FAILURES failure(s)"
exit 1
