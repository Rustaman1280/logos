"""The packaged install is a gate, not a hope.

``tools/verify_install.sh`` installs the project into a throwaway prefix,
checks that ``runtime/rt.c`` shipped as package data, and then builds and runs
``examples/hello.logos`` from outside the repository using only that install.
This test runs the script so a change that breaks packaging fails the suite.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "verify_install.sh"


class PackagingTest(unittest.TestCase):
    def test_pip_install_produces_a_working_logosc(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp, "install")
            done = subprocess.run(
                ["bash", str(SCRIPT), str(prefix)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("verify_install: PASS", done.stdout)


if __name__ == "__main__":
    unittest.main()
