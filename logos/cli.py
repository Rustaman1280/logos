"""Command line entry point for the Logos toolchain.

The compiler is built incrementally. Every subcommand except ``version`` is a
placeholder that accepts its arguments and reports that it is not implemented
yet, so the command surface is fixed before the passes behind it exist.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from logos import __version__

PROG = "logosc"

#: Subcommands that will be wired to real work in later tasks.
PENDING_COMMANDS = ("build", "test", "fmt", "show", "patch")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Compiler for Logos, a prefix-form language for AI readers.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    build = sub.add_parser("build", help="compile a Logos source file to a native binary")
    build.add_argument("args", nargs="*", help="source file and options")

    test = sub.add_parser("test", help="compile and run the (test ...) nodes in a file")
    test.add_argument("args", nargs="*", help="source file and options")

    fmt = sub.add_parser("fmt", help="print Logos source in canonical form")
    fmt.add_argument("args", nargs="*", help="files or directories")

    show = sub.add_parser("show", help="print one node of a Logos file")
    show.add_argument("args", nargs="*", help="source file and node path")

    patch = sub.add_parser("patch", help="replace one node of a Logos file")
    patch.add_argument("args", nargs="*", help="source file, node path and replacement")

    sub.add_parser("version", help="print the compiler version")

    return parser


def main(argv: list[str]) -> int:
    """Run the command line interface and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"logos {__version__}")
        return 0

    print(f"{args.command}: not implemented")
    return 0


def run() -> int:
    """Console script wrapper used by ``logosc``."""
    return main(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
