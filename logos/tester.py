"""The GCC-style test harness: directives in, one status line per case out.

A test is a grammar node, so the corpus needs no second language. Each ``(test
name ...)`` body carries one ``(expect ...)`` directive with the clauses
``(out S)``, ``(code N)``, ``(leaks N)`` and ``(err-contains S)``; a negative
corpus file carries a module-level ``(expect-error E0xxx)`` instead. This module
reads those nodes and makes them assertions.

Layout
------
Categories are directories under ``tests/``: ``parse``, ``type``, ``exec``,
``mem``, ``thread``, ``generics``, ``regress`` and ``diff``. A case id is
``<category>/<file>/<test>`` for a ``(test ...)`` node and
``<category>/<file>`` for a file that expects a diagnostic. File is the path
under the category without ``.logos``; the category is found from the nearest
ancestor directory named in :data:`CATEGORIES`, so a temporary directory used
by the harness's own tests behaves like ``exec``.

The negative fixtures written before the directive existed carry their code in
a sibling ``.expect`` file. Both spellings are read; when a file has both and
they disagree, that disagreement is reported rather than resolved.

Execution
---------
A test body becomes ``main`` through :func:`logos.driver.run_test`, which is a
debug build: its exit reports every live block, so a leak is visible. The
runner executes the binary with a timeout, counts stderr lines that begin
``LEAK `` and compares stdout, the exit status, that count and the
``err-contains`` substring. Every case runs once per optimisation level in
``opts`` (``-O0`` and ``-O2`` by default); a case passes only if the directive
holds at every level, and a failure names the level that disagreed.

The levels are also compared with each other: the same program built at
``-O0`` and ``-O2`` must print the same bytes, exit the same way and write the
same stderr. Comparing only against the directive would accept two different
wrong answers, so a disagreement between the runs is a failure of the case in
its own right, and the report names the fields that differ.

A disagreement is never repaired. The fixture's directive is the human's
statement of intent, so the runner reports both values and leaves the file
alone.

Baseline
--------
:func:`write_baseline` writes ``<suite root>/summary.txt``: a ``LEAKS N``
line with the suite's total leak count, an ``OPTS -O0 -O2`` line naming the
levels that total came from, then one line per case,
``<status> <encoded category>/<case> <detail>``, with statuses ``PASS`` and
``FAIL``. Case ids are percent-encoded so a path containing a space cannot
split a line. A full run replaces the baseline; a filtered run merges into it,
rewriting the cases it ran and dropping a stale line under a category it
covered in full. :func:`compare_baseline` reports a case that passed before and
fails now, a case in the baseline that a full run no longer contains (even when
its whole category directory went away), a new case with no baseline result,
and a full run whose total leak count exceeds the recorded one at the same
levels. A baseline that predates the ``OPTS`` line is refused rather than
compared, so an old file cannot switch the leak gate off by lacking a header;
regenerating it with ``--update-baseline`` writes the header. The suite root
keeps its own baseline, so the repository's is ``tests/summary.txt``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

from logos import driver
from logos.check import check_module
from logos.diag import LogosError
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.parser import parse

#: The categories, in the order the suite runs them. The order is fixed so a
#: run is byte-for-byte reproducible; the files inside a category are sorted.
CATEGORIES = ("parse", "type", "exec", "mem", "thread", "generics", "regress", "diff")

#: The prefix of the baseline line that records the whole suite's leak total.
LEAKS = "LEAKS "

#: The prefix of the baseline line that records the optimisation levels the
#: total came from, ``OPTS -O0 -O2``. One level's total and two levels' total
#: count different numbers of executions, so they are only comparable when the
#: recorded levels match the run's.
LEVELS = "OPTS "

#: The levels every case runs at unless the caller asks for a subset. The
#: specification requires the whole corpus at both, so both is the default.
OPTS = ("-O0", "-O2")

#: How long one binary may run before it is killed and reported as a failure.
TIMEOUT = 10.0

#: A directive value that names a compiler diagnostic.
CODE = re.compile(r"^E[0-9]{4}$")

#: A module-level ``(expect-error E0xxx)``, read from raw text only when the
#: parser refused the file and there is no tree to walk.
EXPECT_ERROR = re.compile(r"\(\s*expect-error\s+(E[0-9]{4})\s*\)")

#: A clause value that must be an integer literal, not a float or an atom.
INTEGER = re.compile(r"^[+-]?[0-9]+$")

#: An optimisation level as the command line spells it: ``-O0``, ``-O2``.
OPT_LEVEL = re.compile(r"^-O[0-9]+$")


class SuiteError(Exception):
    """The suite itself could not be read: for example, the path is missing.

    A broken input the caller asked to run is a failing run, not a usage
    error, so this exits 1 and names the path it could not read.
    """


class UsageError(ValueError):
    """The command line asked for something the suite cannot do (exit 2)."""


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's outcome, in the order it will be printed and baselined."""

    id: str
    status: str
    detail: str = "-"


