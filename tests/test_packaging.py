"""The packaged install is a gate, not a hope.

``tools/verify_install.sh`` installs the project into a throwaway prefix,
checks that ``logos/runtime/rt.c`` and ``rt.h`` shipped inside the compiler
package, and then builds and runs ``examples/hello.logos`` from outside the
repository using only that install. This test runs the script so a change that
breaks packaging fails the suite.

``WheelContentsTest`` builds a wheel and inspects the archive directly. The
install script can only catch a missing runtime after pip has decided what to
put on disk; the wheel is what pip installs, so asserting the two files are in
it catches a packaging regression on the first run instead of after an
unrelated install step hides it. The metadata name is asserted first: a
setuptools older than PEP 621 builds ``UNKNOWN-0.0.0`` and no runtime files,
and naming that cause is more useful than two missing-file failures.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "verify_install.sh"


def _module_available(name: str) -> bool:
    """Whether ``sys.executable`` can import ``name`` from outside the repo.

    The repository has a ``build/`` directory, so importing from the working
    directory would make ``import build`` succeed as a namespace package even
    when the build front end is not installed. Check from ``/`` instead.
    """
    done = subprocess.run(
        [sys.executable, "-c", f"import {name}"],
        cwd="/",
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


def _wheel_command(dest: Path) -> list[str] | None:
    """A command that builds this project's wheel into ``dest``, if one exists."""
    if _module_available("pip"):
        return [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "-w",
            str(dest),
            str(ROOT),
        ]
    pip_bin = shutil.which("pip3") or shutil.which("pip")
    if pip_bin is not None:
        return [pip_bin, "wheel", "--no-deps", "-w", str(dest), str(ROOT)]
    if _module_available("build"):
        return [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(dest),
            str(ROOT),
        ]
    return None


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


class WheelContentsTest(unittest.TestCase):
    """A wheel is named ``logos`` and carries the C runtime as package data."""

    def test_the_wheel_is_named_logos_and_contains_the_runtime_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            command = _wheel_command(dest)
            if command is None:
                self.skipTest(
                    "neither pip nor build is available to build a wheel; "
                    "install one of them to run this packaging check"
                )
            done = subprocess.run(
                command,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=600,
            )
            self.assertEqual(
                done.returncode,
                0,
                "wheel build failed:\n" + done.stdout + done.stderr,
            )
            wheels = sorted(dest.glob("*.whl"))
            self.assertEqual(len(wheels), 1, f"expected one wheel, found {wheels}")
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
                metadata_files = [
                    name for name in names if name.endswith(".dist-info/METADATA")
                ]
                self.assertEqual(
                    len(metadata_files),
                    1,
                    f"expected one dist-info/METADATA in {wheels[0].name}, "
                    f"found {metadata_files}",
                )
                metadata = archive.read(metadata_files[0]).decode("utf-8")

        # A setuptools that cannot read [project] names the wheel UNKNOWN-0.0.0
        # and ships no package files at all. Name that here so the failure is
        # one clear assertion, not two missing-file ones below.
        metadata_name = ""
        for line in metadata.splitlines():
            if line.startswith("Name:"):
                metadata_name = line.split(":", 1)[1].strip()
                break
        self.assertTrue(
            wheels[0].name.startswith("logos-"),
            f"wheel is named {wheels[0].name}; setuptools built UNKNOWN, "
            "so it did not read the [project] table",
        )
        self.assertNotEqual(
            metadata_name,
            "UNKNOWN",
            f"{wheels[0].name} has UNKNOWN metadata; setuptools did not read "
            "the [project] table",
        )
        self.assertEqual(
            metadata_name,
            "logos",
            f"{wheels[0].name} has metadata Name {metadata_name!r}, not 'logos'",
        )
        for asset in ("logos/runtime/rt.c", "logos/runtime/rt.h"):
            with self.subTest(asset=asset):
                self.assertIn(asset, names, f"{asset} is missing from {wheels[0].name}")


if __name__ == "__main__":
    unittest.main()
