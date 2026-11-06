"""Tests for the logos command line entry point.

The plan brief specified a ``pytest``-style ``test_version(capsys)`` function.
The project is standard library only and runs its tests with
``python3 -m unittest``, which does not discover bare functions or provide the
``capsys`` fixture, so the same assertions are expressed here as a
``unittest.TestCase`` using ``contextlib.redirect_stdout``.
"""

import io
import unittest
from contextlib import redirect_stdout

from logos.cli import main


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


if __name__ == "__main__":
    unittest.main()
