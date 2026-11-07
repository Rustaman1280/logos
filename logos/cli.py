"""Command line entry point for the Logos toolchain.

``build`` compiles a file to a native binary, ``test`` runs the directive-driven
corpus, and ``fmt`` rewrites source in canonical form. ``show`` prints one node
and ``patch`` replaces one node, both addressed by the node paths of
``logos.nodes``; a patch reformats the whole file, so everything outside the
replaced subtree comes back in the same canonical bytes ``fmt`` would write.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from logos import __version__, driver, tester
from logos.diag import LogosError
from logos.fmt import render
from logos.nodes import Node, assign_paths, find
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
        default="-O2",
        help="optimisation level, -O0 or -O2 (default: -O2)",
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
    test.add_argument(
        "--update-baseline",
        action="store_true",
        help="write the run's results to the suite's summary.txt baseline",
    )

    fmt = sub.add_parser("fmt", help="rewrite Logos source in canonical form")
    fmt.add_argument(
        "files",
        metavar="FILE",
        nargs="+",
        help="Logos source file, directory of sources, or - for stdin",
    )

    show = sub.add_parser("show", help="print one node of a Logos file")
    show.add_argument("file", metavar="FILE", help="Logos source file")
    show.add_argument(
        "path",
        metavar="PATH",
        help="node path such as hello/main/2/0 (the empty path is the file)",
    )

    patch = sub.add_parser("patch", help="replace one node of a Logos file")
    patch.add_argument("file", metavar="FILE", help="Logos source file")
    patch.add_argument(
        "path",
        metavar="PATH",
        help="node path such as hello/main/2/0",
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

    if args.command == "build":
        return _build(args.file, args.out, args.opt)

    if args.command == "test":
        return _test(args.path, args.only, args.opt, args.update_baseline)

    if args.command == "show":
        return _show(args.file, args.path)

    if args.command == "patch":
        return _patch(args.file, args.path)

    print(f"{args.command}: not implemented")
    return 0


def _baseline_argument_problem(path: str) -> str | None:
    """Why ``--update-baseline`` cannot use ``path``, or ``None`` if it can.

    A baseline belongs to a suite root: a directory that contains at least one
    known category directory. A single file has no suite, a category directory
    is not the root of one, and a nested or unrelated directory would get a
    ``summary.txt`` that no run ever reads. Writing one there is a stray file
    that looks like a baseline, so all three are refused rather than served.
    """
    root = Path(path)
    if root.is_file():
        return "--update-baseline needs a directory, not a single file"
    if not root.is_dir():
        return f"--update-baseline needs a directory, and '{path}' is not one"
    if root.name in tester.CATEGORIES:
        return (
            f"--update-baseline needs the suite root, not the category "
            f"directory '{path}'"
        )
    if not any((root / category).is_dir() for category in tester.CATEGORIES):
        return (
            f"--update-baseline needs the suite root, a directory holding a "
            f"category such as 'exec'; '{path}' is not one"
        )
    return None


def _test(
    path: str, only: str | None, opts: list[str] | None, update_baseline: bool
) -> int:
    """Run the corpus, print one line per case, and enforce the baseline.

    The exit status is 1 when any case fails, when the baseline records a
    regression or a leak total above the baseline's, when a file sat outside
    every known category, or when there was nothing to run. A usage error (an
    unknown category, or a baseline asked for a file or a category directory)
    is 2, as elsewhere in the command line.
    """
    if update_baseline:
        problem = _baseline_argument_problem(path)
        if problem is not None:
            print(f"logos: test: {problem}", file=sys.stderr)
            return 2

    try:
        summary = tester.run_suite(path, only, opts)
    except tester.UsageError as error:
        print(f"logos: test: {error}", file=sys.stderr)
        return 2
    except tester.SuiteError as error:
        print(f"logos: test: {error}", file=sys.stderr)
        return 1

    for case in summary.cases:
        print(f"{case.status} {case.id} {case.detail}")
    for skipped in summary.skipped:
        print(f"SKIP {skipped}")
    print(f"{summary.passed} passed, {summary.failed} failed, {summary.leaks} leaked")

    if summary.total == 0:
        print(f"test: no cases found in {path}")
        return 1

    if summary.skipped:
        print(
            f"logos: test: {len(summary.skipped)} file(s) outside the known "
            f"categories were skipped",
            file=sys.stderr,
        )
        return 1

    baseline = tester.baseline_path(path)
    try:
        if update_baseline:
            if baseline is None:
                print("logos: test: --update-baseline needs a directory", file=sys.stderr)
                return 2
            tester.write_baseline(summary, baseline)
            print(f"baseline {_display(baseline)}: {summary.total} cases")
            return 1 if summary.failed else 0
        regressions = tester.compare_baseline(summary, baseline) if baseline else []
    except tester.SuiteError as error:
        print(f"logos: test: {error}", file=sys.stderr)
        return 1

    if baseline is not None and baseline.is_file():
        for case, detail in regressions:
            print(f"BASELINE {case} {detail}")
        print(f"baseline {_display(baseline)}: {len(regressions)} regression(s)")
    else:
        print("baseline: none")
    return 1 if summary.failed or regressions else 0


def _display(path: Path) -> str:
    """A path as it reads from the working directory, or in full."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _build(source: str, out: str | None, opt: str) -> int:
    """Compile one file and report a failure the way every other pass does.

    The IR is always kept: it is the artefact a person reads when a program
    misbehaves, so ``--keep-ir`` is accepted but changes nothing today.
    """
    try:
        driver.build(source, out, opt=opt, keep_ir=True)
    except driver.BuildError as error:
        print(f"logos: {error}", file=sys.stderr)
        return 1
    except LogosError as error:
        print(error.render(), file=sys.stderr)
        return 1
    return 0