@dataclass(slots=True)
class Summary:
    """What one suite run did.

    ``leaks`` is the number of ``LEAK`` lines every execution in the run
    printed, summed. A failing case's leaks are included: they are what the
    program did, and a count that hid them would be a worse report.
    """

    passed: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    leaks: int = 0
    cases: list[CaseResult] = field(default_factory=list)
    categories: tuple[str, ...] = ()
    #: The optimisation levels this run executed, in the order it ran them.
    #: The baseline records them beside its leak total, because a total taken
    #: at one level is not comparable with a total taken at two.
    opts: tuple[str, ...] = ()
    #: Categories whose whole directory this run covered. A filtered update
    #: may drop a baseline case under one of these that produced no result:
    #: the category ran, and the case is gone. A category the run saw only
    #: part of (a single file, a nested directory) is never in this set.
    covered: tuple[str, ...] = ()
    #: True when the run covered a whole suite (no ``--only``, a directory
    #: root that is not itself one category). A full run is what the baseline
    #: records, so it is also what may report a deleted category missing and
    #: gate on the leak total.
    full: bool = False
    #: ``.logos`` files under the root that no known category claimed. A typo
    #: in a category directory name must not hide a whole file, so each is
    #: reported and any makes the run fail.
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """How many cases the run contained."""
        return len(self.cases)


@dataclass(frozen=True, slots=True)
class Expectation:
    """The clauses one ``(expect ...)`` directive asked for."""

    out: str | None = None
    code: int | None = None
    leaks: int | None = None
    err_contains: str | None = None


class DirectiveError(Exception):
    """A directive is malformed; the case fails with this sentence."""


def run_suite(
    root: str,
    only: str | None = None,
    opts: list[str] | None = None,
    *,
    timeout: float = TIMEOUT,
) -> Summary:
    """Run every case under ``root`` and return what happened.

    ``root`` is a file, a category directory or the ``tests`` directory.
    ``only`` narrows the run to one category. ``opts`` lists the optimisation
    levels; ``None`` or an empty list means :data:`OPTS`. ``timeout`` is the
    seconds one binary may run, and is a keyword so the harness's own tests can
    prove a hang fails without waiting ten seconds.
    """
    if only is not None and only not in CATEGORIES:
        raise UsageError(f"unknown test category '{only}'")
    levels = list(opts) if opts else list(OPTS)
    summary = Summary()
    root_path = Path(root)
    files, skipped = _discover(root_path, only)
    summary.categories = tuple(sorted({_category(path) for path in files}))
    summary.covered = _covered(root_path, only)
    summary.opts = tuple(levels)
    summary.skipped = skipped
    summary.full = (
        only is None
        and root_path.is_dir()
        and root_path.name not in CATEGORIES
    )
    for path in files:
        _run_file(path, levels, summary, timeout)
    return summary


