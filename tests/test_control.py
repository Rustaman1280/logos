"""Control flow: ``if``, ``while``, ``match``, ``break``/``continue``, logic.

Three layers are pinned here, one test class each: what the checker accepts and
rejects, what the lowerer emits (block structure, branch predicates, aggregate
values), and what a real binary does at ``-O0`` and ``-O2``. The exec recipes in
``tests/exec`` carry the same expectations in their ``(expect ...)`` directives;
that harness is Task 13's, so the strings are restated here rather than parsed.
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
from logos.types import Prim

ROOT = Path(__file__).resolve().parents[1]
EXEC = ROOT / "tests" / "exec"
LOOPS = EXEC / "loops.logos"
MATCH = EXEC / "match.logos"
DIVZERO = EXEC / "divzero.logos"


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def path_of(cm: CheckedModule, kind: str) -> str:
    """The node path of the first node of ``kind`` in ``cm``."""
    for node in cm.module.root.walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in the module")


def ops(fn: ir.IrFn) -> list[str]:
    return [instr[0] for block in fn.blocks for instr in block.instrs]


def calls(fn: ir.IrFn) -> list[tuple]:
    return [instr for block in fn.blocks for instr in block.instrs if instr[0] == "call"]


def labels(fn: ir.IrFn) -> list[str]:
    return [block.label for block in fn.blocks]


def run(binary: str) -> subprocess.CompletedProcess:
    return subprocess.run([binary], capture_output=True, text=True)


def fail_on(source: str) -> LogosError:
    """The diagnostic ``source`` raises; the test fails if it checks."""
    try:
        checked(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this checked: {source}")


class ControlFlowCheckTest(unittest.TestCase):
    """The checker's share: loop jumps need a loop, logic needs bools."""

    def test_break_outside_a_loop_is_rejected(self):
        error = fail_on("(mod m)(fn f :void () (break))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/f/2")
        self.assertEqual(error.message, "'break' outside a loop")

    def test_continue_outside_a_loop_is_rejected(self):
        error = fail_on("(mod m)(fn f :void () (continue))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "'continue' outside a loop")

    def test_a_loop_jump_takes_no_operand(self):
        error = fail_on("(mod m)(fn f :void () (while true (break 1)))")
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.path, "m/f/2/1")
        self.assertEqual(error.message, "'break' takes no operands")

    def test_a_loop_jump_in_a_match_inside_a_loop_is_inside_the_loop(self):
        checked(
            "(mod m)(fn f :void ()"
            " (while true (match 1 (0 (break)) (_ (continue)))))"
        )

    def test_and_and_or_take_two_bools(self):
        for source, code in (
            ("(mod m)(fn f :bool ((a :bool)) (ret (and a)))", "E0102"),
            ("(mod m)(fn f :bool ((a :bool)) (ret (and a a a)))", "E0102"),
            ("(mod m)(fn f :bool ((a :i64)) (ret (and a true)))", "E0100"),
            ("(mod m)(fn f :bool ((a :bool)) (ret (or 1 a)))", "E0100"),
        ):
            with self.subTest(source=source):
                self.assertEqual(fail_on(source).code, code)

    def test_not_takes_one_bool(self):
        self.assertEqual(fail_on("(mod m)(fn f :bool () (ret (not)))").code, "E0102")
        self.assertEqual(
            fail_on("(mod m)(fn f :bool ((a :i64)) (ret (not a)))").code, "E0100"
        )
        checked("(mod m)(fn f :bool ((a :bool)) (ret (not a)))")

    def test_logic_returns_bool(self):
        cm = checked("(mod m)(fn f :bool ((a :bool)) (ret (and (or a a) (not a))))")
        self.assertEqual(cm.expr_types[path_of(cm, "and")], Prim("bool"))
        self.assertEqual(cm.expr_types[path_of(cm, "not")], Prim("bool"))

    def test_match_needs_an_integer_scrutinee(self):
        error = fail_on('(mod m)(fn f :str ((s :str)) (ret (match s (0 "no"))))')
        self.assertEqual(error.code, "E0100")
        self.assertIn("'match' needs an integer", error.message)

    def test_match_needs_at_least_one_arm(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (match n)))")
        self.assertEqual(error.code, "E0102")

    def test_an_arm_needs_a_pattern_and_a_body(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (match n (0))))")
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.path, "m/f/2/0/1")

    def test_a_pattern_is_an_integer_literal_or_wildcard(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (match n (a 1))))")
        self.assertEqual(error.code, "E0002")
        self.assertIn("integer literal or '_'", error.message)

    def test_an_arm_after_the_wildcard_is_rejected(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (match n (_ 1) (0 2))))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/f/2/0/2")
        self.assertEqual(error.message, "an arm after '_' can never match")

    def test_a_pattern_must_fit_the_scrutinee_width(self):
        error = fail_on("(mod m)(fn f :u8 ((n :u8)) (ret (match n (256 1) (_ 2))))")
        self.assertEqual(error.code, "E0106")
        self.assertEqual(error.message, "literal 256 does not fit :u8")

    def test_match_arms_must_agree(self):
        error = fail_on(
            '(mod m)(fn f :str ((n :i64)) (ret (match n (0 "a") (_ 1))))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("match arms do not agree", error.message)

    def test_a_match_is_void_when_its_arms_are_void(self):
        cm = checked(
            '(mod m)(fn f :void ((n :i64)) (match n (0 (println "a")) (_ (println "b"))))'
        )
        self.assertEqual(cm.expr_types[path_of(cm, "match")], Prim("void"))

    def test_a_match_arm_may_be_a_block_whose_last_item_is_its_value(self):
        cm = checked(
            "(mod m)(fn f :i64 ((n :i64))"
            " (ret (match n (0 (let x :i64 1) (+ x 1)) (_ 9))))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "match")], Prim("i64"))

    def test_string_equality_is_lowered_as_a_byte_comparison(self):
        # The operator and its lowering landed together (Task 9): ``=`` on two
        # strings is the same length test and ``memcmp`` that ``str-eq`` is.
        for op, source in (
            ("=", '(mod m)(fn f :bool () (ret (= "a" "b")))'),
            ("=", '(mod m)(fn f :bool ((s :str)) (ret (= s "a")))'),
            ("!=", '(mod m)(fn f :bool ((s :str)) (ret (!= s "a")))'),
        ):
            with self.subTest(source=source):
                text = llvm.emit(lower.lower_module(checked(source)))
                self.assertIn("call i32 @memcmp(i8*", text)
                self.assertIn("icmp eq i64", text)

    def test_a_binding_from_an_if_branch_is_not_visible_after_the_join(self):
        error = fail_on(
            "(mod m)(fn f :i64 ((c :bool))"
            " (if c (let x :i64 1) (let x :i64 2)) (ret x))"
        )
        self.assertEqual(error.code, "E0005")
        self.assertEqual(error.path, "m/f/3")
        self.assertEqual(error.message, "unknown name 'x'")

    def test_a_binding_from_a_loop_body_is_not_visible_after_the_loop(self):
        error = fail_on(
            "(mod m)(fn f :i64 () (letmut i :i64 0)"
            " (while (< i 1) (let x :i64 1) (set i 1)) (ret x))"
        )
        self.assertEqual(error.code, "E0005")
        self.assertEqual(error.path, "m/f/4")
        self.assertEqual(error.message, "unknown name 'x'")

    def test_a_binding_from_a_match_arm_is_not_visible_after_the_match(self):
        error = fail_on(
            "(mod m)(fn f :i64 ((n :i64))"
            ' (match n (0 (let x :i64 1) (println "a")) (_ (println "b")))'
            " (ret x))"
        )
        self.assertEqual(error.code, "E0005")
        self.assertEqual(error.path, "m/f/3")
        self.assertEqual(error.message, "unknown name 'x'")

    def test_a_nested_block_may_shadow_an_outer_binding(self):
        # Shadowing is legal inside the block that declares the inner name;
        # the outer binding is untouched when the block ends.
        checked(
            "(mod m)(fn f :i64 () (letmut x :i64 1)"
            " (match 0 (0 (letmut x :i64 2) (set x 3)) (_ (set x 4)))"
            " (ret x))"
        )
        checked(
            "(mod m)(fn f :i64 () (letmut i :i64 0) (let x :i64 1)"
            " (while (< i 1) (let x :i64 2) (set i x)) (ret x))"
        )


class ControlFlowLoweringTest(unittest.TestCase):
    """The IR shape: every block named, every branch explicit."""

    def test_an_if_takes_its_value_from_a_slot_on_one_join_path(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :i64 ((a :bool)) (ret (if a 1 2)))")
        )
        fn = m.functions[0]
        names = labels(fn)
        self.assertIn("if1.then", names)
        self.assertIn("if1.else", names)
        self.assertIn("if1.join", names)
        self.assertIn("cbr", [block.term[0] for block in fn.blocks])
        text = llvm.emit(m)
        self.assertIn("br i1 %t", text)
        # The two branches converge on a load, not on a phi: an alloca and a
        # load stay correct when a branch ends in a terminator.
        self.assertEqual(ops(fn)[-1], "load")

    def test_a_while_branches_back_to_its_head(self):
        m = lower.lower_module(
            checked(
                "(mod m)(fn f :void ((n :i64))"
                " (letmut i :i64 0)"
                " (while (< i n) (set i (+ i 1))))"
            )
        )
        fn = m.functions[0]
        self.assertIn("while1.head", labels(fn))
        self.assertIn("while1.body", labels(fn))
        self.assertIn("while1.exit", labels(fn))
        self.assertIn(("br", "while1.head"), [block.term for block in fn.blocks])
        # The body must be entered only from the conditional branch.
        head = next(b for b in fn.blocks if b.label == "while1.head")
        self.assertEqual(head.term[0], "cbr")
        self.assertEqual(head.term[2], "while1.body")
        self.assertEqual(head.term[3], "while1.exit")

    def test_break_and_continue_branch_to_the_exit_and_the_head(self):
        m = lower.lower_module(
            checked(
                "(mod m)(fn f :void ((n :i64))"
                " (letmut i :i64 0)"
                " (while (< i n) (set i (+ i 1)) (if (> i 3) (break) (continue))))"
            )
        )
        terms = [block.term for block in m.functions[0].blocks]
        self.assertIn(("br", "while1.exit"), terms)
        self.assertIn(("br", "while1.head"), terms)

    def test_a_break_leaves_only_the_innermost_loop(self):
        m = lower.lower_module(
            checked(
                "(mod m)(fn f :void ((n :i64))"
                " (while (< n 4) (while true (break)) (break)))"
            )
        )
        terms = [block.term for block in m.functions[0].blocks]
        self.assertIn(("br", "while1.exit"), terms)
        self.assertIn(("br", "while2.exit"), terms)

    def test_comparisons_pick_the_signed_or_unsigned_predicate(self):
        cases = (
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (< a b)))", "slt"),
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (<= a b)))", "sle"),
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (> a b)))", "sgt"),
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (>= a b)))", "sge"),
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (= a b)))", "eq"),
            ("(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (!= a b)))", "ne"),
            ("(mod m)(fn f :bool ((a :u32) (b :u32)) (ret (< a b)))", "ult"),
            ("(mod m)(fn f :bool ((a :u8) (b :u8)) (ret (>= a b)))", "uge"),
        )
        for source, predicate in cases:
            with self.subTest(predicate=predicate):
                fn = lower.lower_module(checked(source)).functions[0]
                icmps = [
                    instr
                    for block in fn.blocks
                    for instr in block.instrs
                    if instr[0] == "icmp"
                ]
                self.assertEqual(icmps[-1][2], predicate)

    def test_a_comparison_adopts_the_width_of_its_non_literal_operand(self):
        fn = lower.lower_module(
            checked("(mod m)(fn f :bool ((a :u8)) (ret (< 200 a)))")
        ).functions[0]
        icmp = [i for b in fn.blocks for i in b.instrs if i[0] == "icmp"][-1]
        self.assertEqual(icmp[3], Prim("u8"))

    def test_a_bool_comparison_uses_i1(self):
        fn = lower.lower_module(
            checked("(mod m)(fn f :bool ((a :bool) (b :bool)) (ret (= a b)))")
        ).functions[0]
        icmp = [i for b in fn.blocks for i in b.instrs if i[0] == "icmp"][-1]
        self.assertEqual(icmp[3], Prim("bool"))

    def test_and_short_circuits_its_right_hand_side(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :bool ((a :bool) (b :bool)) (ret (and a b)))")
        )
        fn = m.functions[0]
        self.assertIn("and1.rhs", labels(fn))
        self.assertIn("and1.join", labels(fn))
        rhs = next(b for b in fn.blocks if b.label == "and1.rhs")
        self.assertEqual(rhs.term, ("br", "and1.join"))
        join = next(b for b in fn.blocks if b.label == "and1.join")
        self.assertEqual(join.instrs[-1][0], "phi")
        incoming = join.instrs[-1][3]
        self.assertEqual([label for _, label in incoming], ["and1.rhs", "entry"])

    def test_or_short_circuits_its_right_hand_side(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :bool ((a :bool) (b :bool)) (ret (or a b)))")
        )
        fn = m.functions[0]
        join = next(b for b in fn.blocks if b.label == "or1.join")
        incoming = join.instrs[-1][3]
        self.assertEqual([label for _, label in incoming], ["entry", "or1.rhs"])

    def test_not_is_an_icmp_against_false(self):
        fn = lower.lower_module(
            checked("(mod m)(fn f :bool ((a :bool)) (ret (not a)))")
        ).functions[0]
        icmp = [i for b in fn.blocks for i in b.instrs if i[0] == "icmp"][-1]
        self.assertEqual(icmp[2], "eq")
        self.assertEqual(icmp[5], "false")

    def test_a_match_tests_each_literal_in_order_and_stores_one_value(self):
        m = lower.lower_module(
            checked('(mod m)(fn f :str ((n :i64)) (ret (match n (0 "a") (_ "b"))))')
        )
        fn = m.functions[0]
        self.assertIn("match1.test0", labels(fn))
        self.assertIn("match1.body0", labels(fn))
        self.assertIn("match1.body1", labels(fn))
        self.assertIn("match1.join", labels(fn))
        self.assertEqual(ops(fn)[-1], "load")
        text = llvm.emit(m)
        self.assertIn("icmp eq i64", text)
        self.assertIn("ret { i8*, i64 } %t", text)

    def test_a_wildcard_arm_needs_no_comparison_and_no_panic(self):
        m = lower.lower_module(
            checked('(mod m)(fn f :str ((n :i64)) (ret (match n (0 "a") (_ "b"))))')
        )
        self.assertNotIn("rt_panic", [c[2] for c in calls(m.functions[0])])

    def test_a_match_with_no_wildcard_panics_on_the_unmatched_path(self):
        m = lower.lower_module(
            checked('(mod m)(fn f :str ((n :i64)) (ret (match n (0 "a"))))')
        )
        fn = m.functions[0]
        self.assertIn("rt_panic", [c[2] for c in calls(fn)])
        self.assertTrue(any(g.data == b"R0003\x00" for g in m.globals))
        self.assertIn(("unreachable",), [block.term for block in fn.blocks])

    def test_a_string_literal_is_an_aggregate_over_a_private_global(self):
        m = lower.lower_module(
            checked('(mod m)(fn main :void () (let s "hi") (println s))')
        )
        text = llvm.emit(m)
        self.assertIn("%v.0.addr = alloca { i8*, i64 }", text)
        self.assertIn("insertvalue { i8*, i64 } undef, i8* %t", text)
        self.assertIn("insertvalue { i8*, i64 } %t", text)
        self.assertIn("extractvalue { i8*, i64 } %t", text)
        self.assertIn("store { i8*, i64 } %t", text)
        self.assertEqual(
            [c[2] for c in calls(m.functions[0])], ["rt_print_str", "rt_println"]
        )

    def test_a_str_value_carries_its_byte_count_not_its_character_count(self):
        m = lower.lower_module(
            checked('(mod m)(fn main :void () (let s "\u00e9\u2192") (println s))')
        )
        text = llvm.emit(m)
        # "é→" is two characters and five bytes.
        self.assertIn("insertvalue { i8*, i64 } %t", text)
        self.assertIn(", i64 5, 1", text)

    def test_a_str_parameter_and_a_str_return_keep_the_aggregate(self):
        m = lower.lower_module(
            checked(
                "(mod m)(fn id :str ((s :str)) (ret s))"
                '(fn main :void () (println (id "x")))'
            )
        )
        text = llvm.emit(m)
        self.assertIn("define internal { i8*, i64 } @lg.m.id({ i8*, i64 } %v.0)", text)
        self.assertIn("call { i8*, i64 } @lg.m.id({ i8*, i64 } %t", text)

    def test_every_alloca_is_in_the_entry_block(self):
        """A ``let`` inside a loop must not grow the stack once per iteration."""

        m = lower.lower_module(
            checked(
                "(mod m)(fn f :void ((n :i64))"
                " (letmut i :i64 0)"
                " (while (< i n) (let x :i64 (+ i 1)) (set i x)))"
            )
        )
        fn = m.functions[0]
        self.assertEqual(
            [i[0] for i in fn.blocks[0].instrs].count("alloca"), 3
        )
        for block in fn.blocks[1:]:
            self.assertNotIn("alloca", [i[0] for i in block.instrs])

    def test_an_unannotated_binding_keeps_the_width_of_the_name_it_copies(self):
        m = lower.lower_module(
            checked("(mod m)(fn f :i8 ((a :i8)) (let b a) (ret b))")
        )
        self.assertIn("%v.1.addr = alloca i8", llvm.emit(m))

    def test_a_type_with_no_layout_is_e0002_not_a_value_error(self):
        # ``(vec T)`` has a layout now (Task 9); ``(opt T)`` is the vocabulary
        # this backend still cannot spell, and it must still be a diagnostic.
        with self.assertRaises(LogosError) as caught:
            lower.lower_module(
                checked("(mod m)(fn f :void ((v :(opt :i64))) (ret))")
            )
        error = caught.exception
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "(opt :i64) is not lowered yet")
        self.assertEqual(error.path, "m/f/1/0")

    def test_a_constant_true_loop_has_no_exit_edge(self):
        # Nothing can leave the loop, so the head branches straight to the
        # body and the exit block is unreachable; the function cannot fall off
        # its end even though it returns only inside the loop.
        m = lower.lower_module(checked("(mod m)(fn f :i64 () (while true (ret 1)))"))
        fn = m.functions[0]
        head = next(b for b in fn.blocks if b.label == "while1.head")
        self.assertEqual(head.term, ("br", "while1.body"))
        self.assertNotIn("cbr", [b.term[0] for b in fn.blocks])
        exit_block = next(b for b in fn.blocks if b.label == "while1.exit")
        self.assertEqual(exit_block.term, ("unreachable",))
        llvm.emit(m)  # the printer accepts every block

    def test_a_break_still_reaches_the_exit_of_a_constant_true_loop(self):
        # A ``break`` is the only edge into the exit, so it must not be closed
        # as unreachable when one exists.
        m = lower.lower_module(
            checked("(mod m)(fn f :i64 () (while true (break)) (ret 2))")
        )
        fn = m.functions[0]
        head = next(b for b in fn.blocks if b.label == "while1.head")
        self.assertEqual(head.term, ("br", "while1.body"))
        exit_block = next(b for b in fn.blocks if b.label == "while1.exit")
        self.assertNotEqual(exit_block.term, ("unreachable",))
        self.assertEqual(exit_block.term, ("ret", Prim("i64"), "2"))


