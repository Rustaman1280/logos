#!/usr/bin/env bash
# Verify that `pip install .` produces a working `logosc`.
#
# The check is deliberately end to end: it installs into a throwaway target
# directory (never a site-/dist-packages path, which differs by interpreter
# version and by distribution), confirms the C runtime shipped inside the
# compiler package as package data, then builds and runs examples/hello.logos
# from a directory outside the repository with the installed package on
# PYTHONPATH. Nothing from the source tree is importable except the example it
# compiles, so a missing logos/runtime/rt.c or a driver that only finds the
# runtime at its source path fails here.
#
# `pip install --target` is used instead of `--prefix` because the latter puts
# the package wherever the interpreter's install scheme says (lib/site-packages
# on CPython, lib/dist-packages on Debian, and so on), while --target writes
# exactly where we tell it.
#
# Usage: tools/verify_install.sh [PREFIX]   (default /tmp/lg-install)
# Environment: PYTHON overrides the interpreter used to build and install.
#
# The script is rerunnable: the prefix is removed first and the build runs in a
# temporary directory that is removed on exit.
#
# A wheel's metadata comes from the [project] table in pyproject.toml, which
# only setuptools >= 61 reads. With an older backend the build does not fail:
# it produces UNKNOWN-0.0.0-py3-none-any.whl containing dist-info and nothing
# else, and the missing runtime surfaces later as two unrelated file errors.
# So the backend is checked up front, upgraded once in this interpreter, and
# the build is refused (exit 2) if it is still too old.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${1:-/tmp/lg-install}"
PKG_DIR="$PREFIX/pkg"
PYTHON="${PYTHON:-python3}"
MIN_SETUPTOOLS="61"

if ! command -v "$PYTHON" > /dev/null 2>&1; then
    echo "verify_install: interpreter '$PYTHON' not found" >&2
    exit 2
fi

setuptools_version() {
    "$1" -c 'import setuptools; print(setuptools.__version__)' 2>/dev/null || true
}

version_ge() {  # version_ge FOUND REQUIRED
    local found="$1" required="$2"
    [[ -n "$found" ]] || return 1
    [[ "$(printf '%s\n%s\n' "$required" "$found" | sort -V | head -n 1)" == "$required" ]]
}

backend_ready() {
    local version
    version="$(setuptools_version "$PYTHON")"
    version_ge "$version" "$MIN_SETUPTOOLS" || return 1
    # setuptools bundles bdist_wheel from 70.1; before that the wheel package
    # must be importable for a --no-build-isolation build.
    if ! version_ge "$version" "70.1"; then
        "$PYTHON" -c 'import wheel' 2>/dev/null || return 1
    fi
}

if ! "$PYTHON" -m pip --version > /dev/null 2>&1; then
    # A venv created with --without-pip still ships ensurepip; use it so the
    # interpreter, not some unrelated pip on PATH, owns the build.
    "$PYTHON" -m ensurepip --upgrade > /dev/null 2>&1 || true
fi

PIP=()
if "$PYTHON" -m pip --version > /dev/null 2>&1; then
    PIP=("$PYTHON" -m pip)
fi

FIX_COMMAND="$PYTHON -m pip install --upgrade \"pip>=23\" \"setuptools>=68\" wheel"

if [[ ${#PIP[@]} -eq 0 ]]; then
    NO_PIP_VERSION="$(setuptools_version "$PYTHON")"
    echo "verify_install: '$PYTHON' has setuptools ${NO_PIP_VERSION:-<none>} and no pip, so it cannot be checked against setuptools>=$MIN_SETUPTOOLS." >&2
    echo "verify_install: without that, the build produces UNKNOWN-0.0.0-py3-none-any.whl instead of logos." >&2
    echo "verify_install: fix it with: $PYTHON -m ensurepip --upgrade && $FIX_COMMAND" >&2
    exit 2
fi

if ! backend_ready; then
    OLD_VERSION="$(setuptools_version "$PYTHON")"
    echo "verify_install: '$PYTHON' has setuptools ${OLD_VERSION:-<none>}; PEP 621 [project] metadata needs setuptools>=$MIN_SETUPTOOLS." >&2
    echo "verify_install: setuptools < $MIN_SETUPTOOLS ignores [project] and builds UNKNOWN-0.0.0-py3-none-any.whl, so this is refused." >&2
    echo "verify_install: attempting one upgrade in that interpreter" >&2
    if ! "${PIP[@]}" install --upgrade "setuptools>=68" wheel; then
        echo "verify_install: the upgrade failed; fix the interpreter with:" >&2
        echo "verify_install:   $FIX_COMMAND" >&2
        echo "verify_install: refusing to build a wheel with setuptools $OLD_VERSION." >&2
        exit 2
    fi
    if ! backend_ready; then
        echo "verify_install: after upgrading, '$PYTHON' has setuptools $(setuptools_version "$PYTHON"), still below $MIN_SETUPTOOLS; refusing to build." >&2
        echo "verify_install: fix it with: $FIX_COMMAND" >&2
        exit 2
    fi
    echo "verify_install: upgraded setuptools to $(setuptools_version "$PYTHON")" >&2
fi

rm -rf "$PREFIX"
mkdir -p "$PKG_DIR"
# --no-deps: this project has no runtime dependencies, and reinstalling any
# that pip thought it saw would write outside the target.
# --no-build-isolation: the backend was verified above, so build with this
# interpreter's setuptools rather than a copy fetched per run.
"${PIP[@]}" install --target "$PKG_DIR" --no-deps --no-build-isolation --quiet "$ROOT"

for asset in rt.c rt.h; do
    if [[ ! -f "$PKG_DIR/logos/runtime/$asset" ]]; then
        echo "verify_install: installed package is missing logos/runtime/$asset" >&2
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
