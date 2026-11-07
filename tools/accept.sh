#!/usr/bin/env bash
# Acceptance for the Logos compiler, end to end.
#
# It runs, in order:
#   1. the directive corpus at -O0 and -O2 against its baseline;
#   2. the differential oracle against gcc;
#   3. examples/wc.logos against wc -c -w -l on README.md and the Unicode inputs;
#   4. examples/json.logos against jq on a fixture, a duplicate-key document and
#      a malformed one;
#   5. examples/matrix.logos against a C reference build at -O0 and -O2;
#   6. logosc fmt idempotency over every .logos file in the repository;
#   7. compile speed: a 200-function program under five seconds.
#
# Every section prints PASS or FAIL, a section that examined zero items is a
# failure, and the script exits 0 only when all EXPECTED_SECTIONS pass. The
# per-section floors (MIN_DIFF_CASES, MIN_FORMATTED, MIN_WC_INPUTS,
# MIN_JSON_INPUTS) are there so deleting a fixture cannot turn a gate green.
#
# Build products and captured output live under build/accept/, which is
# ignored; the working tree is never modified.
#
# Requirements: python3, clang, gcc, jq, GNU wc, timeout, cmp.

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/build/accept"
PYTHON="${PYTHON:-python3}"
CC="${CC:-gcc}"
FAILURES=0
SECTIONS=0
SECTION_REPORTED=0

EXPECTED_SECTIONS=7
MIN_DIFF_CASES=3
MIN_FORMATTED=37
MIN_WC_INPUTS=2
MIN_JSON_INPUTS=3

cd "$ROOT" || exit 2
mkdir -p "$OUT"

pass() {
    SECTION_REPORTED=1
    printf 'PASS %s\n' "$1"
}

fail() {
    SECTION_REPORTED=1
    FAILURES=$((FAILURES + 1))
    printf 'FAIL %s: %s\n' "$1" "$2"
}

