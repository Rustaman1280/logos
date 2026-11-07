"""Manual memory: ``alloc``, ``free``, raw pointers, moves and the leak tracker.

Three layers, one class each. The checker's share is ownership: an owning
pointer moves, a moved binding cannot be read, a raw pointer cannot be freed,
and a move out of a plain ``let`` needs ``letmut``. The lowering's share is the
runtime contract: ``rt_alloc`` gets a byte count, an alignment and the path of
the allocating node; ``deref`` is a typed load; ``free`` hands the block back.
The exec share is a real binary at ``-O0`` and ``-O2``: a released block leaves
no trace, an unreleased one is reported with its size and path, and a second
free panics ``R0002`` instead of corrupting the heap.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from logos import driver, ir, llvm, lower
from logos.check import CheckedModule, check_module
from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import resolve
from logos.types import Own, Prim, Ptr, StructRef, Ty

ROOT = Path(__file__).resolve().parents[1]
MEM = ROOT / "tests" / "mem"
LEAK = MEM / "leak.logos"
DOUBLE_FREE = MEM / "double-free.logos"


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def emitted(source: str) -> str:
    return llvm.emit(lower.lower_module(checked(source)))


def path_of(cm: CheckedModule, kind: str) -> str:
    for node in cm.module.root.walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in the module")


def path_in(cm: CheckedModule, test_name: str, kind: str) -> str:
    """The path of the first ``kind`` node inside one test."""
    for node in cm.module.tests[test_name].walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in test '{test_name}'")


def calls(fn: ir.IrFn) -> list[tuple]:
    return [ins for block in fn.blocks for ins in block.instrs if ins[0] == "call"]


def callees(fn: ir.IrFn) -> list[str]:
    return [ins[2] for ins in calls(fn)]


def instructions(fn: ir.IrFn, opcode: str) -> list[tuple]:
    return [ins for block in fn.blocks for ins in block.instrs if ins[0] == opcode]


def fail_on(source: str) -> LogosError:
    try:
        checked(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this checked: {source}")


def fail_on_lower(source: str) -> LogosError:
    try:
        lower.lower_module(checked(source))
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this lowered: {source}")


def fail_on_emit(source: str) -> LogosError:
    """Lower and print: the gap a ``ValueError`` used to escape from."""
    try:
        llvm.emit(lower.lower_module(checked(source)))
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this emitted: {source}")


class AllocCheckTest(unittest.TestCase):
    """``alloc`` gives an owning pointer; the count is an integer."""

    def test_alloc_has_the_own_type(self):
        cm = checked("(mod m)(fn f :void () (let p (alloc :i64 4)))")
        self.assertEqual(cm.expr_types[path_of(cm, "alloc")], Own(Prim("i64")))

    def test_alloc_of_a_struct_owns_the_struct(self):
        cm = checked(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :void () (let p (alloc point 2)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "alloc")], Own(StructRef("point")))

    def test_alloc_needs_an_integer_count(self):
        error = fail_on('(mod m)(fn f :void () (let p (alloc :i64 "four")))')
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.path, "m/f/2/1")
        self.assertEqual(error.message, "alloc count must be an integer, found :str")

    def test_alloc_of_void_is_refused(self):
        error = fail_on("(mod m)(fn f :void () (let p (alloc :void 2)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "cannot allocate :void")

    def test_alloc_takes_a_type_and_a_count(self):
        error = fail_on("(mod m)(fn f :void () (let p (alloc :i64)))")
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "alloc takes a type and an element count")

    def test_an_owning_pointer_may_be_a_parameter(self):
        cm = checked("(mod m)(fn f :void ((p (own :i64))) (free p))")
        self.assertEqual(
            cm.fn_sigs["f"].params, (Own(Prim("i64")),)
        )


class PointerCheckTest(unittest.TestCase):
    """``ptr``, ``deref``, ``set-deref`` and ``ptr-add`` over both pointer kinds."""

    def test_deref_reads_an_owning_pointers_element(self):
        cm = checked(
            "(mod m)(fn f :void () (let p (alloc :i32 1)) (let x :i32 (deref p)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "deref")], Prim("i32"))

    def test_deref_reads_a_raw_pointers_element(self):
        cm = checked(
            "(mod m)(fn f :void ((p (ptr :u8))) (let x :u8 (deref p)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "deref")], Prim("u8"))

    def test_deref_needs_a_pointer(self):
        error = fail_on("(mod m)(fn f :void ((n :i64)) (let x (deref n)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "deref needs a pointer, found :i64")

    def test_set_deref_checks_the_value_against_the_element(self):
        error = fail_on(
            '(mod m)(fn f :void () (let p (alloc :i64 2)) (set-deref p "s"))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "cannot store :str through (own :i64)")

    def test_set_deref_takes_a_pointer_and_a_value(self):
        error = fail_on("(mod m)(fn f :void () (set-deref 1 2))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "deref needs a pointer, found :i64")

    def test_ptr_takes_an_owning_pointer(self):
        error = fail_on("(mod m)(fn f :void ((n :i64)) (let q (ptr n)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "ptr takes an owning pointer, found :i64")

    def test_ptr_yields_the_same_element_as_a_raw_pointer(self):
        cm = checked(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (let q (ptr p)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "ptr")], Ptr(Prim("i64")))

    def test_ptr_add_yields_a_raw_pointer(self):
        cm = checked(
            "(mod m)(fn f :void () (let p (alloc :i64 2))"
            " (let q (ptr-add p 1)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "ptr-add")], Ptr(Prim("i64")))

    def test_ptr_add_needs_an_integer_offset(self):
        error = fail_on(
            '(mod m)(fn f :void () (let p (alloc :i64 2)) (let q (ptr-add p "x")))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "pointer offset must be an integer, found :str")

    def test_ptr_add_needs_a_pointer(self):
        error = fail_on("(mod m)(fn f :void ((n :i64)) (let q (ptr-add n 1)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "ptr-add needs a pointer, found :i64")


class MoveCheckTest(unittest.TestCase):
    """A move is a transfer: the binding afterwards holds nothing."""

    def test_free_needs_an_owning_pointer(self):
        error = fail_on("(mod m)(fn f :void ((p (ptr :i64))) (free p))")
        self.assertEqual(error.code, "E0301")
        self.assertEqual(error.path, "m/f/2")
        self.assertEqual(
            error.message, "cannot free (ptr :i64): it does not own its memory"
        )

    def test_free_of_a_plain_value_is_a_type_mismatch(self):
        error = fail_on("(mod m)(fn f :void () (free 1))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "free needs an (own T), found :i64")

    def test_use_after_move_is_e0300(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (let p (alloc :i64 2)) (free p) (let x :i64 (deref p)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4/2")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_a_second_free_is_use_after_move(self):
        error = fail_on(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (free p) (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_move_out_of_an_immutable_binding_is_e0302(self):
        error = fail_on(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (let q p))"
        )
        self.assertEqual(error.code, "E0302")
        self.assertEqual(error.path, "m/f/3")
        self.assertEqual(error.message, "cannot move out of immutable binding 'p'")

    def test_a_mutable_binding_may_be_moved_and_is_empty_afterwards(self):
        error = fail_on(
            "(mod m)(fn f :void () (letmut p (alloc :i64 2)) (let q p)"
            " (let x :i64 (deref p)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_setting_a_moved_binding_is_e0300(self):
        # A ``set`` cannot re-arm a binding whose value one path may already
        # have released: the assignment is on this path only, while the move
        # may have happened on another.
        error = fail_on(
            "(mod m)(fn f :void () (letmut p (alloc :i64 2)) (free p)"
            " (set p (alloc :i64 1)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4")
        self.assertEqual(error.message, "set of moved binding 'p'")

    def test_set_in_a_sibling_branch_cannot_rearm_a_released_binding(self):
        # The ``then`` branch releases ``p``; the ``else`` branch assigning a
        # fresh block must not erase that. After the join the binding is moved
        # on one path, and reading it is a use after move at both opts.
        error = fail_on(
            "(mod m)(fn f :void ((c :bool))"
            " (letmut p (alloc :i64 2))"
            " (if c (free p) (set p (alloc :i64 1)))"
            " (let x :i64 (deref p)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4/2")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_both_branches_may_free_the_same_binding(self):
        # Each branch starts from the pre-branch state, so the second branch
        # does not see the first branch's move. Exactly one release runs.
        checked(
            "(mod m)(fn f :void ((c :bool))"
            " (let p (alloc :i64 2))"
            " (if c (free p) (free p)))"
        )

    def test_both_match_arms_may_free_the_same_binding(self):
        checked(
            "(mod m)(fn f :void ((n :i64))"
            " (let p (alloc :i64 2))"
            " (match n (0 (free p)) (_ (free p))))"
        )

    def test_a_free_in_one_match_arm_is_a_move_after_the_join(self):
        error = fail_on(
            "(mod m)(fn f :void ((n :i64))"
            " (let p (alloc :i64 2))"
            ' (match n (0 (free p)) (_ (println "kept")))'
            " (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4")

    def test_a_move_in_a_while_body_is_a_move_after_the_loop(self):
        error = fail_on(
            "(mod m)(fn f :void () (letmut p (alloc :i64 2))"
            " (while true (free p)) (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4")

    def test_a_parameter_may_be_returned(self):
        cm = checked(
            "(mod m)(fn pass :(own :i64) ((p (own :i64))) (ret p))"
        )
        self.assertEqual(cm.fn_sigs["pass"].ret, Own(Prim("i64")))

    def test_a_parameter_may_be_freed_and_is_then_moved(self):
        error = fail_on(
            "(mod m)(fn f :void ((p (own :i64))) (free p) (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_passing_an_own_value_moves_it(self):
        error = fail_on(
            "(mod m)(fn eat :void ((p (own :i64))) (free p))"
            "(fn f :void () (letmut p (alloc :i64 2)) (eat p) (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'p'")

    def test_a_move_inside_a_branch_is_visible_after_the_join(self):
        # A path that freed the block and a path that did not cannot be told
        # apart after the join, so the binding counts as moved: the conservative
        # answer is the only one that never accepts a second free.
        error = fail_on(
            "(mod m)(fn f :void ((c :bool))"
            " (letmut p (alloc :i64 2))"
            ' (if c (free p) (println "kept"))'
            " (free p))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.path, "m/f/4")

    def test_a_move_does_not_leak_into_a_shadowing_binding(self):
        checked(
            "(mod m)(fn f :void () (letmut p (alloc :i64 2))"
            " (match 0 (0 (letmut p (alloc :i64 1)) (free p)) (_ (println \"x\")))"
            " (free p))"
        )


class MemoryLoweringTest(unittest.TestCase):
    """The runtime contract, opcode by opcode."""

    def test_alloc_calls_rt_alloc_with_bytes_an_alignment_and_a_path(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :void () (let p (alloc :i64 4)))")
        )
        fn = m.functions[0]
        call = [ins for ins in calls(fn) if ins[2] == "rt_alloc"][0]
        self.assertEqual(len(call[3]), 3)
        # Four :i64 elements are sized by a multiply; :i64 is eight-byte aligned.
        text = llvm.emit(m)
        self.assertIn("mul i64 4, 8", text)
        self.assertIn("call i8* @rt_alloc(i64 %t", text)
        self.assertIn(", i64 8, i8* %t", text)
        self.assertTrue(any(g.data == b"m/f/2/1\x00" for g in m.globals))

    def test_alloc_declares_rt_alloc(self):
        text = emitted("(mod m)(fn f :void () (let p (alloc :i64 1)))")
        self.assertIn("declare i8* @rt_alloc(i64, i64, i8*)", text)

    def test_the_size_guard_refuses_a_count_that_would_overflow(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :void ((n :i64)) (let p (alloc :i64 n)))")
        )
        fn = m.functions[0]
        guards = [ins for ins in instructions(fn, "icmp") if ins[2] == "ugt"]
        self.assertEqual(len(guards), 1)
        # INT64_MAX // 8, so ``n * 8`` cannot wrap.
        self.assertEqual(guards[0][5], str((2**63 - 1) // 8))
        self.assertTrue(any(g.data == b"R0003\x00" for g in m.globals))

    def test_an_element_that_is_a_struct_is_sized_by_its_c_layout(self):
        # (type pair (struct (a :u8) (b :i64))) is sixteen bytes in C.
        text = emitted(
            "(mod m)(type pair (struct (a :u8) (b :i64)))"
            "(fn f :void () (let p (alloc pair 3)))"
        )
        self.assertIn("mul i64 3, 16", text)

    def test_str_is_eight_byte_aligned(self):
        # ``%str = { i8*, i64 }``: two eight-byte fields, so the aggregate is
        # eight-aligned even though it is sixteen bytes wide.
        self.assertEqual(lower.align_of(Prim("str"), {}), 8)

    def test_a_struct_with_a_str_field_is_sized_by_its_c_layout(self):
        # { i32, %str } is 4 bytes, 4 of padding, then sixteen: twenty-four
        # bytes, eight-aligned. The old str alignment of sixteen rounded this
        # up to thirty-two and over-allocated every such struct.
        structs: dict[str, tuple[tuple[str, Ty], ...]] = {
            "record": (("tag", Prim("i32")), ("name", Prim("str")))
        }
        self.assertEqual(lower.sizeof_of(StructRef("record"), structs), 24)
        self.assertEqual(lower.align_of(StructRef("record"), structs), 8)

    def test_an_alloc_of_a_str_struct_asks_for_its_true_layout(self):
        text = emitted(
            "(mod m)(type record (struct (tag :i32) (name :str)))"
            "(fn f :void () (let p (alloc record 2)))"
        )
        self.assertIn("mul i64 2, 24", text)
        self.assertIn(", i64 8, i8* %t", text)

    def test_deref_is_a_typed_load(self):
        text = emitted(
            "(mod m)(fn f :void () (let p (alloc :i32 1)) (let x :i32 (deref p)))"
        )
        self.assertIn("load i32, i32* %t", text)

    def test_set_deref_is_a_typed_store(self):
        text = emitted(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (set-deref p 7))"
        )
        self.assertIn("store i64 7, i64* %t", text)

    def test_ptr_add_is_a_gep_with_the_element_stride(self):
        text = emitted(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (let q (ptr-add p 1)))"
        )
        self.assertIn("= getelementptr i64, i64* %t", text)
        self.assertRegex(text, r"getelementptr i64, i64\* %t\d+, i64 1")

    def test_ptr_is_a_conversion_with_no_instruction(self):
        text = emitted(
            "(mod m)(fn f :void () (let p (alloc :i64 2)) (let q (ptr p)))"
        )
        self.assertNotIn("bitcast i64*", text)
        self.assertIn("store i64* %t", text)

    def test_free_calls_rt_free(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :void () (let p (alloc :i64 2)) (free p))")
        )
        self.assertIn("rt_free", callees(m.functions[0]))
        self.assertIn("declare void @rt_free(i8*)", llvm.emit(m))

    def test_an_own_type_is_a_pointer_in_the_ir(self):
        text = emitted(
            "(mod m)(fn take :(own :i64) ((p (own :i64))) (ret p))"
        )
        self.assertIn("define internal i64* @lg.m.take(i64* %v.0)", text)

    def test_a_debug_build_reports_leaks_before_main_returns(self):
        module = lower.lower_test(
            checked("(mod m)(fn f :void () (let p (alloc :i64 2)) (free p))"
                    "(test t (expect (code 0)) (free (alloc :i64 1)))"),
            "t",
        )
        main = [fn for fn in module.functions if fn.symbol == "main"][0]
        self.assertIn("rt_leak_report", callees(main))
        self.assertIn(
            "declare void @rt_leak_report()", llvm.emit(module)
        )

    def test_a_release_build_has_no_leak_report(self):
        module = lower.lower_module(
            checked("(mod m)(fn main :void () (let p (alloc :i64 2)) (free p))")
        )
        self.assertNotIn("rt_leak_report", callees(module.functions[0]))
        self.assertNotIn("rt_leak_report", llvm.emit(module))


class PtrLayoutGapTest(unittest.TestCase):
    """Carried forward from Task 7: a pointer must not hide a type with no layout.

    ``(ptr bad)`` used to reach ``type_text`` as ``%struct.bad*`` with no field
    walk, so a struct whose field had no layout emitted invalid IR and escaped
    as a bare ``ValueError``. The walk now descends through a pointer and the
    refusal is E0002 at the node that named the pointer.
    """

    def test_a_struct_behind_a_pointer_with_no_layout_is_e0002(self):
        error = fail_on_emit(
            "(mod m)(type bad (struct (xs (shared :i64))))"
            "(fn f :void ((p (ptr bad))))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/f/1/0")
        self.assertEqual(error.message, "(shared :i64) is not lowered yet")

    def test_an_own_pointer_to_an_unlowered_type_is_e0002(self):
        error = fail_on_emit(
            "(mod m)(type bad (struct (xs (shared :i64))))"
            "(fn f :void ((p (own bad))))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/f/1/0")
        self.assertEqual(error.message, "(shared :i64) is not lowered yet")

    def test_a_struct_that_points_at_itself_is_still_finite(self):
        text = emitted(
            "(mod m)(type node (struct (v :i64) (head (ptr node))))"
            "(fn f :void ((n node)) (ret))"
        )
        self.assertEqual(text.count("%struct.node = type"), 1)


class MemoryExecTest(unittest.TestCase):
    """Real binaries at both optimisation levels."""

    def run_test(self, source: Path, test: str, out: str, code: int, opt: str):
        target = str(ROOT / "build" / f"mem-{out}-{opt[1:]}")
        driver.run_test(str(source), test, target, opt=opt)
        return subprocess.run([target], capture_output=True, text=True)

    def check(self, source: Path, test: str, out: str, code: int):
        for opt in ("-O0", "-O2"):
            with self.subTest(test=test, opt=opt):
                done = self.run_test(source, test, f"{source.stem}-{test}", code, opt)
                self.assertEqual(done.stdout, out, done.stderr)
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_a_released_block_is_printed_and_not_reported(self):
        self.check(LEAK, "no-leak", "seven\n", 0)

    def test_a_raw_pointer_walks_the_block(self):
        self.check(LEAK, "array-elements", "three\n", 0)

    def test_ownership_crosses_a_call(self):
        self.check(LEAK, "handoff", "ok\n", 0)

    def test_a_test_build_reports_each_leak(self):
        case = "leak-reported"
        style = checked(LEAK.read_text(encoding="utf-8"))
        expected = f"LEAK 32 bytes at {path_in(style, case, 'alloc')}\n"
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(LEAK, case, f"leak-{case}", 0, opt)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(expected, done.stderr)

    def test_a_zero_byte_leak_reports_zero_bytes(self):
        # The record keeps the requested size, not the one byte the allocator
        # was asked for so two zero-byte blocks cannot share an address.
        case = "zero-byte-leak"
        style = checked(LEAK.read_text(encoding="utf-8"))
        expected = f"LEAK 0 bytes at {path_in(style, case, 'alloc')}\n"
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(LEAK, case, f"leak-{case}", 0, opt)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(expected, done.stderr)

    def test_a_str_struct_leak_reports_its_true_size(self):
        # (struct (tag :i32) (name :str)) is { i32, %str }: 4 + 4 pad + 16,
        # so twenty-four bytes. Sixteen-aligned str rounded it up to 32.
        case = "struct-leak"
        style = checked(LEAK.read_text(encoding="utf-8"))
        expected = f"LEAK 24 bytes at {path_in(style, case, 'alloc')}\n"
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(LEAK, case, f"leak-{case}", 0, opt)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(expected, done.stderr)

    def test_a_count_that_wrapped_negative_panics_r0003(self):
        # INT64_MAX + 1 wraps to INT64_MIN; read unsigned that is above the
        # limit, so the guard fires at the alloc's own path.
        case = "wrapped-count-panics"
        style = checked(LEAK.read_text(encoding="utf-8"))
        where = path_in(style, case, "alloc")
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(LEAK, case, f"leak-{case}", 0, opt)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(
                    done.stderr,
                    f"{where}: R0003: allocation size overflows\n",
                )

    def test_a_stale_copy_after_a_new_allocation_panics_r0002(self):
        # The block is freed, a new one is allocated, and only then does the
        # stale copy reach rt_free. In a debug build the freed address cannot
        # be recycled, so the dead record is still there and the panic names
        # the original allocation.
        case = "stale-copy-after-reuse-panics"
        style = checked(DOUBLE_FREE.read_text(encoding="utf-8"))
        first = path_in(style, case, "alloc")
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(DOUBLE_FREE, case, f"double-free-{case}", 0, opt)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(
                    done.stderr, f"{first}: R0002: double free\n"
                )

    def test_a_release_build_says_nothing_about_leaks(self):
        source = ROOT / "build" / "mem-release.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod mem-release)\n"
            "(fn main :void ()\n"
            "  (let p (alloc :i64 4))\n"
            "  (set-deref p 1))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"mem-release-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = subprocess.run([out], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stderr, "")

    def test_a_double_free_panics_with_r0002(self):
        case = "double-free-panics"
        style = checked(DOUBLE_FREE.read_text(encoding="utf-8"))
        # The panic names where the block was allocated, which is the one place
        # a person can find the pointer that was handed back twice.
        first = path_in(style, case, "alloc")
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_test(DOUBLE_FREE, case, "double-free", 0, opt)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertTrue(
                    done.stderr.startswith(f"{first}: R0002: double free"),
                    done.stderr,
                )


class RuntimeTrackerTest(unittest.TestCase):
    """The C contract of the tracker, exercised without the compiler in between.

    ``rt_leak_count`` is the interface Task 13's harness and later tasks read,
    and the runtime is reusable C: these probes call it the way any caller
    would, so the promise in ``rt.h`` is checked on its own terms and not only
    through the paths this week's lowering happens to use.
    """

    PROBE = r"""
