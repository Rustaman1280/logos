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


if __name__ == "__main__":
    unittest.main()
