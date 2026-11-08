"""The C runtime's strings and formatting, exercised directly.

A Logos program reaches these functions through the lowerer, and every value it
can build is covered by ``test_text``. A few are not reachable from the
language at all — ``INT64_MIN``, ``inf``, ``nan``, the largest and smallest
doubles — and those are the ones where a formatter is most likely to be wrong,
so they are checked here by compiling a small C program against ``rt.c``.

Nothing here tests the compiler: it is the same runtime the binaries link, on
its own.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "logos" / "runtime"
BUILD = ROOT / "build"

PROBE = r"""
#include <math.h>
#include "rt.h"

int main(void) {
    size_t n = 0;
    unsigned char *s;

    s = rt_str_concat((const unsigned char *)"ab", 2,
                      (const unsigned char *)"cde", 3, &n);
    rt_print_str(s, n);
    rt_println();

    s = rt_str_from_i64(-9223372036854775807LL - 1, &n);
    rt_print_str(s, n);
    rt_println();

    rt_print_u64(18446744073709551615ULL); rt_println();
    rt_print_i64(-1); rt_println();

    rt_print_f64(0.1); rt_println();
    rt_print_f64(2.0); rt_println();
    rt_print_f64(1e300); rt_println();
    rt_print_f64(1.0 / 3.0); rt_println();
    rt_print_f64(0x1.fffffffffffffp+1023); rt_println();
    rt_print_f64(5e-324); rt_println();
    rt_print_f64(INFINITY); rt_println();
    rt_print_f64(NAN); rt_println();

    rt_print_i64(rt_str_eq((const unsigned char *)"a\0b", 3,
                           (const unsigned char *)"a\0b", 3)); rt_println();
    rt_print_i64(rt_str_eq((const unsigned char *)"a\0b", 3,
                           (const unsigned char *)"a\0c", 3)); rt_println();
    rt_print_i64(rt_str_eq(NULL, 0, NULL, 0)); rt_println();
    rt_print_i64(rt_str_eq((const unsigned char *)"ab", 2,
                           (const unsigned char *)"abc", 3)); rt_println();
    rt_print_str(NULL, 0);
    return 0;
}
"""

EXPECTED = """abcde
-9223372036854775808
18446744073709551615
-1
0.1
2
1e+300
0.3333333333333333
1.7976931348623157e+308
4.94065645841247e-324
inf
nan
1
0
1
0
"""


class RuntimeProbeTest(unittest.TestCase):
    """One binary, one expected transcript."""

    @classmethod
    def setUpClass(cls):
        source = BUILD / "rt-probe.c"
        binary = BUILD / "rt-probe"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(PROBE, encoding="utf-8")
        command = [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-I",
            str(RUNTIME),
            str(source),
            str(RUNTIME / "rt.c"),
            "-o",
            str(binary),
            "-lm",
        ]
        done = subprocess.run(command, capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError(f"rt-probe did not build:\n{done.stderr}")
        cls.binary = str(binary)

    def test_the_formatters_and_the_string_helpers(self):
        done = subprocess.run([self.binary], capture_output=True, text=True)
        self.assertEqual(done.stdout, EXPECTED)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_probe_built_without_a_warning(self):
        # ``setUpClass`` builds with -Wall -Wextra; a warning there is a
        # defect in the runtime that a user's build would print.
        done = subprocess.run(
            ["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I",
             str(RUNTIME), "-fsyntax-only", str(RUNTIME / "rt.c")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
