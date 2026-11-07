#!/usr/bin/env bash
# Verify that `pip install .` produces a working `logosc`.
#
# The check is deliberately end to end: it installs into a throwaway target
# directory (never a site-/dist-packages path, which differs by interpreter
# version and by distribution), confirms the C runtime shipped as package
# data, then builds and runs examples/hello.logos from a directory outside the
# repository with the installed package on PYTHONPATH. Nothing from the source
# tree is importable except the example it compiles, so a missing rt.c or a
# driver that only finds the runtime at its source path fails here.
#
# `pip install --target` is used instead of `--prefix` because the latter puts
# the package wherever the interpreter's install scheme says (lib/site-packages
# on CPython, lib/dist-packages on Debian, and so on), while --target writes
# exactly where we tell it.
#
# Usage: tools/verify_install.sh [PREFIX]   (default /tmp/lg-install)
# Environment: PYTHON overrides the interpreter used for pip, if it has pip.
#
# The script is rerunnable: the prefix is removed first and the build runs in a
# temporary directory that is removed on exit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${1:-/tmp/lg-install}"
PKG_DIR="$PREFIX/pkg"
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

rm -rf "$PREFIX"
mkdir -p "$PKG_DIR"
# --no-deps: this project has no runtime dependencies, and reinstalling any
# that pip thought it saw would write outside the target.
"${PIP[@]}" install --target "$PKG_DIR" --no-deps --quiet "$ROOT"

for asset in rt.c rt.h; do
    if [[ ! -f "$PKG_DIR/runtime/$asset" ]]; then
        echo "verify_install: installed package is missing runtime/$asset" >&2
        exit 1
    fi
done

# The console script is the artifact under test. Its shebang names the
# interpreter pip used, which is the one that must import the package.
LOGOSC="$PKG_DIR/bin/logosc"
if [[ ! -x "$LOGOSC" ]]; then
    echo "verify_install: install produced no executable bin/logosc in $PKG_DIR" >&2
    exit 1
fi
RUNPY="$PYTHON"
SHEBANG="$(head -n 1 "$LOGOSC")"
SHEBANG="${SHEBANG#\#!}"
if [[ "$SHEBANG" == /* && -x "$SHEBANG" ]]; then
    RUNPY="$SHEBANG"
fi

export PYTHONPATH="$PKG_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

"$LOGOSC" version > "$WORK/version"
if [[ ! -s "$WORK/version" ]]; then
    echo "verify_install: logosc version printed nothing" >&2
    exit 1
fi

# Build from outside the repository: only the installed package may supply the
# compiler and the runtime. The example is named by its absolute path.
cd "$WORK"
"$LOGOSC" build "$ROOT/examples/hello.logos" -o "$WORK/hello" > "$WORK/build.log" 2>&1 || {
    echo "verify_install: building examples/hello.logos failed:" >&2
    cat "$WORK/build.log" >&2
    exit 1
}

OUTPUT="$("$WORK/hello")"
if [[ "$OUTPUT" != "hello" ]]; then
    echo "verify_install: hello printed '$OUTPUT', expected 'hello'" >&2
    exit 1
fi

PYVER="$("$RUNPY" -c 'import platform; print(platform.python_implementation() + " " + platform.python_version())')"
echo "verify_install: PASS ($PYVER, $PREFIX)"
