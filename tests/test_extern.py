"""Tests for ``(extern fn ...)``: the C boundary and its ABI.

An extern declaration is the one way a Logos program reaches code this
compiler does not build. It is a declaration, not a body: the checker gives it
a type and the lowerer emits a ``declare`` under the bare name, so the linker
resolves it in libc.

What may cross is a closed list — the primitives, ``:bool``, ``:str`` (two
arguments, pointer then length), a pointer to one of those, and a struct of
those. Everything else is E0002 ``type does not cross the C boundary``.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from logos import driver, llvm, lower
from logos.check import CheckedModule, check_module
from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import resolve
from logos.types import FnTy, Prim, Ptr, StructRef

ROOT = Path(__file__).resolve().parents[1]


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def declares(module) -> dict[str, tuple[tuple, object]]:
    return {decl.name: (decl.params, decl.ret) for decl in module.declares}


def calls(fn) -> list[tuple]:
    return [instr for block in fn.blocks for instr in block.instrs if instr[0] == "call"]


class ExternCase(unittest.TestCase):
    def fail_on(self, source: str) -> LogosError:
        with self.assertRaises(LogosError) as caught:
            checked(source)
        return caught.exception


class ExternSignatureTest(ExternCase):
    def test_a_declaration_is_visible_under_its_own_name(self):
        cm = checked("(mod m)(extern fn getpid :i64 ())")
        self.assertEqual(cm.fn_sigs["getpid"], FnTy((), Prim("i64")))
        self.assertIn("getpid", cm.externs)

    def test_a_string_parameter_has_the_language_type(self):
        cm = checked("(mod m)(extern fn write :i64 ((n :str)))")
        self.assertEqual(cm.fn_sigs["write"], FnTy((Prim("str"),), Prim("i64")))

    def test_a_struct_of_scalars_crosses(self):
        cm = checked(
            "(mod m)(type pair (struct (a :i64) (b :i32)))"
            "(extern fn take :i64 ((p pair)))"
        )
        self.assertEqual(cm.fn_sigs["take"].params, (StructRef("pair"),))

    def test_a_pointer_crosses(self):
        cm = checked("(mod m)(extern fn strlen :i64 ((s :(ptr :u8))))")
        self.assertEqual(cm.fn_sigs["strlen"].params, (Ptr(Prim("u8")),))

    def test_a_vector_is_refused(self):
        error = self.fail_on("(mod m)(extern fn f :i64 ((v :(vec :i64))))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "type does not cross the C boundary")
        self.assertEqual(error.path, "m/f/1/0")

    def test_an_enum_is_refused_in_a_return_type(self):
        error = self.fail_on("(mod m)(type e (enum (a)))(extern fn f e ())")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "type does not cross the C boundary")

    def test_an_owning_pointer_is_refused(self):
        # C has no release this compiler can honour, so the owner is handed
        # over as (ptr p) or not at all.
        error = self.fail_on("(mod m)(extern fn f :i64 ((p :(own :u8))))")
        self.assertEqual(error.message, "type does not cross the C boundary")

    def test_a_type_variable_is_refused(self):
        error = self.fail_on("(mod m)(extern fn f :T ((x :T)))")
        self.assertEqual(error.code, "E0101")
        self.assertIn("cannot have type variables", error.message)

    def test_a_reserved_form_name_is_refused(self):
        error = self.fail_on("(mod m)(extern fn get :i64 ())")
        self.assertEqual(error.code, "E0003")
        self.assertEqual(error.message, "reserved form name")

    def test_an_rt_name_is_refused(self):
        error = self.fail_on("(mod m)(extern fn rt_alloc :i64 ())")
        self.assertEqual(error.code, "E0003")
        self.assertEqual(error.message, "rt_ names are reserved")

    def test_a_duplicate_name_is_refused(self):
        error = self.fail_on(
            "(mod m)(fn getpid :i64 () (ret 1))(extern fn getpid :i64 ())"
        )
        self.assertEqual(error.code, "E0003")

    def test_a_malformed_declaration_is_refused(self):
        error = self.fail_on("(mod m)(extern getpid :i64 ())")
        self.assertEqual(error.code, "E0002")


class ExternLoweringTest(ExternCase):
    def test_the_bare_name_is_declared_not_defined(self):
        m = lower.lower_module(
            checked(
                "(mod m)(extern fn getpid :i64 ())(fn main :i64 () (ret (getpid)))"
            )
        )
        self.assertEqual(declares(m)["getpid"], ((), Prim("i64")))
        self.assertEqual([fn.symbol for fn in m.functions], ["main"])
        self.assertIn("declare i64 @getpid()", llvm.emit(m))
        self.assertNotIn("define i64 @getpid", llvm.emit(m))

    def test_a_string_argument_is_a_pointer_and_a_length(self):
        m = lower.lower_module(
            checked(
                "(mod m)(extern fn write :i64 ((n :str)))"
                '(fn main :i64 () (ret (write "hi")))'
            )
        )
        self.assertEqual(
            declares(m)["write"], ((Ptr(Prim("u8")), Prim("i64")), Prim("i64"))
        )
        call = calls(m.functions[0])[0]
        self.assertEqual(call[2], "write")
        self.assertEqual(len(call[3]), 2)

    def test_a_struct_parameter_is_one_aggregate(self):
        m = lower.lower_module(
            checked(
                "(mod m)(type pair (struct (a :i64) (b :i64)))"
                "(extern fn take :i64 ((p pair)))"
                "(fn main :i64 () (ret (take (make pair 1 2))))"
            )
        )
        self.assertEqual(declares(m)["take"][0], (StructRef("pair"),))
        self.assertIn("%struct.pair = type { i64, i64 }", llvm.emit(m))


class ExternLinkTest(unittest.TestCase):
    """The three calls the ruling names, built and run for real."""

    SOURCE = (
        "(mod linkex)\n"
        "(extern fn getpid :i64 ())\n"
        "(extern fn strlen :i64 ((s : (ptr :u8))))\n"
        "(extern fn write :i64 ((fd :i64) (text :str)))\n"
        "(fn main :i64 ()\n"
        "  (let b (alloc :u8 4))\n"
        "  (set-deref b 111)\n"
        "  (set-deref (ptr-add b 1) 107)\n"
        "  (set-deref (ptr-add b 2) 0)\n"
        "  (set-deref (ptr-add b 3) 0)\n"
        "  (println (strlen (ptr b)))\n"
        '  (let _ (write 1 "libc\\n"))\n'
        "  (free b)\n"
        "  (ret (if (> (getpid) 0) 0 1)))\n"
    )

    def test_the_symbols_resolve_against_libc(self):
        source = ROOT / "build" / "linkex.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(self.SOURCE, encoding="utf-8")
        binary = driver.build(str(source), str(ROOT / "build" / "linkex"))
        run = subprocess.run([binary], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.stdout, "2\nlibc\n")
        self.assertEqual(run.returncode, 0)


if __name__ == "__main__":
    unittest.main()