class ControlFlowExecTest(unittest.TestCase):
    """Real binaries, both optimisation levels, stdout and status compared."""

    def check(self, source: Path, test: str, out: str, code: int) -> str:
        binary = str(ROOT / "build" / f"ctl-{source.stem}-{test}")
        for opt in ("-O0", "-O2"):
            with self.subTest(source=source.name, test=test, opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(source), test, target, opt=opt)
                done = run(target)
                self.assertEqual(done.stdout, out, done.stderr)
                self.assertEqual(done.returncode, code, done.stderr)
        return binary

    def test_loops_fizzbuzz(self):
        self.check(
            LOOPS,
            "fizzbuzz",
            "n\nn\nfizz\nn\nn\nfizz\nn\nn\nfizz\nn\nn\nfizz\nn\nn\nfizz\n",
            0,
        )

    def test_loops_break_and_continue(self):
        self.check(
            LOOPS,
            "break-and-continue",
            "body\nwait\nbody\nwait\nbody\nwait\nbody\nwait\nbody\ndone\n",
            0,
        )

    def test_loops_logic_short_circuits_at_run_time(self):
        # The right-hand side of the ``and`` divides by zero; if it ran, the
        # process would exit 2 instead of printing ``ok``.
        self.check(LOOPS, "logic", "yes\nok\n", 0)

    def test_loops_if_value(self):
        self.check(LOOPS, "if-value", "small\nbig\n", 0)

    def test_match_returns_a_str_value(self):
        self.check(MATCH, "match-returns-a-value", "zero\none\nminus\nmany\n", 0)

    def test_match_arms_are_statements(self):
        self.check(MATCH, "match-arms-are-statements", "low\nlow\nhigh\n", 0)

    def test_a_match_arm_may_hold_a_block(self):
        self.check(MATCH, "match-arm-has-a-block-body", "two\n", 0)

    def test_division_by_a_runtime_zero_panics_with_the_operation_path(self):
        style = checked(DIVZERO.read_text(encoding="utf-8"))
        case = "div-by-zero-panics"
        binary = str(ROOT / "build" / "ctl-divzero-div")
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(DIVZERO), case, target, opt=opt)
                done = run(target)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn("R0001: division by zero", done.stderr)
                self.assertTrue(done.stderr.startswith(path_of(style, "/") + ": "))

    def test_modulo_by_a_runtime_zero_panics_with_the_operation_path(self):
        style = checked(DIVZERO.read_text(encoding="utf-8"))
        case = "modulo-by-zero-panics"
        binary = str(ROOT / "build" / "ctl-divzero-mod")
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(DIVZERO), case, target, opt=opt)
                done = run(target)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertTrue(done.stderr.startswith(path_of(style, "%") + ": "))

    def test_a_long_loop_with_a_binding_inside_does_not_exhaust_the_stack(self):
        """Two million iterations allocate one slot, not two million."""

        source = ROOT / "build" / "stack.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod stack)\n"
            "(fn main :i64 ()\n"
            "  (letmut i :i64 0)\n"
            "  (while (< i 2000000)\n"
            "    (let x :i64 i)\n"
            "    (set i (+ x 1)))\n"
            "  (ret 0))\n",
            encoding="utf-8",
        )
        out = str(ROOT / "build" / "ctl-stack")
        driver.build(str(source), out, opt="-O0")
        done = run(out)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_bound_string_can_be_reassigned_and_printed(self):
        source = ROOT / "build" / "strbind.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod strbind)\n"
            "(fn main :i64 ()\n"
            '  (letmut s :str "\u00e9\u2192")\n'
            "  (println s)\n"
            '  (set s "ok")\n'
            "  (println s)\n"
            "  (ret 0))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"ctl-strbind-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = subprocess.run([out], capture_output=True)
                self.assertEqual(done.stdout, "\u00e9\u2192\nok\n".encode("utf-8"))
                self.assertEqual(done.returncode, 0)

    def test_an_if_whose_branches_both_return_is_not_a_missing_return(self):
        """Every branch rets, so the join is unreachable rather than unclosed."""

        source = ROOT / "build" / "early.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod early)\n"
            "(fn sign :i64 ((n :i64))\n"
            "  (if (< n 0) (ret -1) (ret 1)))\n"
            "(fn main :i64 ()\n"
            "  (ret (sign -5)))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"ctl-early-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = run(out)
                self.assertEqual(done.returncode, 255, done.stderr)  # -1
                self.assertEqual(done.stdout, "")

    def test_a_constant_true_loop_is_not_a_missing_return(self):
        """The only way out of the loop returns, so the function cannot fall off."""

        source = ROOT / "build" / "forever.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod forever)\n"
            "(fn main :i64 ()\n"
            "  (while true (ret 7)))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"ctl-forever-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = run(out)
                self.assertEqual(done.returncode, 7, done.stderr)
                self.assertEqual(done.stdout, "")

    def test_a_branch_binding_shadows_without_retargeting_the_outer_name(self):
        """The arm's ``x`` is 2 inside the arm; the outer ``x`` is still 1."""

        source = ROOT / "build" / "shadowbranch.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod shadowbranch)\n"
            "(fn main :i64 ()\n"
            "  (let x :i64 1)\n"
            "  (letmut y :i64 0)\n"
            "  (match 0\n"
            "    (0 (let x :i64 2) (set y x))\n"
            "    (_ (set y 9)))\n"
            "  (ret (+ y x)))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"ctl-shadowbranch-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = run(out)
                self.assertEqual(done.returncode, 3, done.stderr)
                self.assertEqual(done.stdout, "")

    def test_a_match_whose_arms_all_return_is_not_a_missing_return(self):
        source = ROOT / "build" / "earlymatch.logos"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "(mod earlymatch)\n"
            "(fn pick :i64 ((n :i64))\n"
            "  (match n (1 (ret 1)) (_ (ret -1))))\n"
            "(fn main :i64 ()\n"
            "  (ret (pick 1)))\n",
            encoding="utf-8",
        )
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                out = str(ROOT / "build" / f"ctl-earlymatch-{opt[1:]}")
                driver.build(str(source), out, opt=opt)
                done = run(out)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertEqual(done.stdout, "")


if __name__ == "__main__":
    unittest.main()