def _discover(root: Path, only: str | None) -> tuple[list[Path], list[str]]:
    """Every ``.logos`` file in scope, plus the ones no category claims.

    A ``tests``-style root is walked category by category so its files run in
    the documented order. A file under such a root that sits outside every
    known category is returned as skipped: it is not silently dropped, because
    a misspelt directory name would otherwise hide a whole fixture. Any other
    directory is walked whole and sorted; the category of each file still comes
    from its path, so a nested layout keeps working.
    """
    if not root.exists():
        raise SuiteError(f"{root}: no such file or directory")
    if root.is_file():
        files = [root]
        skipped: list[Path] = []
    elif any((root / category).is_dir() for category in CATEGORIES):
        files = []
        for category in CATEGORIES:
            directory = root / category
            if directory.is_dir():
                files.extend(sorted(directory.rglob("*.logos")))
        claimed = set(files)
        skipped = [path for path in sorted(root.rglob("*.logos")) if path not in claimed]
    else:
        files = sorted(root.rglob("*.logos"))
        skipped = []
    if only is not None:
        files = [path for path in files if _category(path) == only]
    return files, [str(path) for path in skipped]


def _category(path: Path) -> str:
    """The category of ``path``: its nearest ancestor in :data:`CATEGORIES`.

    A fixture outside the corpus (a temporary directory in a test) is runnable
    by definition, so it is treated as ``exec``.
    """
    for parent in path.parents:
        if parent.name in CATEGORIES:
            return parent.name
    return "exec"


def _category_dir(path: Path) -> Path:
    """The directory that gives ``path`` its category, or the file's parent."""
    for parent in path.parents:
        if parent.name in CATEGORIES:
            return parent
    return path.parent


def _covered(root: Path, only: str | None) -> tuple[str, ...]:
    """The categories whose whole directory this run walked.

    A suite root contains every category it knows; ``--only`` narrows that to
    one. A category directory is the whole of that category. Anything else — a
    single file, a nested directory, a directory with no category inside —
    covers no category in full, so a merged update keeps every line it did not
    see rather than deleting one on a guess.
    """
    if not root.is_dir():
        return ()
    if root.name in CATEGORIES:
        return (root.name,) if only in (None, root.name) else ()
    present = tuple(category for category in CATEGORIES if (root / category).is_dir())
    if only is None:
        return present
    return (only,) if only in present else ()


def _run_file(path: Path, opts: list[str], summary: Summary, timeout: float) -> None:
    """Parse one fixture and run every case it declares."""
    category = _category(path)
    base = _category_dir(path)
    relative = path.relative_to(base).with_suffix("").as_posix()
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        _record(
            summary,
            f"{category}/{relative}",
            f"cannot read {path}: not UTF-8 ({error.reason} at byte {error.start})",
        )
        return
    except OSError as error:
        _record(summary, f"{category}/{relative}", f"cannot read {path}: {error.strerror}")
        return
    try:
        root = parse(source, str(path))
    except LogosError as error:
        _run_unparsable(path, category, relative, error, summary)
        return

    expected, problem = _expected_diagnostic(path, root)
    if problem is not None:
        _record(summary, f"{category}/{relative}", problem)
        return
    if expected is not None:
        _run_negative(path, category, relative, expected, summary)
        return

    tests = [
        item for item in root.items if isinstance(item, Node) and item.kind == "test"
    ]
    if not tests:
        # A file with no test and no expected diagnostic declares no case. It
        # is not silently "passing": a suite that contains none of them is
        # reported as an empty run and exits 1. This is also what lets a
        # category such as ``diff`` hold programs whose oracle is a C twin
        # rather than a directive.
        return
    for index, test in enumerate(tests):
        name = test.name or f"unnamed-{index}"
        _run_case(
            path,
            category,
            relative,
            test,
            name,
            index,
            opts,
            summary,
            timeout,
        )


