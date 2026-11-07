"""The directive-driven test harness: one failing expectation is a failure.

The harness is the thing that makes every ``(expect ...)`` in the corpus a real
assertion, so its own tests are about the ways an expectation can disagree with
a binary: stdout, exit status, leak count, stderr text, a hang, a malformed
directive, and a negative case that stops being negative. The last class pins
the baseline: a case that passed before and fails now is a regression, and a
new case without a baseline result is one too.

Every fixture here is temporary, built by the real driver through clang. That
is deliberate: a harness that only agreed with a fake binary would prove
nothing about the corpus it exists to check.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from logos import tester
from logos.cli import main

ROOT = Path(__file__).resolve().parents[1]

HELLO = """\
(mod hello)

(test prints
  (expect (out "hi\\n") (code 0))
  (println "hi"))
"""

BROKEN = """\
(mod wrong)

(test prints
  (expect (out "expected\\n") (code 0))
  (println "observed"))
"""

BROKEN_HELLO = """\
(mod hello)

(test prints
  (expect (out "expected\\n") (code 0))
  (println "observed"))
"""

FINE = """\
(mod fine)

(test frees
  (expect (code 0) (leaks 0))
  (let p (alloc :i64 4))
  (free p))
"""

LEAKY = """\
(mod leaky)

(test leaks
  (expect (code 0) (leaks 1))
  (let p (alloc :i64 4)))
