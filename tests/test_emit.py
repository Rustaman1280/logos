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


def write_source(name: str, source: str) -> Path:
    """Write ``source`` to ``build/<name>.logos`` and return the path."""
    path = ROOT / "build" / f"{name}.logos"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def path_of(cm: CheckedModule, kind: str) -> str:
    """The node path of the first node of ``kind`` in ``cm``."""
    for node in cm.module.root.walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in the module")


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
        # The binding is numbered, not named: a source atom is not necessarily
        # an LLVM identifier, so only the checker and the diagnostics see it.
        self.assertIn("%v.0.addr = alloca i64", text)
        self.assertIn("store i64 41, i64* %v.0.addr", text)

    def test_parameters_are_named_and_copied_to_slots(self):
        m = lower.lower_module(
            checked("(mod m)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n")
        )
        fn = m.functions[0]
        self.assertEqual([name for name, _ in fn.params], ["v.0", "v.1"])
        # Both slots are allocated first, then filled: an alloca belongs to the
        # entry block, so a slot inside a loop is allocated once.
        self.assertEqual(
            ops(fn), ["alloca", "alloca", "store", "store", "load", "load", "add"]
        )
        self.assertEqual(fn.blocks[0].term, ("ret", Prim("i64"), "%t3"))
        text = llvm.emit(m)
        self.assertIn(
            "define internal i64 @lg.m.add(i64 %v.0, i64 %v.1) {", text
        )
        self.assertRegex(text, r"store i64 %v\.0, i64\* %v\.0\.addr")
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
        self.assertIn("call i64 @lg.m.add", llvm.emit(m))
        self.assertEqual(
            [c[2] for c in calls(m.functions[1])],
            ["lg.m.add", "rt_write", "rt_write"],
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

    def test_a_failed_link_raises_and_leaves_no_artefacts(self):
        """A build that failed must not leave a binary or IR to be trusted."""
        out = str(ROOT / "build" / "test-badopt")
        with self.assertRaises(driver.BuildError) as caught:
            driver.build(str(HELLO), out, opt="-Obanana")
        self.assertIn("clang failed", str(caught.exception))
        self.assertFalse(Path(driver.ir_path(str(HELLO))).exists())
        self.assertFalse(Path(out).exists())

    def test_a_failed_rebuild_removes_the_previous_binary(self):
        """The stale-program bug: a bad source must not leave the old runnable."""
        source = ROOT / "build" / "stale.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        out = str(ROOT / "build" / "test-stale")
        source.write_text(
            '(mod stale)\n\n(fn main :void ()\n  (println "one"))\n', encoding="utf-8"
        )
        driver.build(str(source), out)
        self.assertTrue(Path(out).is_file())
        self.assertTrue(Path(driver.ir_path(str(source))).is_file())
        source.write_text(
            "(mod stale)\n\n(fn main :void ()\n  (println 1))\n", encoding="utf-8"
        )
        with self.assertRaises(LogosError):
            driver.build(str(source), out)
        self.assertFalse(Path(out).exists())
        self.assertFalse(Path(driver.ir_path(str(source))).exists())

    def test_keep_ir_false_removes_the_ir_after_a_successful_build(self):
        source = ROOT / "build" / "keep.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('(mod keep)\n(fn main :void ()\n  (println "k"))\n', encoding="utf-8")
        driver.build(str(source), str(ROOT / "build" / "keep"), keep_ir=False)
        self.assertFalse(Path(driver.ir_path(str(source))).exists())
        self.assertEqual(run(str(ROOT / "build" / "keep")).stdout, "k\n")


class CheckedDivisionTest(unittest.TestCase):
    """``/`` and ``%`` never reach a bare ``sdiv``/``srem``.

    LLVM makes a division by zero undefined behaviour, so an unchecked one
    traps at ``-O0`` and vanishes into poison at ``-O2``. These tests run a real
    binary at both levels, because the two levels failed differently.
    """

    def test_division_by_zero_panics_at_both_optimisation_levels(self):
        source = (
            "(mod dz)\n\n"
            "(fn main :void ()\n"
            "  (let x :i64 (/ 1 0))\n"
            '  (println "after"))\n'
        )
        path = write_source("dz", source)
        where = path_of(checked(source), "/")
        for opt in ("-O0", "-O2"):
            out = str(ROOT / "build" / f"test-dz{opt[1:]}")
            driver.build(str(path), out, opt=opt)
            r = run(out)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(r.stderr, f"{where}: R0001: division by zero\n")
            # The statement after the division must not run.
            self.assertNotIn("after", r.stdout)

    def test_modulo_by_zero_panics_with_the_operation_path(self):
        source = (
            "(mod dm)\n\n"
            "(fn main :void ()\n"
            "  (let x :i64 (% 1 0))\n"
            '  (println "after"))\n'
        )
        path = write_source("dm", source)
        where = path_of(checked(source), "%")
        out = str(ROOT / "build" / "test-dm")
        driver.build(str(path), out, opt="-O0")
        r = run(out)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(
            r.stderr, f"{where}: R0001: division by zero\n"
        )

    def test_int_min_divided_by_minus_one_wraps(self):
        """``INT_MIN / -1`` is INT_MIN, not a hardware trap.

        The result is observed through ``+ 1`` and the exit status: the wrapped
        quotient's low 32 bits are ``0x80000001``, so the status is 1.
        """
        source = (
            "(mod ovf)\n\n"
            "(fn main :i64 ()\n"
            "  (ret (+ (/ -9223372036854775808 -1) 1)))\n"
        )
        path = write_source("ovf", source)
        for opt in ("-O0", "-O2"):
            out = str(ROOT / "build" / f"test-ovf{opt[1:]}")
            driver.build(str(path), out, opt=opt)
            r = run(out)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertNotIn("panic", r.stderr)

    def test_a_signed_division_is_guarded_and_a_phi_carries_the_result(self):
        m = lower.lower_module(
            checked("(mod m)\n(fn f :i64 ((a :i64) (b :i64)) (ret (/ a b)))\n")
        )
        fn = m.functions[0]
        self.assertIn("sdiv", ops(fn))
        self.assertIn("phi", ops(fn))
        self.assertIn("and", ops(fn))
        self.assertIn("rt_panic", [c[2] for c in calls(fn)])
        text = llvm.emit(m)
        self.assertIn("unreachable", text)
        self.assertIn("declare void @rt_panic(i8*, i8*, i8*)", text)

    def test_unsigned_division_and_remainder_use_the_unsigned_opcodes(self):
        m = lower.lower_module(
            checked(
                "(mod m)\n"
                "(fn f :u64 ((a :u64) (b :u64))\n"
                "  (let q :u64 (/ a b))\n"
                "  (ret (% q b)))\n"
            )
        )
        text = llvm.emit(m)
        self.assertIn("= udiv i64", text)
        self.assertIn("= urem i64", text)
        self.assertNotIn("sdiv", text)
        self.assertNotIn("srem", text)

    def test_an_unsigned_remainder_of_a_value_above_two_to_the_63_is_correct(self):
        """2**64-1 is above 2**63, so the two readings of it differ.

        A ``:u64`` value cannot cross into the ``:i64`` the entry point returns,
        so the remainder is observed through a checked division: the unsigned
        remainder of 2**64-1 by 10 is 5, which makes the divisor zero and
        panics; a signed ``srem`` would give -1, a divisor of 2**64-6, and no
        panic at all.
        """
        source = (
            "(mod urem)\n\n"
            "(fn main :void ()\n"
            "  (let x :u64 18446744073709551615)\n"
            "  (let d :u64 (- (% x 10) 5))\n"
            "  (/ 100 d))\n"
        )
        path = write_source("urem", source)
        where = path_of(checked(source), "/")
        out = str(ROOT / "build" / "test-urem")
        driver.build(str(path), out, opt="-O0")
        r = run(out)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr, f"{where}: R0001: division by zero\n")