def _run_unparsable(
    path: Path,
    category: str,
    relative: str,
    error: LogosError,
    summary: Summary,
) -> None:
    """A file the parser refused: pass only if it expected that exact code.

    The tree does not exist, so a module-level directive is read from the raw
    text and the ``.expect`` sidecar is honoured as the corpus's other spelling.
    A file that uses both must agree with itself, exactly as it must when a tree
    exists.
    """
    directive, problem = _expected_in_text(path)
    sidecar, sidecar_problem = _expected_from_sidecar(path)
    if problem is not None:
        _record(summary, f"{category}/{relative}", problem)
        return
    if sidecar_problem is not None:
        _record(summary, f"{category}/{relative}", sidecar_problem)
        return
    if directive is not None and sidecar is not None and directive != sidecar:
        _record(
            summary,
            f"{category}/{relative}",
            f"expect-error {directive} disagrees with "
            f"{path.with_suffix('.expect').name} {sidecar}",
        )
        return
    expected = directive if directive is not None else sidecar
    if expected is None:
        _record(summary, f"{category}/{relative}", f"parse failed: {error.render()}")
        return
    if expected == error.code:
        _record(summary, f"{category}/{relative}", None)
    else:
        _record(
            summary,
            f"{category}/{relative}",
            f"expected {expected}, got {error.code}: {error.render()}",
        )


def _expected_in_text(path: Path) -> tuple[str | None, str | None]:
    """Read the expected code from a file the parser could not turn into a tree."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return None, f"cannot read {path}: not UTF-8 ({error.reason} at byte {error.start})"
    except OSError as error:
        return None, f"cannot read {path}: {error.strerror}"
    codes = set(EXPECT_ERROR.findall(source))
    if len(codes) > 1:
        return None, "two expect-error directives: " + " and ".join(sorted(codes))
    return (codes.pop() if codes else None), None


def _expected_diagnostic(path: Path, root: Node) -> tuple[str | None, str | None]:
    """The code a negative fixture expects, its ``.expect`` sidecar, or none.

    A source directive and a sidecar are both statements of intent. When they
    exist they must agree: a disagreement is reported rather than resolved in
    the harness's favour.
    """
    directive = None
    for item in root.items:
        if not (isinstance(item, Node) and item.kind == "expect-error"):
            continue
        try:
            value = _atom(item, "expect-error")
        except DirectiveError as error:
            return None, str(error)
        if not CODE.match(value):
            return None, f"expect-error needs a code like E0101, found '{value}'"
        if directive is not None and directive != value:
            return None, f"two expect-error directives: {directive} and {value}"
        directive = value
    sidecar, problem = _expected_from_sidecar(path)
    if problem is not None:
        return None, problem
    if directive is not None and sidecar is not None and directive != sidecar:
        return None, (
            f"expect-error {directive} disagrees with "
            f"{path.with_suffix('.expect').name} {sidecar}"
        )
    return directive if directive is not None else sidecar, None


def _expected_from_sidecar(path: Path) -> tuple[str | None, str | None]:
    """Read the legacy ``<file>.expect`` code beside a fixture, if it exists."""
    sidecar = path.with_suffix(".expect")
    if not sidecar.is_file():
        return None, None
    try:
        text = sidecar.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        return None, (
            f"cannot read {sidecar.name}: not UTF-8 ({error.reason} at byte {error.start})"
        )
    except OSError as error:
        return None, f"cannot read {sidecar.name}: {error.strerror}"
    if "\n" in text:
        return None, f"{sidecar.name} holds more than one line"
    if not CODE.match(text):
        return None, f"{sidecar.name} needs a code like E0101, found '{text}'"
    return text, None


def _run_negative(
    path: Path, category: str, relative: str, expected: str, summary: Summary
) -> None:
    """A file that must fail the checker with ``expected`` and no other code."""
    case_id = f"{category}/{relative}"
    try:
        check_module(driver.load(str(path)))
    except LogosError as error:
        if error.code == expected:
            _record(summary, case_id, None)
        else:
            _record(summary, case_id, f"expected {expected}, got {error.code}: {error.render()}")
        return
    except (driver.BuildError, OSError) as error:
        _record(summary, case_id, f"build failed: {_one_line(str(error))}")
        return
    _record(summary, case_id, f"expected {expected}, but the module checked")


def _run_case(
    path: Path,
    category: str,
    relative: str,
    test: Node,
    name: str,
    index: int,
    opts: list[str],
    summary: Summary,
    timeout: float,
) -> None:
    """Run one ``(test ...)`` node at every level and record one outcome."""
    case_id = f"{category}/{relative}/{name}"
    try:
        expectation, leak_directive = _expectation(test)
    except DirectiveError as error:
        _record(summary, case_id, str(error))
        return
    if expectation is None:
        _record(summary, case_id, "test has no (expect ...) directive")
        return
    if leak_directive is not None:
        if expectation.leaks is not None:
            _record(summary, case_id, "both (leaks ...) and (expect-leak ...) declare a count")
            return
        expectation = Expectation(
            out=expectation.out,
            code=expectation.code,
            leaks=leak_directive,
            err_contains=expectation.err_contains,
        )

    problems: list[str] = []
    observed: list[tuple[str, tuple[str, str, int]]] = []
    for opt in opts:
        out = _binary_path(category, relative, name, index, opt)
        failed, seen = _execute(path, name, out, opt, expectation, summary, timeout)
        problems.extend(failed)
        if seen is not None:
            observed.append((opt, seen))
    if len(observed) > 1:
        first_opt, first = observed[0]
        for other_opt, other in observed[1:]:
            disagreement = _disagreement(first_opt, first, other_opt, other)
            if disagreement is not None:
                problems.append(disagreement)
    _record(summary, case_id, "; ".join(problems) if problems else None)


def _binary_path(category: str, relative: str, name: str, index: int, opt: str) -> Path:
    """Where one case's binary goes: ``build/harness/<case>/<opt>``.

    Both levels and every rerun write different paths, so a cached binary can
    never answer for the build that was just asked for. The index keeps two
    tests that sanitise to the same filename apart.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    level = re.sub(r"[^A-Za-z0-9]", "", opt) or "O0"
    return driver.BUILD_DIR / "harness" / category / relative / f"{index:02d}-{safe}.{level}.bin"