"""


def write(root: Path, category: str, name: str, source: str) -> Path:
    """Write ``source`` to ``root/category/name.logos`` and return the path."""
    directory = root / category
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.logos"
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


def run(root: Path, opts: list[str] | None = None, **kwargs):
    """Run one temporary suite at ``-O0`` unless a test asks for more."""
    return tester.run_suite(str(root), None, opts or ["-O0"], **kwargs)


class DirectiveTest(unittest.TestCase):
    def test_a_matching_expectation_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)
            self.assertEqual(summary.failed, 0)
            self.assertEqual(summary.failures, [])
            self.assertEqual(summary.cases[0].id, "exec/hello/prints")
            self.assertEqual(summary.cases[0].status, "PASS")

    def test_a_wrong_expectation_is_one_failure_and_names_both_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "wrong", BROKEN)
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 0)
            self.assertEqual(summary.failed, 1)
            case, detail = summary.failures[0]
            self.assertEqual(case, "exec/wrong/prints")
            self.assertIn("expected", detail)
            self.assertIn("observed", detail)

    def test_a_wrong_exit_code_names_both_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "code",
                """\
                (mod code)

                (test exits
                  (expect (code 3))
                  (println "y"))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("expected 3, got 0", summary.failures[0][1])

    def test_leaks_are_counted_from_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "mem",
                "leak",
                """\
                (mod leak)

                (test one-block
                  (expect (code 0) (leaks 1))
                  (let p (alloc :i64 4)))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)
            self.assertEqual(summary.leaks, 1)

    def test_a_leak_count_that_disagrees_reports_both_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "mem",
                "leak",
                """\
                (mod leak)

                (test one-block
                  (expect (code 0) (leaks 0))
                  (let p (alloc :i64 4)))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("leaks: expected 0, got 1", summary.failures[0][1])

    def test_err_contains_is_a_substring_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "divzero",
                """\
                (mod divzero)

                (fn zero :i64 ()
                  (let z :i64 0)
                  (ret z))

                (test panics
                  (expect (code 2) (err-contains "R0001: division by zero"))
                  (let q :i64 (/ 1 (zero))))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)

    def test_a_missing_err_contains_reports_the_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "divzero",
                """\
                (mod divzero)

                (fn zero :i64 ()
                  (let z :i64 0)
                  (ret z))

                (test panics
                  (expect (code 2) (err-contains "R0009: not the panic"))
                  (let q :i64 (/ 1 (zero))))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("R0009", summary.failures[0][1])
            self.assertIn("R0001", summary.failures[0][1])

    def test_a_test_without_an_expectation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "bare",
                """\
                (mod bare)

                (test no-check
                  (println "hi"))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("no (expect ...) directive", summary.failures[0][1])

    def test_an_unknown_clause_fails_before_the_binary_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "odd",
                """\
                (mod odd)

                (test odd
                  (expect (out "hi\\n") (nonsense 1))
                  (println "hi"))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("unknown directive '(nonsense ...)'", summary.failures[0][1])

    def test_an_expect_with_no_clauses_checks_nothing_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "empty-expect",
                """\
                (mod empty)

                (test empty
                  (expect)
                  (println "hi"))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("expect needs at least one clause", summary.failures[0][1])

    def test_an_empty_err_contains_asserts_nothing_and_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "empty-err",
                """\
                (mod empty)

                (test empty
                  (expect (err-contains ""))
                  (println "hi"))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("needs a non-empty string", summary.failures[0][1])

    def test_a_non_utf8_fixture_is_a_case_failure_naming_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "hello", HELLO)
            bad = root / "exec" / "broken.logos"
            bad.write_bytes(b"\xff\xfe not utf-8")
            summary = run(root)
            self.assertEqual(summary.passed, 1)
            self.assertEqual(summary.failed, 1)
            case, detail = summary.failures[0]
            self.assertEqual(case, "exec/broken")
            self.assertIn(str(bad), detail)
            self.assertIn("not UTF-8", detail)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertIn(str(bad), out.getvalue())

    def test_a_file_outside_the_categories_is_reported_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "hello", HELLO)
            stray = root / "exce" / "typo.logos"
            stray.parent.mkdir()
            stray.write_text(HELLO, encoding="utf-8")
            summary = run(root)
            self.assertEqual(summary.skipped, [str(stray)])
            self.assertEqual(summary.total, 1)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue().count(f"SKIP {stray}"), 1)
            self.assertIn("outside the known categories", err.getvalue())

    def test_a_file_without_tests_declares_no_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "empty",
                """\
                (mod empty)

                (fn f :i64 ()
                  (ret 0))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.total, 0)
            self.assertEqual(summary.failed, 0)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp])
            self.assertEqual(code, 1)
            self.assertIn("no cases found", out.getvalue())


class NegativeCorpusTest(unittest.TestCase):
    def test_expect_error_is_a_real_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "type",
                "unknown-type",
                """\
                (mod m)

                (expect-error E0101)

                (fn f :i64 ((a :wat))
                  (ret 0))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)
            self.assertEqual(summary.cases[0].id, "type/unknown-type")

    def test_expect_error_that_does_not_match_names_both_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "type",
                "unknown-type",
                """\
                (mod m)

                (expect-error E0102)

                (fn f :i64 ((a :wat))
                  (ret 0))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("expected E0102, got E0101", summary.failures[0][1])

    def test_a_file_that_was_expected_to_fail_but_checks_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "type",
                "fine",
                """\
                (mod fine)

                (expect-error E0101)

                (fn f :i64 ()
                  (ret 0))
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("expected E0101, but the module checked", summary.failures[0][1])

    def test_a_sidecar_expect_file_is_honoured(self):
        """The corpus spells a negative case ``X.logos`` beside ``X.expect``."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp),
                "type",
                "unknown-type",
                """\
                (mod m)

                (fn f :i64 ((a :wat))
                  (ret 0))
                """,
            )
            path.with_suffix(".expect").write_text("E0101\n", encoding="utf-8")
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)

    def test_a_disagreeing_sidecar_is_reported_not_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(
                Path(tmp),
                "type",
                "unknown-type",
                """\
                (mod m)

                (expect-error E0101)

                (fn f :i64 ((a :wat))
                  (ret 0))
                """,
            )
            path.with_suffix(".expect").write_text("E0102\n", encoding="utf-8")
            summary = run(Path(tmp))
            self.assertEqual(summary.failed, 1)
            self.assertIn("disagrees", summary.failures[0][1])

    def test_a_parse_error_is_matched_against_the_directive_in_the_text(self):
        """The parser refused the file, so the tree cannot carry the code."""
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "parse",
                "unclosed",
                """\
                (mod broken
                (expect-error E0001)
                """,
            )
            summary = run(Path(tmp))
            self.assertEqual(summary.passed, 1)
            self.assertEqual(summary.cases[0].id, "parse/unclosed")


class ParityTest(unittest.TestCase):
    def test_every_case_runs_once_per_optimisation_level(self):
        calls: list[str] = []

        def fake(src, name, out, *, opt="-O2", debug=True):
            calls.append(opt)
            script = Path(out)
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text('#!/bin/sh\nprintf "hi\\n"\nexit 0\n', encoding="utf-8")
            script.chmod(0o755)
            return str(script)

        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            with patch("logos.tester.driver.run_test", side_effect=fake):
                summary = run(Path(tmp), opts=["-O0", "-O2"])
        self.assertEqual(calls, ["-O0", "-O2"])
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.passed, 1)

    def test_a_failure_names_the_level_that_disagreed(self):
        def fake(src, name, out, *, opt="-O2", debug=True):
            script = Path(out)
            script.parent.mkdir(parents=True, exist_ok=True)
            printed = "hi\\n" if opt == "-O0" else "other\\n"
            script.write_text(f'#!/bin/sh\nprintf "{printed}"\nexit 0\n', encoding="utf-8")
            script.chmod(0o755)
            return str(script)

        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            with patch("logos.tester.driver.run_test", side_effect=fake):
                summary = run(Path(tmp), opts=["-O0", "-O2"])
        self.assertEqual(summary.failed, 1)
        detail = summary.failures[0][1]
        self.assertIn("-O2: stdout", detail)
        self.assertNotIn("-O0: stdout", detail)


class TimeoutTest(unittest.TestCase):
    def test_a_hang_is_a_failure_not_a_stalled_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(
                Path(tmp),
                "exec",
                "spin",
                """\
                (mod spin)

                (test spins
                  (expect (code 0))
                  (letmut i :i64 0)
                  (while true (set i (+ i 1))))
                """,
            )
            summary = run(Path(tmp), timeout=0.5)
            self.assertEqual(summary.failed, 1)
            self.assertIn("timed out after 0.5s", summary.failures[0][1])


class FilterTest(unittest.TestCase):
    def test_only_runs_one_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            write(
                Path(tmp),
                "mem",
                "fine",
                """\
                (mod fine)

                (test frees
                  (expect (code 0) (leaks 0))
                  (let p (alloc :i64 4))
                  (free p))
                """,
            )
            summary = tester.run_suite(str(Path(tmp)), "mem", ["-O0"])
            self.assertEqual([case.id for case in summary.cases], ["mem/fine/frees"])

    def test_an_unknown_category_is_a_usage_error(self):
        with self.assertRaises(ValueError):
            tester.run_suite(str(ROOT / "tests"), "nope", ["-O0"])
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["test", "tests", "--only", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown test category 'nope'", err.getvalue())

    def test_an_empty_run_says_so_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp])
            self.assertEqual(code, 1)
            self.assertIn("no cases found", out.getvalue())


class BaselineTest(unittest.TestCase):
    def test_a_baseline_is_one_line_per_case_in_the_documented_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            summary = run(Path(tmp))
            path = Path(tmp, "summary.txt")
            tester.write_baseline(summary, path)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["LEAKS 0", "PASS exec/hello/prints -"])
            self.assertEqual(tester.read_baseline(path), {"exec/hello/prints": "PASS"})

    def test_a_case_that_passed_before_and_fails_now_is_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), "exec", "hello", HELLO)
            summary = run(Path(tmp))
            baseline = Path(tmp, "summary.txt")
            tester.write_baseline(summary, baseline)
            path.write_text(BROKEN_HELLO, encoding="utf-8")
            broken = run(Path(tmp))
            problems = tester.compare_baseline(broken, baseline)
            self.assertEqual([case for case, _ in problems], ["exec/hello/prints"])
            self.assertIn("regression", problems[0][1])
            self.assertIn("expected", problems[0][1])

    def test_a_new_case_without_a_result_is_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            baseline = Path(tmp, "summary.txt")
            tester.write_baseline(run(Path(tmp)), baseline)
            write(
                Path(tmp),
                "exec",
                "second",
                """\
                (mod second)

                (test prints
                  (expect (out "hi\\n") (code 0))
                  (println "hi"))
                """,
            )
            problems = tester.compare_baseline(run(Path(tmp)), baseline)
            self.assertIn(("exec/second/prints", "new case has no baseline result"), problems)

    def test_a_case_that_vanished_from_the_run_is_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = write(Path(tmp), "exec", "hello", HELLO)
            write(
                Path(tmp),
                "exec",
                "second",
                """\
                (mod second)

                (test prints
                  (expect (out "hi\\n") (code 0))
                  (println "hi"))
                """,
            )
            baseline = Path(tmp, "summary.txt")
            tester.write_baseline(run(Path(tmp)), baseline)
            first.unlink()
            problems = tester.compare_baseline(run(Path(tmp)), baseline)
            self.assertIn(
                ("exec/hello/prints", "regression: passed in baseline, missing from this run"),
                problems,
            )

    def test_a_filtered_run_does_not_call_the_other_categories_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "hello", HELLO)
            write(
                Path(tmp),
                "mem",
                "fine",
                """\
                (mod fine)

                (test frees
                  (expect (code 0) (leaks 0))
                  (let p (alloc :i64 4))
                  (free p))
                """,
            )
            baseline = Path(tmp, "summary.txt")
            tester.write_baseline(run(Path(tmp)), baseline)
            filtered = tester.run_suite(str(Path(tmp)), "exec", ["-O0"])
            self.assertEqual(filtered.categories, ("exec",))
            self.assertEqual(tester.compare_baseline(filtered, baseline), [])


    def test_a_filtered_update_keeps_the_other_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write(root, "exec", "hello", HELLO)
            write(root, "mem", "fine", FINE)
            baseline = root / "summary.txt"
            tester.write_baseline(run(root), baseline)
            listed = tester.read_baseline(baseline) or {}
            self.assertEqual(len(listed), 2)
            path.write_text(BROKEN_HELLO, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(
                    ["test", tmp, "--only", "exec", "--opt", "-O0", "--update-baseline"]
                )
            self.assertEqual(code, 1)
            entries = tester.read_baseline(baseline) or {}
            self.assertEqual(entries["exec/hello/prints"], "FAIL")
            self.assertEqual(entries["mem/fine/frees"], "PASS")
            self.assertIn("LEAKS 0", baseline.read_text(encoding="utf-8"))

    def test_a_deleted_category_is_a_regression_in_a_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "hello", HELLO)
            write(root, "mem", "fine", FINE)
            baseline = root / "summary.txt"
            tester.write_baseline(run(root), baseline)
            shutil.rmtree(root / "mem")
            summary = run(root)
            self.assertTrue(summary.full)
            self.assertEqual(summary.categories, ("exec",))
            self.assertIn(
                ("mem/fine/frees", "regression: passed in baseline, missing from this run"),
                tester.compare_baseline(summary, baseline),
            )

    def test_a_leak_total_above_the_baseline_is_a_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "leaky", LEAKY)
            baseline = root / "summary.txt"
            summary = run(root)
            self.assertEqual(summary.leaks, 1)
            tester.write_baseline(summary, baseline)
            baseline.write_text(
                baseline.read_text(encoding="utf-8").replace("LEAKS 1", "LEAKS 0"),
                encoding="utf-8",
            )
            problems = tester.compare_baseline(run(root), baseline)
            self.assertIn(
                ("suite/leaks", "regression: leak total 1 exceeds baseline 0"), problems
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertIn("BASELINE suite/leaks", out.getvalue())
            self.assertIn("baseline 0", out.getvalue())

    def test_a_case_id_with_a_space_is_percent_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "two words", HELLO)
            baseline = root / "summary.txt"
            tester.write_baseline(run(root), baseline)
            self.assertIn(
                "PASS exec/two%20words/prints -",
                baseline.read_text(encoding="utf-8").splitlines(),
            )
            self.assertEqual(
                tester.read_baseline(baseline), {"exec/two words/prints": "PASS"}
            )
            self.assertEqual(tester.compare_baseline(run(root), baseline), [])

    def test_a_corrupt_baseline_names_the_file_and_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "hello", HELLO)
            baseline = root / "summary.txt"
            baseline.write_text("PASS\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertIn(str(baseline), err.getvalue())
            self.assertIn("baseline line needs a status and a case", err.getvalue())
            self.assertIn("PASS exec/hello/prints", out.getvalue())


class CliTest(unittest.TestCase):
    def test_a_failing_case_exits_one_and_prints_its_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "exec", "wrong", BROKEN)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertIn("FAIL exec/wrong/prints", out.getvalue())
            self.assertIn("0 passed, 1 failed", out.getvalue())

    def test_update_baseline_then_a_broken_fixture_fails_with_a_baseline_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(Path(tmp), "exec", "hello", HELLO)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp, "--opt", "-O0", "--update-baseline"])
            self.assertEqual(code, 0)
            self.assertTrue(Path(tmp, "summary.txt").is_file())
            path.write_text(BROKEN_HELLO, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["test", tmp, "--opt", "-O0"])
            self.assertEqual(code, 1)
            self.assertIn("BASELINE exec/hello/prints", out.getvalue())
            self.assertIn("regression: passed in baseline, now fails", out.getvalue())

    def test_update_baseline_refuses_a_single_file(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["test", str(ROOT / "tests" / "exec" / "hello.logos"), "--update-baseline"]
            )
        self.assertEqual(code, 2)
        self.assertIn("needs a directory", err.getvalue())

    def test_update_baseline_refuses_a_category_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "exec", "hello", HELLO)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(
                    ["test", str(root / "exec"), "--opt", "-O0", "--update-baseline"]
                )
            self.assertEqual(code, 2)
            self.assertIn("category directory", err.getvalue())
            self.assertEqual(out.getvalue(), "")
            self.assertFalse((root / "exec" / "summary.txt").exists())


if __name__ == "__main__":
    unittest.main()
