"""Command line entry point for the Logos toolchain.

The compiler is built incrementally. Every subcommand except ``version`` is a
placeholder that parses its arguments and reports that it is not implemented
yet, so the command surface is fixed before the passes behind it exist.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from logos import __version__
from logos.diag import LogosError
from logos.fmt import render
from logos.parser import parse

PROG = "logosc"

#: ``--opt`` values are spelled ``-O0`` and ``-O2``, which argparse would read
#: as options rather than as the flag's value. ``_normalize_argv`` rewrites them
#: into the ``--opt=-O0`` form before parsing so both spellings work.
_OPT_LEVEL = re.compile(r"^-O[0-9]+$")


def _normalize_argv(argv: list[str]) -> list[str]:
    """Attach ``-O`` level tokens to the ``--opt`` flag that precedes them.

    ``main(["test", "f.logos", "--opt", "-O0", "-O2"])`` reaches argparse as
    ``["test", "f.logos", "--opt=-O0", "--opt=-O2"]``. Every other token is
    passed through untouched.
    """
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token != "--opt":
            normalized.append(token)
            index += 1
            continue
        index += 1
        attached = False
        while index < len(argv) and _OPT_LEVEL.match(argv[index]):
            normalized.append(f"--opt={argv[index]}")
            attached = True
            index += 1
        if not attached:
            normalized.append(token)
    return normalized


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Compiler for Logos, a prefix-form language for AI readers.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    build = sub.add_parser("build", help="compile a Logos source file to a native binary")
    build.add_argument("file", metavar="FILE", help="Logos source file")
    build.add_argument("-o", dest="out", metavar="OUT", help="path of the output binary")
    build.add_argument(
        "--opt",
        metavar="O",
        default="-O0",
        help="optimisation level, -O0 or -O2 (default: -O0)",
    )
    build.add_argument(
        "--keep-ir",
        action="store_true",
        help="keep the generated LLVM IR next to the binary",
    )

    test = sub.add_parser("test", help="compile and run the (test ...) nodes in a file")
    test.add_argument(
        "path",
        metavar="PATH",
        nargs="?",
        default="tests",
        help="file or directory of tests (default: tests)",
    )
    test.add_argument("--only", metavar="CAT", help="run only the named test category")
    test.add_argument(
        "--opt",
        metavar="O",
        action="append",
        help="optimisation level to run; repeat for several, e.g. --opt -O0 -O2",
    )

    fmt = sub.add_parser("fmt", help="print Logos source in canonical form")
    fmt.add_argument(
        "files",
        metavar="FILE",
        nargs="+",
        help="Logos source file, directory of sources, or - for stdin",
    )

    show = sub.add_parser("show", help="print one node of a Logos file")
    show.add_argument("path", metavar="PATH", help="source file and node path, as FILE:NODE")

    patch = sub.add_parser("patch", help="replace one node of a Logos file")
    patch.add_argument(
        "path",
        metavar="PATH",
        help="source file and node path, as FILE:NODE",
    )

    sub.add_parser("version", help="print the compiler version")

    return parser


def main(argv: list[str]) -> int:
    """Run the command line interface and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(_normalize_argv(list(argv)))

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"logos {__version__}")
        return 0

    if args.command == "fmt":
        return _fmt(args.files)

    print(f"{args.command}: not implemented")
    return 0


def _fmt(arguments: list[str]) -> int:
    """Print the canonical form of every named source, file by file."""
    status = 0
    read_stdin = False
    for argument in arguments:
        if argument == "-":
            if read_stdin:
                continue
            read_stdin = True
            status |= _format("<stdin>", sys.stdin.read())
            continue
        path = Path(argument)
        if path.is_dir():
            for child in sorted(path.rglob("*.logos")):
                status |= _format_path(child)
            continue
        status |= _format_path(path)
    return status


def _format_path(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"logos: {path}: {error.strerror}", file=sys.stderr)
        return 1
    return _format(str(path), text)


def _format(name: str, text: str) -> int:
    try:
        canonical = render(parse(text, name))
    except LogosError as error:
        print(f"{name}: {error.render()}", file=sys.stderr)
        return 1
    sys.stdout.write(canonical)
    return 0


def run() -> int:
    """Console script wrapper used by ``logosc``."""
    return main(sys.argv[1:])
