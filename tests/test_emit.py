"""Tests for IR lowering, the LLVM text emitter and the driver.

The first test is this task's acceptance criterion: ``examples/hello.logos``
compiles to an ELF that prints ``hello`` and exits 0. The rest pin the layers
that test stands on, one at a time — the IR the lowerer produces, the text the
emitter writes, the single-test entry point — so a failure names a layer
instead of "the build broke".

Every build in this file is real: clang runs, the binary runs, and the emitted
``.ll`` is read back from disk. Nothing here asserts on a mock.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

from logos import driver, ir, llvm, lower
from logos.check import CheckedModule, check_module
from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import resolve
from logos.types import Prim

ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "examples" / "hello.logos"
EXEC_HELLO = ROOT / "tests" / "exec" / "hello.logos"


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def ops(fn: ir.IrFn) -> list[str]:
    """The instruction opcodes of ``fn``, in block order."""
    return [instr[0] for block in fn.blocks for instr in block.instrs]


def calls(fn: ir.IrFn) -> list[tuple]:
    """Every call instruction of ``fn``, in block order."""
    return [instr for block in fn.blocks for instr in block.instrs if instr[0] == "call"]


def run(binary: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary], capture_output=True, text=True)


class HelloBuildTest(unittest.TestCase):
    """The acceptance criterion and the evidence behind it."""

    def test_hello_builds_and_runs(self):
        out = str(ROOT / "build" / "test-hello")
        driver.build(str(HELLO), out)
        r = run(out)
        self.assertEqual(r.stdout, "hello\n")
        self.assertEqual(r.returncode, 0)

    def test_the_ir_is_kept_on_disk_and_declares_the_entry_point(self):
        driver.build(str(HELLO), str(ROOT / "build" / "test-hello-ir"))
        text = Path(driver.ir_path(str(HELLO))).read_text(encoding="utf-8")
        self.assertIn('target triple = "x86_64-unknown-linux-gnu"', text)
        self.assertIn("define i32 @main()", text)
        self.assertIn("declare i64 @rt_write(i32, i8*, i64)", text)
        # The length is explicit and the bytes are exact: no NUL terminator is
        # invented for a language whose strings carry their own length.
        self.assertIn('[5 x i8] c"hello"', text)
        self.assertNotIn("\\00\\00", text)

    def test_the_cli_builds_the_example(self):
        out = str(ROOT / "build" / "test-hello-cli")
        r = subprocess.run(
            [sys.executable, "-m", "logos", "build", "examples/hello.logos", "-o", out],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("error:", r.stderr)
        self.assertEqual(run(out).stdout, "hello\n")

    def test_a_string_with_escapes_prints_byte_for_byte(self):
        """The review focus: what is written is the source's bytes, all of them.

        A newline, a tab, a quote, a backslash and two multi-byte characters go
        through one private global and one ``write`` of an explicit length, so
        nothing is truncated at a NUL that was never emitted.
        """
        source = ROOT / "build" / "bytes.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod bytes)\n"
            "(fn main :void ()\n"
            '  (print "one\\ntwo\\t\\"q\\" \\\\ backslash\\n")\n'
            '  (print "\u00e9\u00e9 \u2192 ok"))\n',
            encoding="utf-8",
        )
        out = str(ROOT / "build" / "test-bytes")
        driver.build(str(source), out)
        done = subprocess.run([out], capture_output=True)
        self.assertEqual(
            done.stdout, b'one\ntwo\t"q" \\ backslash\n\xc3\xa9\xc3\xa9 \xe2\x86\x92 ok'
        )
        self.assertEqual(done.returncode, 0)


class LoweringTest(unittest.TestCase):
    """The IR shape: named blocks, named locals, explicit lengths."""

    def test_hello_lowers_to_one_entry_block(self):
        m = lower.lower_module(checked(HELLO.read_text(encoding="utf-8")))
        self.assertEqual(m.triple, ir.TRIPLE)
        self.assertEqual([fn.name for fn in m.functions], ["main"])
        self.assertEqual(m.functions[0].params, [])
        self.assertEqual([block.label for block in m.functions[0].blocks], ["entry"])

    def test_a_string_literal_becomes_a_private_global_of_its_own_bytes(self):
        m = lower.lower_module(checked('(mod m)\n(fn main :void () (println "hello"))\n'))
        self.assertEqual(
            [(g.name, g.data) for g in m.globals],
            [(".str0", b"hello"), (ir.NEWLINE, b"\n")],
        )
        self.assertEqual(m.functions[0].blocks[0].term, ("ret", Prim("i32"), "0"))

    def test_println_writes_the_bytes_then_one_newline(self):
        m = lower.lower_module(checked('(mod m)\n(fn main :void () (println "hello"))\n'))
        written = calls(m.functions[0])
        self.assertEqual([c[2] for c in written], ["rt_write", "rt_write"])
        self.assertEqual([c[3][-1] for c in written], ["5", "1"])

    def test_print_writes_the_bytes_once(self):
        m = lower.lower_module(checked('(mod m)\n(fn main :void () (print "hi"))\n'))
        self.assertEqual([c[2] for c in calls(m.functions[0])], ["rt_write"])
        self.assertEqual([(g.name, g.data) for g in m.globals], [(".str0", b"hi")])

    def test_the_newline_global_is_shared_by_every_println(self):
        m = lower.lower_module(
            checked('(mod m)\n(fn main :void () (println "a")\n(println "b"))\n')
        )
        self.assertEqual([g.data for g in m.globals], [b"a", b"b", b"\n"])

    def test_an_empty_string_is_a_zero_length_global(self):
        m = lower.lower_module(checked('(mod m)\n(fn main :void () (print ""))\n'))
        self.assertEqual([(g.name, g.data) for g in m.globals], [(".str0", b"")])
        self.assertIn("constant [0 x i8] zeroinitializer", llvm.emit(m))

    def test_a_let_becomes_a_named_stack_slot(self):
        m = lower.lower_module(
            checked(
                "(mod m)\n"
                "(fn main :void ()\n"
                '  (let total :i64 41)\n'
                '  (println "x"))\n'
            )
        )
        text = llvm.emit(m)
        self.assertIn("%total.addr = alloca i64", text)
        self.assertIn("store i64 41, i64* %total.addr", text)

    def test_parameters_are_named_and_copied_to_slots(self):
        m = lower.lower_module(
            checked("(mod m)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n")
        )
        fn = m.functions[0]
        self.assertEqual([name for name, _ in fn.params], ["a", "b"])
        self.assertEqual(ops(fn), ["alloca", "store", "alloca", "store", "load", "load", "add"])
        self.assertEqual(fn.blocks[0].term, ("ret", Prim("i64"), "%t3"))
        text = llvm.emit(m)
        self.assertIn("define i64 @add(i64 %a, i64 %b) {", text)
        self.assertRegex(text, r"store i64 %a, i64\* %a\.addr")
        self.assertRegex(text, r"add i64 %t\d+, %t\d+")

    def test_a_call_passes_each_argument_in_order(self):
        m = lower.lower_module(
            checked(
                "(mod m)\n"
                "(fn add :i64 ((a :i64) (b :i64))\n"
                "  (ret (+ a b)))\n"
                "(fn main :void ()\n"
                "  (let x :i64 2)\n"
                "  (let y :i64 3)\n"
                "  (let z :i64 (add x y))\n"
                '  (println "z"))\n'
            )
        )
        self.assertIn("call i64 @add", llvm.emit(m))
        self.assertEqual(
            [c[2] for c in calls(m.functions[1])], ["add", "rt_write", "rt_write"]
        )
        self.assertEqual(len(calls(m.functions[1])[0][3]), 2)

    def test_every_local_and_block_is_named(self):
        m = lower.lower_module(
            checked(
                "(mod m)\n"
                "(fn add :i64 ((a :i64) (b :i64))\n"
                "  (ret (+ a b)))\n"
                "(fn main :void ()\n"
                "  (let x :i64 (add 1 2))\n"
                '  (println "hi"))\n'
            )
        )
        text = llvm.emit(m)
        self.assertIsNone(re.search(r"%\d", text), text)
        self.assertIsNone(re.search(r"^\d+:", text, re.MULTILINE), text)

    def test_a_function_without_a_value_return_ends_in_ret_void(self):
        m = lower.lower_module(checked('(mod m)\n(fn noop :void ()\n  (println "n"))\n'))
        self.assertEqual(m.functions[0].blocks[-1].term, ("ret", Prim("void"), None))
        self.assertIn("ret void", llvm.emit(m))


class EmitterTest(unittest.TestCase):
    """The text: one place decides the spelling, and it is LLVM 14's."""

    def test_two_blocks_and_a_branch(self):
        m = ir.IrModule(
            ir.TRIPLE,
            [],
            [
                ir.IrFn(
                    "sign",
                    [("x", Prim("i64"))],
                    Prim("i64"),
                    [
                        ir.IrBlock(
                            "entry",
                            [("icmp", "%t0", "sgt", Prim("i64"), "%x", "0")],
                            ("cbr", "%t0", "pos", "neg"),
                        ),
                        ir.IrBlock("pos", [], ("ret", Prim("i64"), "%x")),
                        ir.IrBlock("neg", [], ("ret", Prim("i64"), "0")),
                    ],
                )
            ],
        )
        text = llvm.emit(m)
        self.assertIn("br i1 %t0, label %pos, label %neg", text)
        self.assertIn("icmp sgt i64 %x, 0", text)
        self.assertIn("ret i64 0", text)

    def test_a_string_global_escapes_every_byte_outside_printable_ascii(self):
        m = ir.IrModule(ir.TRIPLE, [ir.IrGlobal(".str0", b'\x00"\\A')], [])
        self.assertIn('[4 x i8] c"\\00\\22\\5CA"', llvm.emit(m))

    def test_declarations_come_before_definitions(self):
        m = ir.IrModule(
            ir.TRIPLE,
            [],
            [
                ir.IrFn(
                    "main",
                    [],
                    Prim("i32"),
                    [ir.IrBlock("entry", [], ("ret", Prim("i32"), "0"))],
                )
            ],
            [ir.IrDecl("rt_write", (Prim("i32"), Prim("i64")), Prim("i64"))],
        )
        text = llvm.emit(m)
        self.assertLess(
            text.index("declare i64 @rt_write(i32, i64)"),
            text.index("define i32 @main()"),
        )

    def test_an_unknown_instruction_is_a_loud_failure(self):
        m = ir.IrModule(
            ir.TRIPLE,
            [],
            [
                ir.IrFn(
                    "f",
                    [],
                    Prim("void"),
                    [ir.IrBlock("entry", [("bogus",)], ("ret", Prim("void"), None))],
                )
            ],
        )
        with self.assertRaises(ValueError):
            llvm.emit(m)