class SymbolTest(unittest.TestCase):
    """A source name can never become a libc or runtime symbol."""

    def test_a_user_write_cannot_interpose_on_rt_write(self):
        source = (
            "(mod inter)\n\n"
            "(fn write :i64 ((fd :i64) (buf :i64) (len :i64))\n"
            "  (ret len))\n\n"
            "(fn main :void ()\n"
            "  (let n :i64 (write 1 2 3))\n"
            '  (println "hi"))\n'
        )
        path = write_source("inter", source)
        out = str(ROOT / "build" / "test-inter")
        driver.build(str(path), out)
        r = run(out)
        self.assertEqual(r.stdout, "hi\n", r.stderr)
        self.assertEqual(r.returncode, 0)
        text = Path(driver.ir_path(str(path))).read_text(encoding="utf-8")
        self.assertIn("define internal i64 @lg.inter.write(", text)
        self.assertIn("call i64 @lg.inter.write", text)
        self.assertIn("declare i64 @rt_write(i32, i8*, i64)", text)
        self.assertNotIn("define i64 @write", text)

    def test_main_stays_external_and_keeps_its_name(self):
        m = lower.lower_module(checked('(mod m)\n(fn main :void () (println "x"))\n'))
        fn = m.functions[0]
        self.assertEqual(fn.symbol, "main")
        self.assertFalse(fn.internal)
        self.assertIn("define i32 @main()", llvm.emit(m))

    def test_the_rt_prefix_is_reserved(self):
        with self.assertRaises(LogosError) as caught:
            checked("(mod m)\n(fn rt_write :i64 ((fd :i64)) (ret fd))\n")
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.message, "rt_ names are reserved")


