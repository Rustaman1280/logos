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
        """The exact build command the plan runs, on a file that is not there.

        ``-o`` has to parse as the flag's value; the build itself then fails on
        the missing source, which is reported as one line on stderr and a
        status of 1 rather than a traceback.
        """
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["build", "x.logos", "-o", "/tmp/o"])
        self.assertEqual(code, 1)
        self.assertIn("x.logos", err.getvalue())

    def test_test_accepts_repeated_optimisation_levels(self):
        """A value starting with ``-`` must parse as the value of ``--opt``."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["test", "tests/exec/hello.logos", "--opt", "-O0"])
        self.assertEqual(code, 0)
        self.assertIn("PASS exec/hello/hello", out.getvalue())

    def test_build_defaults_to_the_drivers_optimisation_level(self):
        """The CLI default and ``driver.build``'s default are the same -O2."""
        with patch("logos.cli.driver.build") as build:
            code = main(["build", "x.logos"])
        self.assertEqual(code, 0)
        build.assert_called_once_with("x.logos", None, opt="-O2", keep_ir=True)


class CliFmtTest(unittest.TestCase):
    def test_fmt_rewrites_files_in_place_and_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp, "a.logos")
            second = pathlib.Path(tmp, "b.logos")
            first.write_text("(mod a)\n(println 1)\n", encoding="utf-8")
            second.write_text("(mod b)\n(println 2)\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["fmt", str(first), str(second)])
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(first.read_text(encoding="utf-8"), "(mod a)\n\n(println 1)\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "(mod b)\n\n(println 2)\n")

    def test_fmt_reads_stdin_for_a_dash(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(SRC)), redirect_stdout(out):
            code = main(["fmt", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), CANONICAL)

    def test_fmt_rejects_a_repeated_dash(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["fmt", "-", "-"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("'-' may be given at most once", err.getvalue())

    def test_fmt_reports_a_diagnostic_with_a_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp, "broken.logos")
            broken.write_text("(mod x))", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["fmt", str(broken)])
            self.assertEqual(broken.read_text(encoding="utf-8"), "(mod x))")
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("E0001", err.getvalue())
        self.assertIn("broken.logos:1:8", err.getvalue())

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
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(good.read_text(encoding="utf-8"), "(mod g)\n")
            self.assertEqual(broken.read_text(encoding="utf-8"), "(mod x")


class CliShowPatchTest(unittest.TestCase):
    NESTED = (
        "(mod m)\n"
        "\n"
        "(fn f :i64 ((a :i64))\n"
        "  (ret (+ a 1)))\n"
        "\n"
        "(fn g :i64 ((b :i64))\n"
        "  (ret (* b 2)))\n"
    )

    def _file(self, tmp, text=None):
        path = pathlib.Path(tmp, "m.logos")
        path.write_text(self.NESTED if text is None else text, encoding="utf-8")
        return path

    def test_show_prints_only_the_named_subtree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["show", str(path), "m/f/2/0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "(+ a 1)\n")

    def test_show_on_a_bad_path_exits_one_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["show", str(path), "m/f/9"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("m/f/9", err.getvalue())

    def test_patch_replaces_one_node_and_leaves_the_rest_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            with patch("sys.stdin", io.StringIO("(+ a 41)\n")):
                code = main(["patch", str(path), "m/f/2/0"])
            self.assertEqual(code, 0)
            expected = self.NESTED.replace("(+ a 1)", "(+ a 41)")
            self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_fmt_is_idempotent_after_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            with patch("sys.stdin", io.StringIO("(+ a 41)\n")):
                self.assertEqual(main(["patch", str(path), "m/f/2/0"]), 0)
            once = path.read_text(encoding="utf-8")
            self.assertEqual(main(["fmt", str(path)]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), once)

    def test_patch_on_a_bad_path_exits_one_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._file(tmp)
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stdin", io.StringIO("(+ a 41)\n")):
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(["patch", str(path), "m/f/9"])
            self.assertEqual(code, 1)
            self.assertIn("m/f/9", err.getvalue())
            self.assertEqual(path.read_text(encoding="utf-8"), self.NESTED)


if __name__ == "__main__":
    unittest.main()
