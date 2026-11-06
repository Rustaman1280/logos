"""Generics and closures: the Task 10 surface, in three layers.

The checker's half — a type variable in a signature, the copy each call site
fixes, the ceiling and the unbound-variable diagnostic; the lowerer's half —
the mangled symbol of a copy and the two words of a function value; and real
binaries at ``-O0`` and ``-O2``.

The negative corpus lives in ``tests/generics``: one file per diagnostic, its
``.expect`` naming the code, exactly as ``tests/type`` does for the rest of the
checker's refusals.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from logos import driver, ir, llvm, lower
from logos.check import (
    MAX_INSTANCES,
    CheckedModule,
    check_module,
    mangle_instance,
)
from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import resolve
from logos.types import (
    FnTy,
    Prim,
    StructRef,
    TypeVar,
    Vec,
    substitute,
    typevars,
)

ROOT = Path(__file__).resolve().parents[1]
EXEC = ROOT / "tests" / "exec" / "closures.logos"
GENERICS = ROOT / "tests" / "generics"


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def fail_on(source: str) -> LogosError:
    """The checker diagnostic ``source`` raises; fails if it checks."""
    try:
        checked(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this checked: {source}")


def lowered(source: str) -> ir.IrModule:
    return lower.lower_module(checked(source))


def emitted(source: str) -> str:
    return llvm.emit(lowered(source))


def fn_named(module: ir.IrModule, symbol: str) -> ir.IrFn:
    for fn in module.functions:
        if (fn.symbol or fn.name) == symbol:
            return fn
    raise AssertionError(f"no function '{symbol}' in {[f.symbol for f in module.functions]}")


def ops(fn: ir.IrFn) -> list[tuple]:
    return [i for block in fn.blocks for i in block.instrs]


def calls(fn: ir.IrFn) -> list[str]:
    return [i[2] for i in ops(fn) if i[0] == "call"]


def many_types(count: int) -> str:
    """A module whose generic ``tag`` is instantiated ``count`` times.

    Each instantiation differs by a struct type, because the built-in
    vocabulary is smaller than the ceiling: 257 distinct arrays or pointers
    would need a value of each, and one empty struct per copy is the shortest
    program that really asks for 257 copies.
    """
    types = "".join(f"(type t{n} (struct))" for n in range(count))
    calls_ = "".join(f"(tag (make t{n}))" for n in range(count))
    return (
        f"(mod m){types}"
        "(fn tag :i64 ((x :T)) (ret 0))"
        f"(fn main :i64 (){calls_} (ret 0))"
    )


class TypeSubstitutionTest(unittest.TestCase):
    """The two pure operations the checker is built on."""

    def test_substitute_replaces_every_bound_variable(self):
        ty = FnTy((Vec(TypeVar("T")),), TypeVar("U"))
        self.assertEqual(
            substitute(ty, {"T": Prim("i64"), "U": Prim("bool")}),
            FnTy((Vec(Prim("i64")),), Prim("bool")),
        )

    def test_substitute_leaves_an_unbound_variable_alone(self):
        self.assertEqual(substitute(TypeVar("T"), {}), TypeVar("T"))

    def test_typevars_are_in_first_appearance_order(self):
        ty = FnTy((Vec(TypeVar("T")), TypeVar("U")), TypeVar("T"))
        self.assertEqual(typevars(ty), ["T", "U"])

    def test_a_mangled_name_spells_the_primitive_bare(self):
        self.assertEqual(
            mangle_instance("map", (Prim("i64"), Prim("str"))), "map$i64$str"
        )

    def test_a_mangled_name_distinguishes_named_and_built_in_types(self):
        self.assertEqual(mangle_instance("f", (StructRef("i64"),)), "f$s:i64")


class GenericSignatureTest(unittest.TestCase):
    def test_a_type_variable_makes_a_generic_signature(self):
        cm = checked("(mod m)(fn id :T ((x :T)) (ret x))")
        self.assertEqual(cm.fn_sigs["id"], FnTy((TypeVar("T"),), TypeVar("T")))
        self.assertEqual(cm.generic, frozenset({"id"}))
        self.assertEqual(cm.instances, {})

    def test_either_spelling_of_a_variable_names_the_same_type(self):
        # ``T`` and ``:T`` are one type: the colon marks the primitive
        # vocabulary, and a single capital is the one name in it that is not a
        # primitive.
        bare = checked("(mod m)(fn id :T ((x T)) (ret x))")
        colon = checked("(mod m)(fn id :T ((x :T)) (ret x))")
        self.assertEqual(bare.fn_sigs, colon.fn_sigs)
        self.assertEqual(bare.fn_sigs["id"].params[0], TypeVar("T"))

    def test_main_cannot_be_generic(self):
        error = fail_on("(mod m)(fn main :T ((x :T)) (ret x))")
        self.assertEqual(error.code, "E0101")
        self.assertIn("main cannot have type variables", error.message)


class InstantiationTest(unittest.TestCase):
    def test_a_call_instantiates_with_the_argument_types(self):
        cm = checked(
            "(mod m)(fn id :T ((x :T)) (ret x))(fn f :i64 () (ret (id 1)))"
        )
        self.assertEqual(list(cm.instances), ["id$i64"])
        copy = cm.instances["id$i64"]
        self.assertEqual(copy.fn, "id")
        self.assertEqual(copy.params, (Prim("i64"),))
        self.assertEqual(copy.ret, Prim("i64"))
        self.assertEqual(cm.call_targets["m/f/2/0"], "id$i64")

    def test_two_call_sites_with_different_types_make_two_copies(self):
        cm = checked(
            '(mod m)(fn id :T ((x :T)) (ret x))'
            '(fn f :i64 () (let a (id 1)) (let b (id "s")) (ret a))'
        )
        self.assertEqual(list(cm.instances), ["id$i64", "id$str"])

    def test_the_same_call_twice_makes_one_copy(self):
        cm = checked(
            "(mod m)(fn id :T ((x :T)) (ret x))"
            "(fn f :i64 () (let a (id 1)) (let b (id 2)) (ret (+ a b)))"
        )
        self.assertEqual(list(cm.instances), ["id$i64"])

    def test_a_copy_spells_its_type_arguments_in_signature_order(self):
        # The return type is read first, then the parameters left to right, so
        # ``first`` is ``first$T$U`` and not ``first$U$T``.
        cm = checked(
            "(mod m)(fn first :T ((a :T) (b :U)) (ret a))"
            '(fn f :i64 () (let x (first 1 "one")) (ret x))'
        )
        self.assertEqual(list(cm.instances), ["first$i64$str"])

    def test_a_copy_records_its_own_types(self):
        cm = checked(
            "(mod m)(fn wrap :T ((x :T)) (let y x) (ret y))"
            '(fn f :i64 () (let a (wrap 1)) (let b (str-len (wrap "s"))) (ret (+ a b)))'
        )
        # One node path, two checks, two types: the copy carries its own
        # record, and the module's map never sees the generic body at all.
        self.assertEqual(
            cm.instances["wrap$str"].body.declared["m/wrap/2"], Prim("str")
        )
        self.assertEqual(
            cm.instances["wrap$i64"].body.declared["m/wrap/2"], Prim("i64")
        )
        self.assertNotIn("m/wrap/2", cm.declared)

    def test_a_generic_that_calls_a_generic_terminates(self):
        cm = checked(
            "(mod m)(fn id :T ((x :T)) (ret x))"
            "(fn twice :T ((x :T)) (ret (id (id x))))"
            "(fn f :i64 () (ret (twice 3)))"
        )
        self.assertEqual(list(cm.instances), ["twice$i64", "id$i64"])

    def test_a_recursive_generic_makes_one_copy(self):
        cm = checked(
            "(mod m)(fn down :T ((x :T)) (ret (down x)))"
            "(fn f :i64 () (ret (down 1)))"
        )
        self.assertEqual(list(cm.instances), ["down$i64"])

    def test_a_generic_body_is_checked_for_each_copy(self):
        # ``+`` is fine at ``:i64`` and refused at ``:str``; the copy is what
        # carries the body's types, so the second instantiation is the one
        # that reports.
        good = checked(
            "(mod m)(fn add :T ((a :T) (b :T)) (ret (+ a b)))"
            "(fn f :i64 () (ret (add 1 2)))"
        )
        self.assertEqual(good.instances["add$i64"].ret, Prim("i64"))
        error = fail_on(
            "(mod m)(fn add :T ((a :T) (b :T)) (ret (+ a b)))"
            '(fn f :str () (ret (add "a" "b")))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.path, "m/add/2/0")
        self.assertIn("needs numbers, found :str", error.message)

    def test_a_call_that_binds_no_variable_is_e0501(self):
        error = fail_on("(mod m)(fn empty :T () (ret 0))(fn f :i64 () (ret (empty)))")
        self.assertEqual(error.code, "E0501")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(
            error.message, "type variable T of 'empty' is not bound by this call"
        )

    def test_an_annotation_naming_an_unbound_variable_is_e0501(self):
        error = fail_on("(mod m)(fn f :i64 ((a :i64)) (let b T a) (ret b))")
        self.assertEqual(error.code, "E0501")
        self.assertEqual(error.path, "m/f/2")

    def test_a_generic_function_name_is_not_a_value(self):
        error = fail_on("(mod m)(fn id :T ((x :T)) (ret x))(fn f :i64 () (let g id) (ret 1))")
        self.assertEqual(error.code, "E0501")
        self.assertIn("id is generic", error.message)

    def test_a_mismatched_argument_names_the_bound_parameter(self):
        error = fail_on(
            '(mod m)(fn same :T ((a :T) (b :T)) (ret a))'
            '(fn f :i64 () (ret (same 1 "s")))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "argument 2 of 'same' expects :i64, found :str")

    def test_a_void_argument_binds_nothing(self):
        error = fail_on(
            '(mod m)(fn id :T ((x :T)) (ret x))'
            '(fn f :i64 () (let a (id (println "x"))) (ret 1))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("found :void", error.message)

    def test_a_vector_argument_is_still_moved(self):
        source = (
            "(mod m)"
            "(fn each :U ((v :(vec T)) (x :T) (f :(fn (:T) :U)))"
            " (vec-free v) (ret (f x)))"
            "(fn f :i64 ()"
            " (letmut v (make (vec :i64)))"
            " (letmut a (each v 0 (fn :i64 ((n :i64)) (ret n))))"
            " (letmut b (each v 0 (fn :i64 ((n :i64)) (ret n))))"
            " (ret a))"
        )
        error = fail_on(source)
        self.assertEqual(error.code, "E0402")
        self.assertIn("'v' moved", error.message)

    def test_a_generic_helper_of_a_test_cannot_be_instantiated(self):
        # A copy of a helper has nowhere to be emitted, so the call is refused
        # with the code the lowerer gives an unlowerable name.
        error = fail_on(
            "(mod m)(test t (fn h :T ((x :T)) (ret x)) (let a (h 1)))"
        )
        self.assertEqual(error.code, "E0103")
        self.assertIn("module functions", error.message)


class InstantiationLimitTest(unittest.TestCase):
    def test_256_copies_are_allowed(self):
        cm = checked(many_types(MAX_INSTANCES))
        self.assertEqual(len(cm.instances), MAX_INSTANCES)

    def test_the_257th_is_e0500(self):
        error = fail_on(many_types(MAX_INSTANCES + 1))
        self.assertEqual(error.code, "E0500")
        self.assertEqual(
            error.message, "more than 256 instantiations of generic functions"
        )
        self.assertEqual(error.path, "m/main/258")
        self.assertTrue(error.path, "the ceiling names the call that passed it")

    def test_the_limit_corpus_case_matches_its_expect_file(self):
        for case in sorted(GENERICS.glob("*.logos")):
            expected = case.with_suffix(".expect").read_text(encoding="utf-8").strip()
            with self.subTest(case=case.name):
                with self.assertRaises(LogosError) as caught:
                    check_module(driver.load(str(case)))
                error = caught.exception
                self.assertEqual(error.code, expected, error.render())
                self.assertTrue(error.path, "every generic error names a node")


class GenericLoweringTest(unittest.TestCase):
    def test_a_copy_is_emitted_under_its_mangled_symbol(self):
        text = emitted(
            "(mod m)(fn id :T ((x :T)) (ret x))(fn f :i64 () (ret (id 1)))"
        )
        self.assertIn("define internal i64 @lg.m.id$i64(i64", text)

    def test_a_call_targets_the_copy(self):
        module = lowered(
            "(mod m)(fn id :T ((x :T)) (ret x))(fn f :i64 () (ret (id 1)))"
        )
        self.assertIn("lg.m.id$i64", calls(fn_named(module, "lg.m.f")))

    def test_two_copies_are_two_definitions(self):
        module = lowered(
            '(mod m)(fn id :T ((x :T)) (ret x))'
            '(fn f :i64 () (let a (id 1)) (let b (str-len (id "s"))) (ret (+ a b)))'
        )
        symbols = {fn.symbol for fn in module.functions}
        self.assertIn("lg.m.id$i64", symbols)
        self.assertIn("lg.m.id$str", symbols)

    def test_an_uninstantiated_generic_is_not_emitted(self):
        module = lowered("(mod m)(fn id :T ((x :T)) (ret x))(fn f :i64 () (ret 1))")
        self.assertEqual([fn.symbol for fn in module.functions], ["lg.m.f"])

    def test_a_copy_of_a_named_type_is_quoted_in_the_ir(self):
        # ``s:`` marks a named type argument, and the colon is not part of an
        # LLVM identifier: the symbol is quoted rather than mangled away, so a
        # struct called ``i64`` cannot be mistaken for the primitive either.
        text = emitted(
            "(mod m)(type point (struct (x :i64)))"
            "(fn id :T ((x :T)) (ret x))"
            "(fn f :i64 () (let p (make point 1)) (let q (id p)) (ret (get q x)))"
        )
        self.assertIn('@"lg.m.id$s:point"', text)


class ClosureCheckTest(unittest.TestCase):
    def test_a_closure_expression_has_a_function_type(self):
        cm = checked(
            "(mod m)(fn f :i64 ((a :i64))"
            " (let g (fn :i64 ((x :i64)) (ret (+ x a)))) (ret (g 1)))"
        )
        self.assertEqual(
            cm.expr_types["m/f/2/1"], FnTy((Prim("i64"),), Prim("i64"))
        )

    def test_the_captures_are_the_outer_names_the_body_read(self):
        cm = checked(
            "(mod m)(fn f :i64 ((a :i64) (b :i64))"
            " (let g (fn :i64 ((x :i64)) (ret (+ x (+ a b))))) (ret (g 1)))"
        )
        closure = cm.closures["m/f/2/1"]
        self.assertEqual(closure.captures, (("a", Prim("i64")), ("b", Prim("i64"))))

    def test_a_capture_is_an_immutable_copy(self):
        error = fail_on(
            "(mod m)(fn f :i64 ((a :i64))"
            " (let g (fn :i64 () (set a 2) (ret a))) (ret (g)))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertIn("immutable binding 'a'", error.message)

    def test_a_binding_declared_after_the_closure_is_not_in_scope(self):
        error = fail_on(
            "(mod m)(fn f :i64 () (let g (fn :i64 () (ret k))) (let k 1) (ret (g)))"
        )
        self.assertEqual(error.code, "E0005")
        self.assertIn("unknown name 'k'", error.message)

    def test_a_vector_cannot_be_captured(self):
        error = fail_on(
            "(mod m)(fn f :i64 () (letmut v (make (vec :i64)))"
            " (let g (fn :i64 () (ret (vec-len v)))) (ret (g)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertIn("cannot capture 'v'", error.message)

    def test_an_owning_pointer_cannot_be_captured(self):
        error = fail_on(
            "(mod m)(fn f :i64 () (let p (alloc :i64 1))"
            " (let g (fn :i64 () (ret (deref p)))) (free p) (ret (g)))"
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot capture 'p'", error.message)

    def test_a_closure_body_cannot_break_an_outer_loop(self):
        error = fail_on(
            "(mod m)(fn f :void () (while true"
            " (let g (fn :i64 () (break) (ret 0))) (println 1)))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertIn("'break' outside a loop", error.message)

    def test_a_non_void_closure_must_return(self):
        error = fail_on("(mod m)(fn f :void () (let g (fn :i64 () (println 1))))")
        self.assertEqual(error.code, "E0105")
        self.assertIn("closure must return :i64", error.message)

    def test_nested_closures_capture_through_each_other(self):
        cm = checked(
            "(mod m)(fn f :i64 ((a :i64))"
            " (let g (fn :i64 ((x :i64))"
            "   (let h (fn :i64 ((y :i64)) (ret (+ y (+ x a)))))"
            "   (ret (h 1))))"
            " (ret (g 1)))"
        )
        inner = max(cm.closures.values(), key=lambda closure: len(closure.captures))
        self.assertEqual([name for name, _ in inner.captures], ["x", "a"])

    def test_a_closure_body_may_name_a_type_variable_of_its_generic(self):
        cm = checked(
            "(mod m)(fn id :T ((x :T)) (ret x))"
            "(fn apply :U ((f :(fn (:T) :U)) (x :T)) (ret (f x)))"
            "(fn f :i64 () (ret (apply (fn :i64 ((n :i64)) (ret (+ n 1))) 2)))"
        )
        self.assertIn("apply$i64$i64", cm.instances)


class ClosureLoweringTest(unittest.TestCase):
    def test_a_closure_becomes_a_function_of_its_own(self):
        text = emitted(
            "(mod m)(fn f :i64 ((k :i64))"
            " (let g (fn :i64 ((x :i64)) (ret (+ x k)))) (ret (g 1)))"
        )
        self.assertIn("define internal i64 @lg.m.$lambda$1(i8*", text)

    def test_the_environment_is_a_stack_copy_of_the_capture(self):
        module = lowered(
            "(mod m)(fn f :i64 ((k :i64))"
            " (let g (fn :i64 ((x :i64)) (ret (+ x k)))) (ret (g 1)))"
        )
        outer = fn_named(module, "lg.m.f")
        kinds = [i[0] for i in ops(outer)]
        self.assertIn("allocarec", kinds)
        self.assertIn("geprec", kinds)
        # The value stored into the environment is a load of the binding, which
        # is what makes the capture a copy rather than a reference.
        self.assertIn("store", kinds)

    def test_an_indirect_call_extracts_both_words_and_calls(self):
        module = lowered(
            "(mod m)(fn f :i64 ((k :i64))"
            " (let g (fn :i64 ((x :i64)) (ret (+ x k)))) (ret (g 1)))"
        )
        kinds = [i[0] for i in ops(fn_named(module, "lg.m.f"))]
        for op in ("extractvalue", "fnptr", "icall"):
            self.assertIn(op, kinds)

    def test_a_closure_inside_a_copy_uses_the_copy_s_types(self):
        # The closure is written once and checked per copy: its annotation
        # ``:T`` must read as the copy's concrete type, and its captures are
        # recorded in the copy's body, not the module's.
        cm = checked(
            "(mod m)(fn add-with :T ((x :T) (f :(fn (:T) :T)))"
            " (let g (fn :T ((y :T)) (ret (f (+ y x))))) (ret (g x)))"
            "(fn f :i64 ()"
            " (ret (add-with 3 (fn :i64 ((n :i64)) (ret (* n 2))))))"
        )
        closure = cm.instances["add-with$i64"].body.closures["m/add-with/2/1"]
        self.assertEqual(closure.params, (Prim("i64"),))
        self.assertEqual([name for name, _ in closure.captures], ["f", "x"])
        self.assertNotIn("m/add-with/2/1", cm.closures)
        self.assertIn("@lg.m.add-with$i64", llvm.emit(lower.lower_module(cm)))

    def test_a_named_function_value_gets_a_wrapper(self):
        module = lowered(
            "(mod m)(fn inc :i64 ((x :i64)) (ret (+ x 1)))"
            "(fn use :i64 ((f :(fn (:i64) :i64))) (ret (f 1)))"
            "(fn f :i64 () (ret (use inc)))"
        )
        symbols = {fn.symbol for fn in module.functions}
        self.assertIn("lg.m.$fnvalue$1", symbols)
        wrapper = fn_named(module, "lg.m.$fnvalue$1")
        self.assertEqual(len(wrapper.params), 2)
        self.assertIn("lg.m.inc", calls(wrapper))


class ClosuresExecTest(unittest.TestCase):
    """Real binaries: closures.logos at both optimisation levels."""

    CASES = {
        "capture-one": ("10\n", 0),
        "capture-is-a-copy": ("1\n", 0),
        "capture-two-bindings": ("30\n", 0),
        "no-captures": ("8\n", 0),
        "closure-argument": ("50\n", 0),
        "named-function-value": ("6\n", 0),
        "generic-calls-generic": ("20\n", 0),
        "two-type-parameters": ("1\nx\n", 0),
        "two-instantiations": ("42\n3\n", 0),
        "map-with-a-closure": ("3\n20\n", 0),
        "map-over-strings": ("2\n3\n2\n", 0),
        "closure-calling-a-closure": ("7\n", 0),
        "closure-inside-a-generic": ("12\n", 0),
    }

    def run_case(self, name: str, opt: str) -> subprocess.CompletedProcess:
        target = str(ROOT / "build" / f"gen-{name}-{opt[1:]}")
        driver.run_test(str(EXEC), name, target, opt=opt)
        return subprocess.run([target], capture_output=True, text=True)

    def test_every_case(self):
        for name, (stdout, code) in self.CASES.items():
            for opt in ("-O0", "-O2"):
                with self.subTest(case=name, opt=opt):
                    done = self.run_case(name, opt)
                    self.assertEqual(done.stdout, stdout, done.stderr)
                    self.assertEqual(done.returncode, code, done.stderr)
                    # A debug build reports every block still live: a closure
                    # environment is a stack slot, so it must never appear.
                    self.assertNotIn("LEAK", done.stderr)


if __name__ == "__main__":
    unittest.main()