class RunTestTest(unittest.TestCase):
    """``run_test`` turns one ``(test ...)`` body into an entry point."""

    def test_a_test_body_becomes_main(self):
        out = str(ROOT / "build" / "test-exec-hello")
        driver.run_test(str(EXEC_HELLO), "hello", out)
        r = run(out)
        self.assertEqual(r.stdout, "hello\n")
        self.assertEqual(r.returncode, 0)

    def test_an_unknown_test_name_is_rejected(self):
        with self.assertRaises(LogosError) as caught:
            driver.run_test(str(EXEC_HELLO), "nope", str(ROOT / "build" / "test-nope"))
        self.assertEqual(caught.exception.code, "E0005")


class BuiltinPrintTest(unittest.TestCase):
    """``print`` and ``println`` are checker builtins, not module functions."""

    def test_println_of_an_integer_is_a_type_error(self):
        with self.assertRaises(LogosError) as caught:
            checked('(mod m)\n(fn main :void () (println 1))\n')
        self.assertEqual(caught.exception.code, "E0100")

    def test_a_program_without_main_cannot_be_built(self):
        source = ROOT / "build" / "no-main.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('(mod n)\n(fn other :void ()\n  (println "x"))\n', encoding="utf-8")
        with self.assertRaises(driver.BuildError):
            driver.build(str(source), str(ROOT / "build" / "no-main"))

    def test_a_failed_link_raises_and_keeps_the_ir(self):
        """The text that failed is the first thing to read; a build keeps it."""
        with self.assertRaises(driver.BuildError) as caught:
            driver.build(
                str(HELLO), str(ROOT / "build" / "test-badopt"), opt="-Obanana"
            )
        self.assertIn("clang failed", str(caught.exception))
        self.assertTrue(Path(driver.ir_path(str(HELLO))).is_file())

    def test_keep_ir_false_removes_the_ir_after_a_successful_build(self):
        source = ROOT / "build" / "keep.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('(mod keep)\n(fn main :void ()\n  (println "k"))\n', encoding="utf-8")
        driver.build(str(source), str(ROOT / "build" / "keep"), keep_ir=False)
        self.assertFalse(Path(driver.ir_path(str(source))).exists())
        self.assertEqual(run(str(ROOT / "build" / "keep")).stdout, "k\n")


if __name__ == "__main__":
    unittest.main()