def _fmt(arguments: list[str]) -> int:
    """Rewrite each named file in place, or print stdin's canonical form.

    ``-`` reads stdin once and writes the result to stdout; a repeated ``-`` is
    an argument error (exit 2), because there is only one stdin.
    """
    if arguments.count("-") > 1:
        print("logos: fmt: '-' may be given at most once", file=sys.stderr)
        return 2
    status = 0
    for argument in arguments:
        if argument == "-":
            status |= _format_stdin()
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
    try:
        canonical = render(parse(text, str(path)))
    except LogosError as error:
        print(error.render(), file=sys.stderr)
        return 1
    try:
        path.write_text(canonical, encoding="utf-8")
    except OSError as error:
        print(f"logos: {path}: {error.strerror}", file=sys.stderr)
        return 1
    return 0


def _format_stdin() -> int:
    try:
        canonical = render(parse(sys.stdin.read(), "<stdin>"))
    except LogosError as error:
        print(error.render(), file=sys.stderr)
        return 1
    sys.stdout.write(canonical)
    return 0


def _load(source: str) -> "tuple[Node | None, int]":
    """Parse ``source``, printing any read or parse failure the usual way.

    Returns ``(tree, 0)`` on success and ``(None, 1)`` after writing one line
    to stderr, so ``show`` and ``patch`` report a bad file exactly as ``fmt``
    does rather than raising out of ``main``.
    """
    try:
        text = Path(source).read_text(encoding="utf-8")
    except OSError as error:
        print(f"logos: {source}: {error.strerror}", file=sys.stderr)
        return None, 1
    try:
        return parse(text, source), 0
    except LogosError as error:
        print(error.render(), file=sys.stderr)
        return None, 1


def _show(source: str, path: str) -> int:
    """Print the canonical form of the one node at ``path``."""
    root, status = _load(source)
    if root is None:
        return status
    target = find(root, path)
    if target is None:
        print(
            f"logos: show: no node at path '{path}' in {source}",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(render(target))
    return 0


def _patch(source: str, path: str) -> int:
    """Replace the node at ``path`` with the one form read from stdin.

    The file is parsed and the path is checked before stdin is read, so a bad
    path is reported without waiting on input. The replacement keeps its new
    shape and then every path in the file is assigned again from scratch, so a
    later ``show`` or ``patch`` addresses the result by its new positions.
    """
    root, status = _load(source)
    if root is None:
        return status
    target = find(root, path)
    if target is None:
        print(
            f"logos: patch: no node at path '{path}' in {source}",
            file=sys.stderr,
        )
        return 1
    try:
        replacement = parse(sys.stdin.read(), "<stdin>")
    except LogosError as error:
        print(error.render(), file=sys.stderr)
        return 1
    forms = [item for item in replacement.items if isinstance(item, Node)]
    if len(replacement.items) != 1 or len(forms) != 1:
        print(
            "logos: patch: stdin must hold exactly one form",
            file=sys.stderr,
        )
        return 1
    new = forms[0]
    target.kind = new.kind
    target.items = new.items
    target.line = new.line
    target.col = new.col
    assign_paths(root)
    try:
        Path(source).write_text(render(root), encoding="utf-8")
    except OSError as error:
        print(f"logos: {source}: {error.strerror}", file=sys.stderr)
        return 1
    return 0


def run() -> int:
    """Console script wrapper used by ``logosc``."""
    return main(sys.argv[1:])
