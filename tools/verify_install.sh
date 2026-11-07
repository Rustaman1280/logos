#!/usr/bin/env bash
# Verify that `pip install .` produces a working `logosc`.
#
# The check is deliberately end to end: it installs into a throwaway prefix,
# confirms the C runtime shipped as package data, then builds and runs
# examples/hello.logos from a directory outside the repository with the
# installed package on PYTHONPATH. Nothing from the source tree is importable
# except the example it compiles, so a missing rt.c or a driver that only
# finds the runtime at its source path fails here.
#
# Usage: tools/verify_install.sh [PREFIX]   (default /tmp/lg-install)
# Environment: PYTHON overrides the interpreter used for pip, if it has pip.
#
# The script is rerunnable: the prefix is removed first and the build runs in a
# temporary directory that is removed on exit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${1:-/tmp/lg-install}"
PYTHON="${PYTHON:-python3}"

if "$PYTHON" -m pip --version > /dev/null 2>&1; then
    PIP=("$PYTHON" -m pip)
else
    PIP_BIN="$(command -v pip3 || command -v pip || true)"
    if [[ -z "$PIP_BIN" ]]; then
        echo "verify_install: no pip on '$PYTHON' and no pip on PATH" >&2
        exit 2
    fi
    PIP=("$PIP_BIN")
fi
RUNPY="$PYTHON"

rm -rf "$PREFIX"
"${PIP[@]}" install --prefix "$PREFIX" --no-deps --quiet "$ROOT"

VERSION="$("$RUNPY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
SITE="$PREFIX/lib/python$VERSION/site-packages"
if [[ ! -d "$SITE" ]]; then
    echo "verify_install: no site-packages at $SITE" >&2
    exit 1
fi

for asset in rt.c rt.h; do
    if [[ ! -f "$SITE/runtime/$asset" ]]; then
        echo "verify_install: installed package is missing runtime/$asset" >&2
        exit 1
    fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PYTHONPATH="$SITE" "$PREFIX/bin/logosc" version > "$WORK/version"
if [[ ! -s "$WORK/version" ]]; then
    echo "verify_install: logosc version printed nothing" >&2
    exit 1
fi

# Build from outside the repository: only the installed package may supply the
# compiler and the runtime. The example is named by its absolute path.
cd "$WORK"
PYTHONPATH="$SITE" "$PREFIX/bin/logosc" build "$ROOT/examples/hello.logos" \
    -o "$WORK/hello" > "$WORK/build.log" 2>&1 || {
    echo "verify_install: building examples/hello.logos failed:" >&2
    cat "$WORK/build.log" >&2
    exit 1
}

OUTPUT="$("$WORK/hello")"
if [[ "$OUTPUT" != "hello" ]]; then
    echo "verify_install: hello printed '$OUTPUT', expected 'hello'" >&2
    exit 1
fi

echo "verify_install: PASS ($("$RUNPY" -c 'import sys; print(sys.version.split()[0])'), $PREFIX)"
