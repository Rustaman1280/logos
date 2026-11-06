"""Tests for the logos command line entry point.

The plan brief specified a ``pytest``-style ``test_version(capsys)`` function.
The project is standard library only and runs its tests with
``python3 -m unittest``, which does not discover bare functions or provide the
``capsys`` fixture, so the same assertions are expressed here as a
``unittest.TestCase`` using ``contextlib.redirect_stdout``.
"""

import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from logos.cli import main

SRC = '(mod math)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n'
CANONICAL = "(mod math)\n\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n"


class CliVersionTest(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "logos 0.1.0")


class CliFlagParsingTest(unittest.TestCase):
    def test_build_accepts_output_flag(self):
        """The exact build command the plan runs in a later task."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["build", "x.logos", "-o", "/tmp/o"])
        self.assertEqual(code, 0)
        self.assertIn("build: not implemented", out.getvalue())

    def test_test_accepts_repeated_optimisation_levels(self):
        """A value starting with ``-`` must parse as the value of ``--opt``."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["test", "tests/exec/strings.logos", "--opt", "-O0", "-O2"])
        self.assertEqual(code, 0)
        self.assertIn("test: not implemented", out.getvalue())


class CliFmtTest(unittest.TestCase):
    def test_fmt_prints_the_canonical_form_of_several_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp, "a.logos")
            second = pathlib.Path(tmp, "b.logos")
            first.write_text("(mod a)\n(println 1)\n", encoding="utf-8")
            second.write_text("(mod b)\n(println 2)\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["fmt", str(first), str(second)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "(mod a)\n\n(println 1)\n(mod b)\n\n(println 2)\n")

    def test_fmt_reads_stdin_for_a_dash(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(SRC)), redirect_stdout(out):
            code = main(["fmt", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), CANONICAL)

    def test_fmt_reports_a_diagnostic_with_the_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp, "broken.logos")
            broken.write_text("(mod x", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["fmt", str(broken)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("broken.logos", err.getvalue())
        self.assertIn("E0001", err.getvalue())

    def test_fmt_formats_the_good_files_after_a_bad_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp, "broken.logos")
            good = pathlib.Path(tmp, "good.logos")
            broken.write_text("(mod x", encoding="utf-8")
            good.write_text("(mod g)\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["fmt", str(broken), str(good)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "(mod g)\n")


if __name__ == "__main__":
    unittest.main()