section() {
    if [[ $SECTIONS -gt 0 && $SECTION_REPORTED -eq 0 ]]; then
        fail "section $SECTIONS" "reported neither PASS nor FAIL"
    fi
    SECTIONS=$((SECTIONS + 1))
    SECTION_REPORTED=0
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
    CASES=$(grep -c '^PASS ' "$OUT/test.log")
    if [[ "$CASES" -eq 0 ]]; then
        fail "test corpus" "the run passed no case; the corpus is empty"
    else
        pass "test corpus ($CASES cases)"
    fi
else
    fail "test corpus" "see build/accept/test.log"
    tail -n 20 "$OUT/test.log" | sed 's/^/  /'
fi

# ---------------------------------------------------------------------------
# 2. The differential oracle
# ---------------------------------------------------------------------------
section
if bash tests/diff/run_diff.sh > "$OUT/diff.log" 2>&1; then
    DIFF_CASES=$(sed -n 's/.*run_diff: \([0-9][0-9]*\) case(s).*/\1/p' "$OUT/diff.log" | tail -n 1)
    if [[ -z "$DIFF_CASES" || "$DIFF_CASES" -lt $MIN_DIFF_CASES ]]; then
        fail "differential oracle" "ran ${DIFF_CASES:-0} case(s), expected at least $MIN_DIFF_CASES"
        cat "$OUT/diff.log" | sed 's/^/  /'
    else
        pass "differential oracle ($(tail -n 1 "$OUT/diff.log"))"
    fi
else
    fail "differential oracle" "see build/accept/diff.log"
    cat "$OUT/diff.log" | sed 's/^/  /'
fi

# ---------------------------------------------------------------------------
# 3. examples/wc.logos against wc -c -w -l
#
# Every input goes through a pipe, because that is the case where GNU wc uses
# its fixed 7-wide fields. Both optimisation levels are compared, and the
# process's exit status and stderr are checked: a pipeline without pipefail
# would hide a program that printed the right bytes and then failed.
# ---------------------------------------------------------------------------
section
shopt -s nullglob
WC_INPUTS=("README.md" examples/inputs/*.txt)
if [[ ${#WC_INPUTS[@]} -lt $MIN_WC_INPUTS ]]; then
    fail "examples/wc.logos" "found ${#WC_INPUTS[@]} input(s), expected at least $MIN_WC_INPUTS"
elif ! "$PYTHON" -m logos build examples/wc.logos -o "$OUT/wc-O2" --opt -O2 \
        > "$OUT/wc-build.log" 2>&1 \
        || ! "$PYTHON" -m logos build examples/wc.logos -o "$OUT/wc-O0" --opt -O0 \
        >> "$OUT/wc-build.log" 2>&1; then
    fail "examples/wc.logos" "did not build: see build/accept/wc-build.log"
else
    WC_FAILURES=0
    WC_REF=""
    for input in "${WC_INPUTS[@]}"; do
        WC_REF_FILE="$OUT/wc-ref.out"
        cat "$input" | wc -c -w -l > "$WC_REF_FILE" 2> "$OUT/wc.ref.err"
        REF_CODE=$?
        if [[ $REF_CODE -ne 0 ]]; then
            fail "examples/wc.logos $input" "wc exited $REF_CODE: $(tr '\n' ' ' < "$OUT/wc.ref.err")"
            WC_FAILURES=$((WC_FAILURES + 1))
            continue
        fi
        WC_REF="$(cat "$WC_REF_FILE")"
        for level in O0 O2; do
            cat "$input" | "$OUT/wc-$level" > "$OUT/wc-$level.out" 2> "$OUT/wc-$level.err"
            CODE=$?
            if [[ $CODE -ne 0 ]]; then
                fail "examples/wc.logos $input -$level" "exited $CODE: $(tr '\n' ' ' < "$OUT/wc-$level.err")"
                WC_FAILURES=$((WC_FAILURES + 1))
            elif [[ -s "$OUT/wc-$level.err" ]]; then
                fail "examples/wc.logos $input -$level" "wrote to stderr: $(tr '\n' ' ' < "$OUT/wc-$level.err")"
                WC_FAILURES=$((WC_FAILURES + 1))
            elif ! cmp -s "$OUT/wc-$level.out" "$WC_REF_FILE"; then
                fail "examples/wc.logos $input -$level" "output differs from wc -c -w -l"
                diff -u "$WC_REF_FILE" "$OUT/wc-$level.out" | sed 's/^/  /' || true
                WC_FAILURES=$((WC_FAILURES + 1))
            fi
        done
    done
    if [[ $WC_FAILURES -eq 0 ]]; then
        pass "examples/wc.logos (${#WC_INPUTS[@]} inputs, $WC_REF)"
    fi
fi

# ---------------------------------------------------------------------------
# 4. examples/json.logos against jq
#
# Two valid documents (the fixture and one with duplicate keys, where jq keeps
# only the last member of a repeated key) and one malformed document. A valid
# document must match jq's summary byte for byte with a zero exit and no
# stderr; the malformed one must be refused by both programs. jq exits 5 on a
# parse error and writes nothing to stdout.
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
JSON_VALID=("examples/sample.json" "examples/inputs/duplicate-keys.json")
JSON_BAD="examples/inputs/malformed.json"
JSON_ITEMS=$(( ${#JSON_VALID[@]} + 1 ))
if [[ $JSON_ITEMS -lt $MIN_JSON_INPUTS || ! -f "$JSON_BAD" ]]; then
    fail "examples/json.logos" "found $JSON_ITEMS input(s), expected at least $MIN_JSON_INPUTS"
elif ! "$PYTHON" -m logos build examples/json.logos -o "$OUT/json-O2" --opt -O2 \
        > "$OUT/json-build.log" 2>&1 \
        || ! "$PYTHON" -m logos build examples/json.logos -o "$OUT/json-O0" --opt -O0 \
        >> "$OUT/json-build.log" 2>&1; then
    fail "examples/json.logos" "did not build: see build/accept/json-build.log"
else
    JSON_FAILURES=0
    for input in "${JSON_VALID[@]}"; do
        jq -c -f "$OUT/summary.jq" "$input" > "$OUT/json.ref" 2> "$OUT/json.ref.err"
        JQ_CODE=$?
        if [[ $JQ_CODE -ne 0 ]]; then
            fail "examples/json.logos $input" "jq exited $JQ_CODE: $(tr '\n' ' ' < "$OUT/json.ref.err")"
            JSON_FAILURES=$((JSON_FAILURES + 1))
            continue
        fi
        for level in O0 O2; do
            "$OUT/json-$level" < "$input" > "$OUT/json-$level.out" 2> "$OUT/json-$level.err"
            CODE=$?
            if [[ $CODE -ne 0 ]]; then
                fail "examples/json.logos $input -$level" "exited $CODE: $(tr '\n' ' ' < "$OUT/json-$level.err")"
                JSON_FAILURES=$((JSON_FAILURES + 1))
            elif [[ -s "$OUT/json-$level.err" ]]; then
                fail "examples/json.logos $input -$level" "wrote to stderr: $(tr '\n' ' ' < "$OUT/json-$level.err")"
                JSON_FAILURES=$((JSON_FAILURES + 1))
            elif ! cmp -s "$OUT/json-$level.out" "$OUT/json.ref"; then
                fail "examples/json.logos $input -$level" "output differs from jq"
                diff -u "$OUT/json.ref" "$OUT/json-$level.out" | sed 's/^/  /' || true
                JSON_FAILURES=$((JSON_FAILURES + 1))
            fi
        done
    done

    jq -c -f "$OUT/summary.jq" "$JSON_BAD" > "$OUT/json.bad.ref" 2> "$OUT/json.bad.jqerr"
    JQ_CODE=$?
    if [[ $JQ_CODE -eq 0 ]]; then
        fail "examples/json.logos $JSON_BAD" "jq accepted the malformed document"
        JSON_FAILURES=$((JSON_FAILURES + 1))
    fi
    for level in O0 O2; do
        "$OUT/json-$level" < "$JSON_BAD" > "$OUT/json-$level.bad" 2> "$OUT/json-$level.bad.err"
        CODE=$?
        if [[ $CODE -eq 0 ]]; then
            fail "examples/json.logos $JSON_BAD -$level" "accepted the malformed document"
            JSON_FAILURES=$((JSON_FAILURES + 1))
        elif ! grep -q '^invalid json: ' "$OUT/json-$level.bad"; then
            fail "examples/json.logos $JSON_BAD -$level" "refused without a diagnostic: $(tr '\n' ' ' < "$OUT/json-$level.bad")"
            JSON_FAILURES=$((JSON_FAILURES + 1))
        fi
    done
    if [[ $JSON_FAILURES -eq 0 ]]; then
        pass "examples/json.logos (2 valid documents and 1 malformed, $(cat "$OUT/json.ref"))"
    fi
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
    MATRIX_FAILURES=0
    "$OUT/matrix-ref" > "$OUT/matrix.ref" 2> "$OUT/matrix.err"
    CODE=$?
    if [[ $CODE -ne 0 || -s "$OUT/matrix.err" || ! -s "$OUT/matrix.ref" ]]; then
        fail "examples/matrix.logos reference" "exited $CODE with $(wc -c < "$OUT/matrix.ref") byte(s) of output"
        MATRIX_FAILURES=$((MATRIX_FAILURES + 1))
    fi
    for level in O0 O2; do
        "$OUT/matrix-$level" > "$OUT/matrix-$level.out" 2> "$OUT/matrix-$level.err"
        CODE=$?
        if [[ $CODE -ne 0 ]]; then
            fail "examples/matrix.logos -$level" "exited $CODE: $(tr '\n' ' ' < "$OUT/matrix-$level.err")"
            MATRIX_FAILURES=$((MATRIX_FAILURES + 1))
        elif [[ -s "$OUT/matrix-$level.err" ]]; then
            fail "examples/matrix.logos -$level" "wrote to stderr: $(tr '\n' ' ' < "$OUT/matrix-$level.err")"
            MATRIX_FAILURES=$((MATRIX_FAILURES + 1))
        elif ! cmp -s "$OUT/matrix-$level.out" "$OUT/matrix.ref"; then
            fail "examples/matrix.logos -$level" "output differs from the C reference"
            diff -u "$OUT/matrix.ref" "$OUT/matrix-$level.out" | sed 's/^/  /' || true
            MATRIX_FAILURES=$((MATRIX_FAILURES + 1))
        fi
    done
    if [[ $MATRIX_FAILURES -eq 0 ]]; then
        pass "examples/matrix.logos ($(tr '\n' ' ' < "$OUT/matrix.ref" | sed 's/ $//'))"
    fi
else
    fail "examples/matrix.logos" "did not build: see build/accept/matrix-build.log"
fi

# ---------------------------------------------------------------------------
# 6. fmt idempotency over every .logos file in the repository
#
# MIN_FORMATTED is the count recorded when this gate was written (37). Adding
# files raises it; deleting files must fail the section rather than quietly
# shrink it.
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
if [[ $FORMATTED -lt $MIN_FORMATTED ]]; then
    fail "fmt idempotency" "examined $FORMATTED file(s), expected at least $MIN_FORMATTED"
elif [[ $FMT_FAILURES -eq 0 ]]; then
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
    SPEED_CODE=$?
    if [[ $SPEED_CODE -ne 0 || -s "$OUT/speed.err" ]]; then
        fail "compile speed" "the 200-function program exited $SPEED_CODE: $(tr '\n' ' ' < "$OUT/speed.err")"
    elif [[ "$(cat "$OUT/speed.out")" != $'3\n20100' ]]; then
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
# The section count is part of the gate: a section that neither passed nor
# failed, or a section quietly removed from this script, is a failure.
# ---------------------------------------------------------------------------
if [[ $SECTION_REPORTED -eq 0 ]]; then
    fail "section $SECTIONS" "reported neither PASS nor FAIL"
fi
if [[ $SECTIONS -ne $EXPECTED_SECTIONS ]]; then
    fail "sections" "ran $SECTIONS section(s), expected $EXPECTED_SECTIONS"
fi

echo
if [[ $FAILURES -eq 0 ]]; then
    echo "accept: $SECTIONS section(s), every section PASS"
    exit 0
fi
echo "accept: $SECTIONS section(s), $FAILURES failure(s)"
exit 1