def _execute(
    path: Path,
    name: str,
    out: Path,
    opt: str,
    expectation: Expectation,
    summary: Summary,
    timeout: float,
) -> tuple[list[str], tuple[str, str, int] | None]:
    """Build and run one case at one level; return its problems and its run.

    The observation is ``(stdout, stderr, exit code)``, kept so the caller can
    compare the optimisation levels with each other. A case that did not run
    has no observation: there is nothing to compare.
    """
    try:
        binary = driver.run_test(str(path), name, str(out), opt=opt)
    except (driver.BuildError, LogosError, OSError) as error:
        return [f"{opt}: build failed: {_one_line(_message(error))}"], None
    try:
        done = subprocess.run(
            [binary],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [f"{opt}: timed out after {timeout:g}s"], None
    except OSError as error:
        return [f"{opt}: could not run: {_one_line(str(error))}"], None

    leaks = sum(1 for line in done.stderr.splitlines() if line.startswith("LEAK "))
    summary.leaks += leaks
    problems = _compare(expectation, done.stdout, done.stderr, done.returncode, leaks)
    seen = (done.stdout, done.stderr, done.returncode)
    return [f"{opt}: {problem}" for problem in problems], seen


def _disagreement(
    first_opt: str,
    first: tuple[str, str, int],
    other_opt: str,
    other: tuple[str, str, int],
) -> str | None:
    """One line naming every way two optimisation levels disagree.

    Optimisation parity is a test in its own right: the same program built at
    ``-O0`` and ``-O2`` must print the same bytes, exit the same way and write
    the same stderr, or the compiler has changed meaning while optimising.
    Comparing only against the directive would accept two different wrong
    answers, so the comparison is explicit and names the fields that differ.
    """
    out_one, err_one, code_one = first
    out_other, err_other, code_other = other
    parts: list[str] = []
    if out_one != out_other:
        parts.append(f"stdout {out_one!r} vs {out_other!r}")
    if code_one != code_other:
        parts.append(f"exit code {code_one} vs {code_other}")
    if err_one != err_other:
        parts.append(f"stderr {err_one!r} vs {err_other!r}")
    if not parts:
        return None
    return f"{first_opt} and {other_opt} disagree: " + ", ".join(parts)


def _compare(
    expectation: Expectation,
    out: str,
    err: str,
    code: int,
    leaks: int,
) -> list[str]:
    """Every clause the run did not satisfy, in directive order.

    Both values are always shown: a report that says only 'stdout differed'
    makes the reader run the program again to learn what it printed.
    """
    problems: list[str] = []
    if expectation.out is not None and out != expectation.out:
        problems.append(f"stdout: expected {expectation.out!r}, got {out!r}")
    if expectation.code is not None and code != expectation.code:
        problems.append(f"exit code: expected {expectation.code}, got {code}")
    if expectation.leaks is not None and leaks != expectation.leaks:
        problems.append(f"leaks: expected {expectation.leaks}, got {leaks}")
    if expectation.err_contains is not None and expectation.err_contains not in err:
        problems.append(
            f"stderr: expected to contain {expectation.err_contains!r}, got {err!r}"
        )
    return problems


def expectation_of(test: Node) -> tuple[Expectation | None, int | None]:
    """Read one test's directives for a caller outside the runner.

    The generator of the manual embeds the same ``(expect ...)`` the runner
    compares against, so both read it here rather than in two places that could
    drift. A malformed directive raises :class:`DirectiveError`.
    """
    return _expectation(test)


def _expectation(test: Node) -> tuple[Expectation | None, int | None]:
    """Read one test's directives, or raise :class:`DirectiveError`.

    Returns the expectation and a count from a legacy ``(expect-leak N)``
    sibling, which the language still accepts as a directive node.
    """
    directives = [
        item for item in test.items if isinstance(item, Node) and item.kind == "expect"
    ]
    if len(directives) > 1:
        raise DirectiveError(f"test has {len(directives)} (expect ...) directives, wanted one")
    legacy = [
        item
        for item in test.items
        if isinstance(item, Node) and item.kind == "expect-leak"
    ]
    if len(legacy) > 1:
        raise DirectiveError(f"test has {len(legacy)} (expect-leak ...) directives, wanted one")
    leak_directive = _count(legacy[0], "expect-leak") if legacy else None
    if not directives:
        return None, leak_directive
    return _clauses(directives[0]), leak_directive


def _clauses(node: Node) -> Expectation:
    """Decode ``(expect (out S) (code N) (leaks N) (err-contains S))``."""
    values: dict[str, object] = {}
    for clause in node.items[1:]:
        if not isinstance(clause, Node):
            raise DirectiveError("expect takes clauses, not atoms")
        if clause.kind in values:
            raise DirectiveError(f"duplicate directive '({clause.kind} ...)'")
        arguments = clause.items[1:]
        if len(arguments) != 1:
            raise DirectiveError(f"({clause.kind} ...) takes one value")
        argument = arguments[0]
        if clause.kind in ("out", "err-contains"):
            text = _string(clause, argument)
            if clause.kind == "err-contains" and text == "":
                raise DirectiveError("(err-contains ...) needs a non-empty string")
            values[clause.kind] = text
        elif clause.kind in ("code", "leaks"):
            values[clause.kind] = _integer(clause, argument)
        else:
            raise DirectiveError(f"unknown directive '({clause.kind} ...)'")
    if not values:
        raise DirectiveError("expect needs at least one clause")
    return Expectation(
        out=values.get("out"),
        code=values.get("code"),
        leaks=values.get("leaks"),
        err_contains=values.get("err-contains"),
    )


def _string(node: Node, value: object) -> str:
    if not (isinstance(value, Token) and value.kind == STRING):
        raise DirectiveError(f"({node.kind} ...) needs a string")
    return value.value


def _integer(node: Node, value: object) -> int:
    if not (isinstance(value, Token) and value.kind == NUMBER and INTEGER.match(value.value)):
        raise DirectiveError(f"({node.kind} ...) needs an integer")
    return int(value.value)


def _count(node: Node, kind: str) -> int:
    arguments = node.items[1:]
    if len(arguments) != 1:
        raise DirectiveError(f"({kind} ...) takes one value")
    return _integer(node, arguments[0])


def _atom(node: Node, kind: str) -> str:
    arguments = node.items[1:]
    if len(arguments) != 1 or not (
        isinstance(arguments[0], Token) and arguments[0].kind == ATOM
    ):
        raise DirectiveError(f"({kind} ...) takes one code")
    return arguments[0].value


def _record(summary: Summary, case_id: str, problem: str | None) -> None:
    """Add one case outcome to the summary."""
    if problem is None:
        summary.passed += 1
        summary.cases.append(CaseResult(case_id, "PASS"))
        return
    detail = _one_line(problem)
    summary.failed += 1
    summary.failures.append((case_id, detail))
    summary.cases.append(CaseResult(case_id, "FAIL", detail))


def _one_line(text: str) -> str:
    """Collapse a report to one baseline-safe line, keeping it readable."""
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if len(text) > 240:
        text = text[:237] + "..."
    return text


def _message(error: BaseException) -> str:
    """One sentence from a diagnostic or a build failure."""
    if isinstance(error, LogosError):
        return error.render()
    return str(error)


def baseline_path(root: str | Path) -> Path | None:
    """The baseline a suite root owns, or ``None`` for a single file.

    A file has no suite to baseline, so ``--update-baseline`` on one is an
    argument error rather than a file written beside the source.
    """
    path = Path(root)
    if path.is_file():
        return None
    return path / "summary.txt"


@dataclass(frozen=True, slots=True)
class _Baseline:
    """What a baseline file records: one line per case, and two headers."""

    records: list[tuple[str, str, str]]
    total: int | None
    #: The levels the total was taken at, or ``None`` when the file predates
    #: the header. A total with no levels cannot be compared with any run, so
    #: :func:`compare_baseline` refuses such a file instead of ungating it.
    opts: tuple[str, ...] | None


def write_baseline(summary: Summary, path: Path) -> None:
    """Write the baseline, merging a filtered run into what is already there.

    A full run replaces the baseline: it is the record of the whole suite, so
    it is also what drops a case that no longer exists, what records the
    suite's leak total, and what records the levels the total came from. A
    filtered run only knows the cases it ran, so it rewrites those lines and
    keeps every other line and both headers untouched; updating one category
    must never delete another's history.

    The one exception is the filter's own ground: a category the run covered in
    full but from which no case came back has lost that case, so its stale
    line is dropped. Without that, deleting a fixture and merging the category
    it lived in would leave a record the next full run reports as missing.
    """
    previous = None if summary.full else _read_baseline(path)
    lines: list[str] = []
    if summary.full:
        total = summary.leaks
        levels = summary.opts
        lines = [_format_case(case.id, case.status, case.detail) for case in summary.cases]
    else:
        records = previous.records if previous is not None else []
        total = previous.total if previous is not None else None
        levels = previous.opts if previous is not None else None
        current = {case.id: case for case in summary.cases}
        known = {case_id for _, case_id, _ in records}
        covered = set(summary.covered)
        for status, case_id, detail in records:
            case = current.get(case_id)
            if case is None:
                if case_id.split("/", 1)[0] in covered:
                    continue
                lines.append(f"{status} {_encode_id(case_id)} {detail}")
            else:
                lines.append(_format_case(case.id, case.status, case.detail))
        for case in summary.cases:
            if case.id not in known:
                lines.append(_format_case(case.id, case.status, case.detail))
    header: list[str] = []
    if total is not None:
        header.append(f"{LEAKS}{total}")
    if levels:
        header.append(f"{LEVELS}{' '.join(levels)}")
    lines = header + lines
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def read_baseline(path: Path) -> dict[str, str] | None:
    """Read a baseline into ``{case id: status}``, or ``None`` if absent.

    The header lines are not cases and are omitted here;
    :func:`compare_baseline` reads them separately.
    """
    baseline = _read_baseline(path)
    if baseline is None:
        return None
    return {case_id: status for status, case_id, _ in baseline.records}


def _read_baseline(path: Path) -> _Baseline | None:
    """Parse the baseline's case records, its total and its levels.

    A line the harness cannot read is a broken input, not a usage error: it
    raises :class:`SuiteError` naming the file and line, which the caller
    reports as exit 1. When that happens no case results are trusted, but the
    cases have already run and been printed.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SuiteError(
            f"{path}: baseline is not UTF-8 ({error.reason} at byte {error.start})"
        ) from None
    except OSError as error:
        raise SuiteError(f"{path}: cannot read baseline: {error.strerror}") from None
    records: list[tuple[str, str, str]] = []
    total: int | None = None
    opts: tuple[str, ...] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith(LEAKS):
            value = line[len(LEAKS) :].strip()
            if not value.isdigit():
                raise SuiteError(
                    f"{path}:{number}: leak total needs a number, found '{value}'"
                )
            total = int(value)
            continue
        if line.startswith(LEVELS):
            values = line[len(LEVELS) :].split()
            if not values or not all(OPT_LEVEL.match(value) for value in values):
                raise SuiteError(
                    f"{path}:{number}: levels must be spellings like -O0, "
                    f"found '{line[len(LEVELS) :].strip()}'"
                )
            opts = tuple(values)
            continue
        fields = line.split(" ", 2)
        if len(fields) < 2:
            raise SuiteError(f"{path}:{number}: baseline line needs a status and a case")
        status, encoded = fields[0], fields[1]
        if status not in ("PASS", "FAIL"):
            raise SuiteError(
                f"{path}:{number}: baseline status must be PASS or FAIL, found '{status}'"
            )
        records.append((status, _decode_id(encoded), fields[2] if len(fields) > 2 else "-"))
    return _Baseline(records, total, opts)


def _format_case(case_id: str, status: str, detail: str) -> str:
    """One baseline line; the id is percent-encoded so a space cannot split it."""
    return f"{status} {_encode_id(case_id)} {_one_line(detail)}"


def _encode_id(case_id: str) -> str:
    """A case id made space-free for the baseline, keeping the category slash."""
    return quote(case_id, safe="/._-")


def _decode_id(text: str) -> str:
    """The inverse of :func:`_encode_id`."""
    return unquote(text)


def compare_baseline(summary: Summary, path: Path) -> list[tuple[str, str]]:
    """Every disagreement between this run and the baseline, sorted by case.

    Three things count as a regression: a case the baseline passed that now
    fails, a passing case the run no longer contains, and a case with no
    baseline result at all. The last is not a complaint about the case; it is
    the instruction that a new test must have a recorded result before the
    suite is green.

    The baseline is the record of the whole suite, so a full run also reports
    a case missing even when its whole category directory was deleted. A
    filtered run is only responsible for the categories it ran.

    A full run whose total leak count exceeds the baseline's is a regression
    too: a leak that no individual ``(leaks N)`` clause names must not keep the
    suite green. The problem names both totals. The totals only compare when
    the recorded levels and the run's levels are the same, because one level
    runs half as many executions as two and its total is a different number for
    an identical suite.

    A baseline with no ``OPTS`` header cannot be compared at all, so it is
    refused rather than silently ungated: an old file must not be able to
    switch off the leak total by lacking a line. The refusal tells the reader
    to regenerate the baseline, which a full ``--update-baseline`` run does.
    """
    baseline = _read_baseline(path)
    if baseline is None:
        return []
    if baseline.opts is None:
        raise SuiteError(
            f"{path}: baseline has no OPTS header, so its leak total cannot be "
            "compared with this run; regenerate it with --update-baseline"
        )
    total = baseline.total
    previous = {case_id: status for status, case_id, _ in baseline.records}
    current = {case.id: case for case in summary.cases}
    scope = set(summary.categories)
    problems: list[tuple[str, str]] = []
    for case in summary.cases:
        status = previous.get(case.id)
        if status is None:
            problems.append((case.id, "new case has no baseline result"))
        elif status == "PASS" and case.status == "FAIL":
            problems.append(
                (case.id, f"regression: passed in baseline, now fails: {case.detail}")
            )
    for case_id, status in previous.items():
        category = case_id.split("/", 1)[0]
        if status != "PASS" or case_id in current:
            continue
        if not (summary.full or not scope or category in scope):
            continue
        problems.append((case_id, "regression: passed in baseline, missing from this run"))
    if (
        summary.full
        and total is not None
        and baseline.opts is not None
        and baseline.opts == summary.opts
        and summary.leaks > total
    ):
        problems.append(
            ("suite/leaks", f"regression: leak total {summary.leaks} exceeds baseline {total}")
        )
    return sorted(problems)