class BuiltinShadowTest(unittest.TestCase):
    """A module-level ``fn`` named ``print``/``println`` wins over the builtin.

    The checker has always resolved ``env.fns`` before ``BUILTINS``. The
    lowerer used to do the reverse, so a program that defined its own ``print``
    checked against its own signature and then silently emitted the builtin
    instead: the user function was never called and no diagnostic appeared.
    """

    def test_a_user_print_is_called_instead_of_the_builtin(self):
        source = (
            "(mod shadow)\n\n"
            "(fn print :i64 ((n :i64))\n"
            "  (ret (+ n 1)))\n\n"
            "(fn main :i64 ()\n"
            "  (ret (print 41)))\n"
        )
        path = write_source("shadow", source)
        out = str(ROOT / "build" / "test-shadow")
        driver.build(str(path), out)
        r = run(out)
        # 42 only happens if @lg.shadow.print ran and returned its sum.
        self.assertEqual(r.returncode, 42, r.stderr)
        self.assertEqual(r.stdout, "")
        text = Path(driver.ir_path(str(path))).read_text(encoding="utf-8")
        self.assertIn("call i64 @lg.shadow.print(i64 41)", text)
        self.assertNotIn("call i64 @rt_write", text)

    def test_a_shadow_taking_str_calls_the_user_function(self):
        """The review's probe, now that ``:str`` values are lowered.

        Falling back to the builtin made the bug silent: the call would print
        ``x`` and never enter the user's function. Now the parameter carries
        the aggregate, and only ``USER-FN`` may reach stdout.
        """
        source = (
            "(mod shadow)\n\n"
            "(fn print :void ((s :str))\n"
            '  (println "USER-FN"))\n\n'
            "(fn main :void ()\n"
            '  (print "x"))\n'
        )
        path = write_source("shadow-str", source)
        out = str(ROOT / "build" / "test-shadow-str")
        driver.build(str(path), out)
        r = run(out)
        self.assertEqual(r.stdout, "USER-FN\n")
        self.assertEqual(r.returncode, 0)
        text = Path(driver.ir_path(str(path))).read_text(encoding="utf-8")
        self.assertIn("define internal void @lg.shadow.print({ i8*, i64 } %v.0)", text)
        self.assertIn("call void @lg.shadow.print({ i8*, i64 } %t", text)


class IntegerWidthTest(unittest.TestCase):
    """An integer literal outside its resolved width is E0106, never truncated."""

    def test_an_out_of_range_i8_literal_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            checked("(mod m)\n(fn main :void () (let x :i8 300))\n")
        self.assertEqual(caught.exception.code, "E0106")
        self.assertEqual(caught.exception.message, "literal 300 does not fit :i8")
        self.assertEqual(caught.exception.path, "m/main/2")

    def test_every_width_checks_both_ends(self):
        failing = (
            ("i8", "128"),
            ("i8", "-129"),
            ("u8", "-1"),
            ("u8", "256"),
            ("u16", "65536"),
            ("u32", "-1"),
            ("u64", "18446744073709551616"),
        )
        for width, text in failing:
            with self.subTest(width=width, literal=text):
                with self.assertRaises(LogosError) as caught:
                    checked(f"(mod m)\n(fn f :void () (let x :{width} {text}))\n")
                self.assertEqual(caught.exception.code, "E0106")
                self.assertEqual(
                    caught.exception.message, f"literal {text} does not fit :{width}"
                )

    def test_the_boundaries_still_pass(self):
        passing = (
            ("i8", "127"),
            ("i8", "-128"),
            ("u8", "255"),
            ("i32", "-2147483648"),
            ("u32", "4294967295"),
            ("i64", "9223372036854775807"),
            ("i64", "-9223372036854775808"),
            ("u64", "18446744073709551615"),
        )
        for width, text in passing:
            with self.subTest(width=width, literal=text):
                checked(f"(mod m)\n(fn f :void () (let x :{width} {text}))\n")

    def test_an_unannotated_literal_must_fit_i64(self):
        with self.assertRaises(LogosError) as caught:
            checked("(mod m)\n(fn f :void () (let x 99999999999999999999))\n")
        self.assertEqual(caught.exception.code, "E0106")
        self.assertEqual(
            caught.exception.message, "literal 99999999999999999999 does not fit :i64"
        )