#include "rt.h"
#include <stdint.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc < 2) return 90;
    if (strcmp(argv[1], "count") == 0) {
        void *a = rt_alloc(8, 8, "m/a");
        void *b = rt_alloc(16, 8, "m/b");
        if (a == NULL || b == NULL) return 91;
        if (rt_leak_count() != 2) return 10;
        rt_free(a);
        if (rt_leak_count() != 1) return 11;
        rt_free(b);
        if (rt_leak_count() != 0) return 12;
        return 0;
    }
    if (strcmp(argv[1], "double") == 0) {
        void *a = rt_alloc(8, 8, "m/a");
        rt_free(a);
        rt_free(a);
        return 13;
    }
    if (strcmp(argv[1], "report") == 0) {
        void *a = rt_alloc(3, 1, "m/first");
        void *b = rt_alloc(32, 8, "m/second");
        (void)b;
        rt_free(a);
        rt_alloc(7, 4, "m/third");
        rt_leak_report();
        return 0;
    }
    if (strcmp(argv[1], "zero") == 0) {
        void *a = rt_alloc(0, 1, "m/zero");
        if (a == NULL) return 17;
        rt_leak_report();
        return 0;
    }
    if (strcmp(argv[1], "quarantine") == 0) {
        void *a = rt_alloc(8, 8, "m/a");
        void *b;
        rt_free(a);
        /* A debug runtime never returns a freed block to malloc, so the next
         * block cannot land on the released address. */
        b = rt_alloc(8, 8, "m/b");
        if (b == a) return 14;
        rt_free(a);
        return 15;
    }
    if (strcmp(argv[1], "unknown") == 0) {
        rt_free((void *)(uintptr_t)1);
        return 16;
    }
    return 92;
}
"""

    @classmethod
    def build(cls) -> str:
        source = ROOT / "build" / "mem-probe.c"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(cls.PROBE, encoding="utf-8")
        binary = str(ROOT / "build" / "mem-probe")
        done = subprocess.run(
            [
                "clang",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                # The tracker's exactness is a debug-build promise, and the
                # quarantine is what makes it true; test what ships in a test
                # build, not the release fallback.
                "-DLOGOS_DEBUG=1",
                "-I",
                str(ROOT / "runtime"),
                str(source),
                str(ROOT / "runtime" / "rt.c"),
                "-o",
                binary,
            ],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise AssertionError("the runtime did not build:\n" + done.stderr)
        return binary

    def test_the_count_falls_back_to_zero(self):
        done = subprocess.run([self.build(), "count"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_double_free_from_c_panics_with_the_allocation_path(self):
        done = subprocess.run([self.build(), "double"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, "m/a: R0002: double free\n")

    def test_the_report_lists_live_blocks_in_allocation_order(self):
        done = subprocess.run(
            [self.build(), "report"], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stderr, "LEAK 32 bytes at m/second\nLEAK 7 bytes at m/third\n"
        )

    def test_a_zero_byte_request_is_recorded_as_zero(self):
        done = subprocess.run([self.build(), "zero"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "LEAK 0 bytes at m/zero\n")

    def test_a_freed_address_is_quarantined_so_a_stale_copy_still_panics(self):
        # R0002 must hold through a stale copy after another allocation: the
        # debug runtime keeps the freed block, so the new one cannot reuse its
        # address, and the stale free finds the original (dead) record.
        done = subprocess.run(
            [self.build(), "quarantine"], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(done.stderr, "m/a: R0002: double free\n")

    def test_free_of_a_never_allocated_pointer_is_r0004(self):
        done = subprocess.run(
            [self.build(), "unknown"], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertEqual(
            done.stderr,
            "<runtime>: R0004: free of a pointer that was never allocated\n",
        )


if __name__ == "__main__":
    unittest.main()