class CallResolutionTest(unittest.TestCase):
    """The lowerer, not the printer, owns "no such callee"."""

    def test_a_call_with_no_lowered_definition_is_a_diagnostic(self):
        other = parse("(mod other)\n\n(fn helper :i64 ()\n  (ret 1))\n", "other.logos")
        root = parse(
            "(mod main)\n\n(use other)\n\n(fn main :i64 ()\n  (ret (helper)))\n",
            "main.logos",
        )
        module = resolve(root, lambda name: other, "main.logos")
        cm = check_module(module)
        with self.assertRaises(LogosError) as caught:
            lower.lower_module(cm)
        self.assertEqual(caught.exception.code, "E0103")
        self.assertEqual(caught.exception.path, path_of(cm, "helper"))

    def test_a_test_body_can_call_a_module_function(self):
        source = (
            "(mod t)\n\n"
            "(fn triple :i64 ((n :i64))\n"
            "  (ret (* n 3)))\n\n"
            "(test calls-helper\n"
            "  (expect (code 0))\n"
            "  (triple 2)\n"
            '  (println "ok"))\n'
        )
        path = write_source("calltest", source)
        out = str(ROOT / "build" / "test-calltest")
        driver.run_test(str(path), "calls-helper", out)
        r = run(out)
        self.assertEqual(r.stdout, "ok\n", r.stderr)
        self.assertEqual(r.returncode, 0)
        text = Path(driver.test_ir_path(str(path), "calls-helper")).read_text(
            encoding="utf-8"
        )
        self.assertIn("define internal i64 @lg.t.triple(", text)
        self.assertIn("call i64 @lg.t.triple(i64 2)", text)


class IRNameTest(unittest.TestCase):
    """A Logos atom is not an LLVM identifier; the IR must not show it raw."""

    def test_a_keyword_shaped_name_still_lowers(self):
        source = (
            "(mod kw)\n\n"
            "(fn main :void ()\n"
            "  (let len :i64 2)\n"
            "  (let x? :i64 (+ len 1))\n"
            '  (println "kw"))\n'
        )
        path = write_source("kw", source)
        out = str(ROOT / "build" / "test-kw")
        driver.build(str(path), out)
        self.assertEqual(run(out).stdout, "kw\n")
        text = Path(driver.ir_path(str(path))).read_text(encoding="utf-8")
        self.assertNotIn("?", text)
        self.assertIn("%v.0.addr", text)
        self.assertIn("%v.1.addr", text)

    def test_an_unsafe_symbol_is_quoted(self):
        m = ir.IrModule(
            ir.TRIPLE,
            [],
            [
                ir.IrFn(
                    "f",
                    [],
                    Prim("i32"),
                    [ir.IrBlock("entry", [], ("ret", Prim("i32"), "0"))],
                    symbol="lg.a?b.f",
                )
            ],
        )
        self.assertIn('define i32 @"lg.a?b.f"()', llvm.emit(m))


class I64MainTest(unittest.TestCase):
    """``main`` is the C entry point: ``:void`` exits 0, ``:i64`` exits its value."""

    def test_an_i64_main_returns_its_value_as_the_process_status(self):
        for value, status in (("7", 7), ("-1", 255)):
            with self.subTest(value=value):
                source = f"(mod ex)\n\n(fn main :i64 ()\n  (ret {value}))\n"
                path = write_source("ex", source)
                out = str(ROOT / "build" / "test-ex")
                driver.build(str(path), out)
                self.assertEqual(run(out).returncode, status)
        text = Path(driver.ir_path(str(path))).read_text(encoding="utf-8")
        self.assertIn("define i32 @main()", text)
        self.assertIn("trunc i64", text)

    def test_a_void_main_exits_zero(self):
        out = str(ROOT / "build" / "test-hello-status")
        driver.build(str(HELLO), out)
        self.assertEqual(run(out).returncode, 0)

    def test_an_unsupported_main_return_type_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            lower.lower_module(checked("(mod m)\n(fn main :i32 () (ret 0))\n"))
        self.assertEqual(caught.exception.code, "E0100")
        self.assertIn(":void or :i64", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
