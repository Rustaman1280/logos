"""Strings, vectors and formatting: the Task 9 surface, in three layers.

The checker's half (which spellings exist and what they return), the
lowerer's half (exactly what a ``str-eq``, a slice, a push and a print emit),
and real binaries at ``-O0`` and ``-O2``.

The exec recipes in ``tests/exec`` carry the same expectations in their
``(expect ...)`` directives; that harness is Task 13's, so the strings are
restated here rather than parsed.
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
from logos.types import Arr, FnTy, Prim, TypeVar, Vec

ROOT = Path(__file__).resolve().parents[1]
EXEC = ROOT / "tests" / "exec"
STRINGS = EXEC / "strings.logos"
VEC = EXEC / "vec.logos"

LONG = "0123456789" * 30


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


def type_of(cm: CheckedModule, kind: str):
    return cm.expr_types[path_of(cm, kind)]


def fail_on(source: str) -> LogosError:
    """The checker diagnostic ``source`` raises; fails if it checks."""
    try:
        checked(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this checked: {source}")


def lowered(source: str, *, debug: bool = False) -> ir.IrModule:
    return lower.lower_module(checked(source), debug=debug)


def fail_on_lower(source: str) -> LogosError:
    """The lowerer diagnostic ``source`` raises; fails if it lowers."""
    try:
        lowered(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this lowered: {source}")


def emitted(source: str) -> str:
    return llvm.emit(lowered(source))


def calls(fn: ir.IrFn) -> list[tuple]:
    return [i for b in fn.blocks for i in b.instrs if i[0] == "call"]


def ops(fn: ir.IrFn) -> list[str]:
    return [i[0] for b in fn.blocks for i in b.instrs]


def callees(fn: ir.IrFn) -> list[str]:
    return [i[2] for i in calls(fn)]


class StringCheckTest(unittest.TestCase):
    """``str-len``, ``str-concat``, ``str-eq`` and ``str-slice``."""

    def test_str_len_is_an_i64(self):
        cm = checked('(mod m)(fn f :i64 ((s :str)) (ret (str-len s)))')
        self.assertEqual(type_of(cm, "str-len"), Prim("i64"))

    def test_str_concat_is_a_str(self):
        cm = checked('(mod m)(fn f :str ((a :str) (b :str)) (ret (str-concat a b)))')
        self.assertEqual(type_of(cm, "str-concat"), Prim("str"))

    def test_str_eq_is_a_bool(self):
        cm = checked('(mod m)(fn f :bool ((a :str) (b :str)) (ret (str-eq a b)))')
        self.assertEqual(type_of(cm, "str-eq"), Prim("bool"))

    def test_str_slice_is_a_str(self):
        cm = checked('(mod m)(fn f :str ((s :str)) (ret (str-slice s 0 1)))')
        self.assertEqual(type_of(cm, "str-slice"), Prim("str"))

    def test_str_len_refuses_something_that_is_not_a_str(self):
        error = fail_on("(mod m)(fn f :i64 () (ret (str-len 5)))")
        self.assertEqual(error.code, "E0401")
        # The operand is a token, which carries no path: the form owns it.
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "'str-len' takes a :str, found :i64")

    def test_str_concat_checks_its_second_operand(self):
        error = fail_on('(mod m)(fn f :str ((a :str)) (ret (str-concat a 2)))')
        self.assertEqual(error.code, "E0401")
        self.assertEqual(error.message, "'str-concat' takes a :str, found :i64")

    def test_str_eq_checks_both_operands(self):
        error = fail_on('(mod m)(fn f :bool ((a :str)) (ret (str-eq a true)))')
        self.assertEqual(error.code, "E0401")
        self.assertEqual(error.message, "'str-eq' takes a :str, found :bool")

    def test_a_slice_index_must_be_an_integer(self):
        error = fail_on(
            '(mod m)(fn f :str ((s :str)) (ret (str-slice s 0 "x")))'
        )
        self.assertEqual(error.code, "E0400")
        self.assertEqual(error.message, "slice index must be an integer, found :str")

    def test_the_arity_of_each_string_form_is_checked(self):
        for source, message in (
            ("(mod m)(fn f :i64 ((s :str)) (ret (str-len s s)))",
             "'str-len' takes a :str"),
            ("(mod m)(fn f :str ((s :str)) (ret (str-slice s 0)))",
             "'str-slice' takes a :str, a start and an end"),
        ):
            with self.subTest(source=source):
                error = fail_on(source)
                self.assertEqual(error.code, "E0102")
                self.assertIn(message, error.message)

    def test_str_concat_and_str_eq_check_their_arity(self):
        for source, message in (
            ('(mod m)(fn f :str ((s :str)) (ret (str-concat s)))',
             "'str-concat' takes two strings"),
            ('(mod m)(fn f :bool ((s :str)) (ret (str-eq s)))',
             "'str-eq' takes two strings"),
        ):
            with self.subTest(source=source):
                error = fail_on(source)
                self.assertEqual(error.code, "E0102")
                self.assertEqual(error.message, message)

    def test_equality_on_str_is_accepted_and_yields_a_bool(self):
        cm = checked('(mod m)(fn f :bool ((a :str)) (ret (= a "x")))')
        self.assertEqual(type_of(cm, "="), Prim("bool"))

    def test_inequality_on_str_is_accepted(self):
        cm = checked('(mod m)(fn f :bool ((a :str)) (ret (!= a "x")))')
        self.assertEqual(type_of(cm, "!="), Prim("bool"))

    def test_str_is_still_not_ordered(self):
        error = fail_on('(mod m)(fn f :bool ((a :str)) (ret (< a "x")))')
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'<' needs numbers, found :str")


class VecCheckTest(unittest.TestCase):
    """``(vec T)`` and the five operations over it."""

    def test_make_builds_an_empty_vector(self):
        cm = checked(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64))))"
        )
        self.assertEqual(type_of(cm, "make"), Vec(Prim("i64")))

    def test_make_refuses_elements(self):
        error = fail_on(
            "(mod m)(fn f :void () (let v (vec :i64) (make (vec :i64) 1)))"
        )
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "a vector starts empty: make takes no elements")

    def test_vec_len_is_an_i64(self):
        cm = checked(
            "(mod m)(fn f :i64 ((v (vec :i64))) (ret (vec-len v)))"
        )
        self.assertEqual(type_of(cm, "vec-len"), Prim("i64"))

    def test_vec_get_is_the_element_type(self):
        cm = checked(
            "(mod m)(fn f :str ((v (vec :str))) (ret (vec-get v 0)))"
        )
        self.assertEqual(type_of(cm, "vec-get"), Prim("str"))

    def test_vec_len_refuses_a_non_vector(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (vec-len n)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'vec-len' needs a (vec T), found :i64")

    def test_a_vector_index_must_be_an_integer(self):
        error = fail_on(
            '(mod m)(fn f :i64 ((v (vec :i64))) (ret (vec-get v "x")))'
        )
        self.assertEqual(error.code, "E0400")
        self.assertEqual(error.message, "vector index must be an integer, found :str")

    def test_push_still_needs_letmut_on_an_ordinary_binding(self):
        # A parameter is a place for push/set/free (R4), but a plain ``let``
        # still promises to hold its value, so pushing through it is E0104.
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (let v (vec :i64) (make (vec :i64))) (vec-push v 1))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertEqual(error.message, "cannot push to immutable binding 'v'")

    def test_a_vector_parameter_can_be_pushed_to(self):
        # The caller moved the vector in, so the callee owns the buffer and
        # must be able to grow it — otherwise there is no way to fill a vector
        # on behalf of a caller.
        checked("(mod m)(fn f :void ((v (vec :i64))) (vec-push v 1))")

    def test_a_vector_parameter_can_be_set(self):
        checked("(mod m)(fn f :void ((v (vec :i64))) (vec-set v 0 1))")

    def test_a_vector_parameter_can_be_freed(self):
        # The callee owns what it was handed (R1), so it is the one that has
        # to be able to release the buffer.
        checked("(mod m)(fn f :void ((v (vec :i64))) (vec-free v))")

    def test_set_still_refuses_a_vector_parameter(self):
        # R4 names push, set and free as the three vector forms; ``set`` is
        # not one that may target a parameter.
        error = fail_on(
            "(mod m)(fn f :void ((v (vec :i64)))"
            " (set v (make (vec :i64))))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertEqual(error.message, "cannot set immutable binding 'v'")

    def test_push_checks_the_element(self):
        error = fail_on(
            '(mod m)(fn f :void ()'
            ' (letmut v (vec :i64) (make (vec :i64))) (vec-push v "s"))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot push :str into (vec :i64)", error.message)

    def test_push_needs_a_vector(self):
        error = fail_on(
            "(mod m)(fn f :void () (letmut n :i64 0) (vec-push n 1))"
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'vec-push' needs a (vec T), found :i64")

    def test_set_checks_the_index_and_the_value(self):
        error = fail_on(
            '(mod m)(fn f :void ()'
            ' (letmut v (vec :i64) (make (vec :i64))) (vec-set v 0 "s"))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot store :str in (vec :i64)", error.message)

    def test_each_vector_form_checks_its_arity_before_its_target(self):
        for source, message in (
            ("(mod m)(fn f :void ((v (vec :i64))) (vec-free))",
             "vec-free takes a vector name"),
            ("(mod m)(fn f :void ((v (vec :i64))) (vec-push v))",
             "vec-push takes a vector name and a value"),
            ("(mod m)(fn f :void ((v (vec :i64))) (vec-get v))",
             "'vec-get' takes a vector and an index"),
            ("(mod m)(fn f :void ((v (vec :i64))) (vec-set v 0))",
             "vec-set takes a vector name, an index and a value"),
        ):
            with self.subTest(source=source):
                error = fail_on(source)
                self.assertEqual(error.code, "E0102")
                self.assertEqual(error.message, message)

    def test_free_still_needs_letmut_on_an_ordinary_binding(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (let v (vec :i64) (make (vec :i64))) (vec-free v))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertEqual(error.message, "cannot free immutable binding 'v'")

    def test_a_vector_of_a_type_variable_is_a_generic_signature(self):
        # A type variable is what makes a definition generic: ``(vec T)`` is a
        # vector of whatever a call site fixes, and nothing inside the body is
        # checked until one does.
        checked_ = checked("(mod m)(fn f :void ((v (vec T))) (ret))")
        self.assertEqual(
            checked_.fn_sigs["f"], FnTy((Vec(TypeVar("T")),), Prim("void"))
        )
        self.assertEqual(checked_.generic, frozenset({"f"}))

    def test_a_vector_of_void_is_refused(self):
        error = fail_on_lower("(mod m)(fn f :void ((v (vec :void))) (ret))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, ":void is not lowered yet")

    def test_an_array_index_is_an_e0400_too(self):
        error = fail_on(
            "(mod m)(fn f :i64 ()"
            ' (let a (arr :i64 2) (make (arr :i64 2) 1 2)) (ret (arr-get a "x")))'
        )
        self.assertEqual(error.code, "E0400")
        self.assertEqual(error.message, "array index must be an integer, found :str")

    def test_a_vector_in_a_struct_field_is_refused_at_the_field(self):
        # A vector is released through its descriptor and a field is not a
        # place this language can name, so the field could never be freed.
        error = fail_on(
            "(mod m)(type b (struct (items (vec :i64))))(fn f :void ())"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "vector fields are not supported yet")
        self.assertEqual(error.path, "m/b/0/0")

    def test_a_struct_holding_a_vector_of_itself_is_the_same_refusal(self):
        # ``(vec node)`` is a pointer and two counts, so the type is finite;
        # the reason it is refused is the field, not self-containment, and the
        # message has to say so.
        error = fail_on(
            "(mod m)(type node (struct (kids (vec node))))(fn f :void ())"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "vector fields are not supported yet")
        self.assertEqual(error.path, "m/node/0/0")

    def test_a_vector_array_element_is_refused_where_the_array_is_spelled(self):
        error = fail_on(
            "(mod m)(fn f :void ((a (arr (vec :i64) 2))) (ret))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message, "vector array elements are not supported yet"
        )


class VectorMoveTest(unittest.TestCase):
    """A ``(vec T)`` is owned, so a second name for one is a move.

    Two bindings must never share a buffer: whichever released it first would
    leave the other reading freed memory, and the second release would be a
    double free. The source is empty after the move (E0300 on a read), and the
    value cannot be handed over twice (E0402).
    """

    def test_binding_a_vector_moves_the_source(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (println (vec-len v)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertIn("use after move of 'v'", error.message)

    def test_the_new_binding_is_the_one_that_holds_the_buffer(self):
        checked(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (vec-free w))"
        )

    def test_an_immutable_binding_moves_too(self):
        # A plain let cannot be moved out of (E0302) unless the value contains
        # a vector: there is no copy to make, and refusing would leave the
        # buffer with no owner at all.
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (let v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (letmut x (vec :i64) v))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(
            error.message,
            "a value containing a vector is not copyable; 'v' moved",
        )

    def test_freeing_a_moved_vector_is_e0300(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (vec-free v))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'v'")

    def test_passing_the_same_vector_twice_is_e0402(self):
        error = fail_on(
            "(mod m)(fn take :i64 ((a (vec :i64)) (b (vec :i64)))"
            " (ret (+ (vec-len a) (vec-len b))))"
            "(fn f :i64 ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (ret (take v v)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(
            error.message,
            "a value containing a vector is not copyable; 'v' moved",
        )

    def test_a_moved_vector_can_be_freed_once_in_each_branch(self):
        # The branches are independent: each releases the one buffer, and only
        # one of them runs, so this is not a double free.
        checked(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (if (= (vec-len w) 1) (vec-free w) (vec-free w)))"
        )

    def test_a_plain_read_of_a_moved_vector_is_still_e0300(self):
        # E0402 is for a second transfer; reading the empty binding is the
        # ordinary use-after-move.
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) v)"
            " (vec-push v 1))"
        )
        self.assertEqual(error.code, "E0300")


class VectorOwnershipTest(unittest.TestCase):
    """R4 and R5: the callee may release, and a pushed vector moves.

    A vector argument is a transfer, so after the call the caller's binding is
    empty (E0300). That leaves exactly one owner, which is why the callee has
    to be able to release the buffer: a parameter is a place for push, set and
    free. Storing a descriptor in an element moves the argument too — an
    element is not a place, so it can never be released, but two live
    descriptors for one buffer must not exist.
    """

    def test_the_callee_can_free_the_vector_it_was_given(self):
        checked(
            "(mod m)(fn release :void ((v (vec :i64)))"
            " (vec-push v 1) (vec-free v))"
            "(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (release v))"
        )

    def test_the_caller_cannot_read_a_vector_it_passed(self):
        error = fail_on(
            "(mod m)(fn release :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (release v)"
            " (println (vec-len v)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'v'")

    def test_the_caller_cannot_free_a_vector_it_passed(self):
        # The callee owns it and may already have released it: a second free
        # at the call site would be a double free.
        error = fail_on(
            "(mod m)(fn release :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void ()"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (release v)"
            " (vec-free v))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'v'")

    def test_pushing_a_vector_moves_the_source(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut inner (vec :i64) (make (vec :i64)))"
            " (letmut outer (vec (vec :i64)) (make (vec (vec :i64))))"
            " (vec-push outer inner)"
            " (println (vec-len inner)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'inner'")

    def test_a_pushed_vector_cannot_be_freed_afterwards(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut inner (vec :i64) (make (vec :i64)))"
            " (letmut outer (vec (vec :i64)) (make (vec (vec :i64))))"
            " (vec-push outer inner)"
            " (vec-free inner))"
        )
        self.assertEqual(error.code, "E0300")

    def test_the_container_stays_usable_after_the_push(self):
        # Only the pushed value moved; the container is still the one owner of
        # its own buffer.
        checked(
            "(mod m)(fn f :void ()"
            " (letmut inner (vec :i64) (make (vec :i64)))"
            " (letmut outer (vec (vec :i64)) (make (vec (vec :i64))))"
            " (vec-push outer inner)"
            " (println (vec-len outer))"
            " (vec-free outer))"
        )

    def test_setting_a_vector_element_moves_the_source_too(self):
        # ``vec-set`` stores the same descriptor a push would, so it is the
        # same move; a copy here would be the aliasing R5 removes.
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (letmut inner (vec :i64) (make (vec :i64)))"
            " (letmut outer (vec (vec :i64)) (make (vec (vec :i64))))"
            " (vec-push outer inner)"
            " (letmut second (vec :i64) (make (vec :i64)))"
            " (vec-set outer 0 second)"
            " (println (vec-len second)))"
        )
        self.assertEqual(error.code, "E0300")
        self.assertEqual(error.message, "use after move of 'second'")

    def test_a_vector_element_cannot_be_passed_as_a_vector(self):
        # R6: the callee may free what it is handed, so a descriptor copied
        # out of the caller's container would leave it pointing at released
        # memory. The argument has to be a name.
        error = fail_on(
            "(mod m)(fn drain :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void ()"
            " (letmut outer (vec (vec :i64)) (make (vec (vec :i64))))"
            " (drain (vec-get outer 0)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(error.message, "a vector argument must be a name")

    def test_a_call_result_cannot_be_passed_as_a_vector(self):
        error = fail_on(
            "(mod m)(fn make-one :(vec :i64) () (ret (make (vec :i64))))"
            "(fn drain :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void () (drain (make-one)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(error.message, "a vector argument must be a name")

    def test_an_array_element_cannot_be_passed_as_a_vector(self):
        # A vector may not be an array element (E0002, above), so an
        # ``arr-get`` cannot produce one; the form is still refused as a
        # vector argument before its element type is considered, which is
        # what keeps the rule syntactic and unreachable-by-accident.
        error = fail_on(
            "(mod m)(fn drain :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void ((a (arr :i64 2))) (drain (arr-get a 0)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(error.message, "a vector argument must be a name")

    def test_a_vector_cannot_be_selected_by_if(self):
        # R7: both arms still own the buffer, so the result would be a second
        # descriptor for it and freeing it would double-free.
        error = fail_on(
            "(mod m)(fn f :void ((c :bool))"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) (if c v v)))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(
            error.message, "a vector cannot be selected by if or match"
        )

    def test_a_vector_cannot_be_selected_by_match(self):
        error = fail_on(
            "(mod m)(fn f :void ((n :i64))"
            " (letmut v (vec :i64) (make (vec :i64)))"
            " (letmut w (vec :i64) (match n (0 v) (_ v))))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertEqual(
            error.message, "a vector cannot be selected by if or match"
        )


class PrinterCheckTest(unittest.TestCase):
    """Every scalar the runtime formats, and nothing that needs a cast."""

    def test_every_printable_scalar_is_accepted(self):
        for ty, value in (
            (":i64", "1"),
            (":u64", "1"),
            (":f64", "1.5"),
            (":bool", "true"),
            (":str", '"x"'),
        ):
            with self.subTest(ty=ty):
                cm = checked(f"(mod m)(fn f :void () (println {value}))")
                self.assertEqual(type_of(cm, "println"), Prim("void"))

    def test_a_narrow_integer_needs_a_cast_of_its_own(self):
        error = fail_on(
            '(mod m)(fn f :void ((n :i32)) (println n))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(
            error.message,
            "println takes one of :i64, :u64, :f64, :bool or :str, found :i32",
        )

    def test_f32_is_not_printed_by_the_f64_formatter(self):
        error = fail_on('(mod m)(fn f :void ((n :f32)) (print n))')
        self.assertEqual(error.code, "E0100")

    def test_a_struct_is_not_printable(self):
        error = fail_on(
            "(mod m)(type p (struct (x :i64)))"
            "(fn f :void ((v p)) (println v))"
        )
        self.assertEqual(error.code, "E0100")

    def test_a_printer_takes_exactly_one_argument(self):
        for source in (
            "(mod m)(fn f :void () (println))",
            '(mod m)(fn f :void () (println "a" "b"))',
        ):
            with self.subTest(source=source):
                error = fail_on(source)
                self.assertEqual(error.code, "E0102")

    def test_a_modules_own_fn_still_shadows_the_printer(self):
        cm = checked(
            '(mod m)(fn print :void ((n :i32)) (ret))'
            "(fn f :void ((n :i32)) (print n))"
        )
        self.assertEqual(type_of(cm, "print"), Prim("void"))

    def test_printing_a_float_literal_does_not_widen_an_integer(self):
        # No implicit conversion: a float literal in the print position is an
        # f64, and an integer literal is an i64.
        self.assertEqual(
            type_of(checked("(mod m)(fn f :void () (println 1))"), "println"),
            Prim("void"),
        )
        cm = checked("(mod m)(fn f :void () (println 1.5))")
        self.assertEqual(cm.expr_types[path_of(cm, "println")], Prim("void"))


class StringLoweringTest(unittest.TestCase):
    """What one string operation costs in the IR."""

    def test_str_eq_compares_lengths_then_bytes(self):
        m = lowered('(mod m)(fn f :bool ((a :str) (b :str)) (ret (str-eq a b)))')
        fn = m.functions[0]
        text = llvm.emit(m)
        # The length test comes first and the byte comparison only runs when
        # the two lengths agree.
        self.assertIn("icmp eq i64", text)
        self.assertIn("call i32 @memcmp(i8*", text)
        self.assertIn("declare i32 @memcmp(i8*, i8*, i64)", text)
        self.assertEqual(callees(fn).count("memcmp"), 1)
        preds = [i[2] for i in fn.blocks[0].instrs if i[0] == "icmp"]
        self.assertIn("eq", preds)

    def test_the_equality_operator_on_str_uses_the_same_lowering(self):
        text = emitted('(mod m)(fn f :bool ((a :str) (b :str)) (ret (= a b)))')
        self.assertIn("call i32 @memcmp(i8*", text)

    def test_inequality_inverts_the_equality_result(self):
        text = emitted('(mod m)(fn f :bool ((a :str) (b :str)) (ret (!= a b)))')
        self.assertIn("call i32 @memcmp(i8*", text)
        # ``!=`` is ``=`` with the answer flipped, never a second comparison.
        self.assertIn("icmp eq i1", text)

    def test_str_len_extracts_the_length_field(self):
        text = emitted('(mod m)(fn f :i64 ((s :str)) (ret (str-len s)))')
        self.assertIn("extractvalue { i8*, i64 } %t", text)

    def test_str_concat_calls_the_runtime_with_an_out_length(self):
        m = lowered('(mod m)(fn f :str ((a :str) (b :str)) (ret (str-concat a b)))')
        fn = m.functions[0]
        self.assertIn("rt_str_concat", callees(fn))
        text = llvm.emit(m)
        self.assertIn(
            "declare i8* @rt_str_concat(i8*, i64, i8*, i64, i64*)", text
        )
        # The length the runtime wrote back is what the new :str carries.
        self.assertIn("load i64, i64* %v.", text)

    def test_the_concat_overflow_guard_is_a_signed_compare(self):
        # Two lengths are in [0, 2^63), so a sum that wrapped landed in
        # [2^63, 2^64) and reads back negative. An unsigned compare against
        # the left length — ``icmp ult total, left`` — is false in both the
        # wrapped and the unwrapped case, so the panic could never run.
        text = emitted(
            '(mod m)(fn f :str ((a :str) (b :str)) (ret (str-concat a b)))'
        )
        self.assertIn("icmp slt i64", text)
        self.assertIn(", 0", text)
        self.assertNotIn("icmp ult i64", text)
        self.assertIn("string length overflows", text)

    def test_the_overflow_guard_survives_the_optimiser(self):
        """The panic block is still in the IR clang produces at ``-O2``.

        A guard the optimiser can prove false takes its panic block, its
        code and its message with it, so the check runs on the optimised IR,
        not on ours. The function is exported for the test: an unused
        ``internal`` one is deleted whole at ``-O2``, which would hide the
        guard rather than pin it. The compare it keeps is asserted signed,
        because that is the part a rewrite to ``ult`` would change.
        """
        m = lowered(
            '(mod m)(fn f :str ((a :str) (b :str)) (ret (str-concat a b)))'
        )
        for fn in m.functions:
            fn.internal = False
        path = ROOT / "build" / "concat-overflow.ll"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(llvm.emit(m), encoding="utf-8")
        try:
            done = subprocess.run(
                [
                    "clang",
                    "-O2",
                    "-S",
                    "-emit-llvm",
                    "-target",
                    ir.TRIPLE,
                    str(path),
                    "-o",
                    "-",
                ],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            self.skipTest("clang is not available")
        if done.returncode != 0:
            self.skipTest(f"clang refused the IR: {done.stderr.strip()}")
        # ``-S`` prints the module it optimised, warnings and all.
        text = done.stdout
        self.assertIn("call void @rt_panic", text)
        self.assertIn("string length overflows", text)
        self.assertIn("icmp slt i64", text)

    def test_a_slice_is_a_view_and_allocates_nothing(self):
        m = lowered('(mod m)(fn f :str ((s :str)) (ret (str-slice s 1 4)))')
        fn = m.functions[0]
        self.assertNotIn("rt_alloc", callees(fn))
        self.assertNotIn("rt_str_concat", callees(fn))
        text = llvm.emit(m)
        self.assertIn("getelementptr i8, i8* %t", text)
        self.assertIn("sub i64", text)

    def test_a_slice_checks_both_ends_and_the_order(self):
        m = lowered('(mod m)(fn f :str ((s :str)) (ret (str-slice s 1 4)))')
        fn = m.functions[0]
        self.assertIn("rt_panic", callees(fn))
        self.assertTrue(any(g.data == b"R0003\x00" for g in m.globals))
        self.assertTrue(
            any(b"string slice out of bounds\x00" in g.data for g in m.globals)
        )
        self.assertTrue(any(("unreachable",) == b.term for b in fn.blocks))


class VecLoweringTest(unittest.TestCase):
    """A vector is three words, and growth is alloc/copy/free."""

    def test_a_vector_has_three_words_of_layout(self):
        self.assertEqual(llvm.type_text(Vec(Prim("i64"))), "{ i64*, i64, i64 }")
        self.assertEqual(lower.sizeof_of(Vec(Prim("i64")), {}), 24)
        self.assertEqual(lower.align_of(Vec(Prim("i64")), {}), 8)

    def test_make_builds_a_null_descriptor(self):
        text = emitted(
            "(mod m)(fn f :void () (letmut v (vec :i64) (make (vec :i64))))"
        )
        self.assertIn("insertvalue { i64*, i64, i64 } undef, i64* null, 0", text)
        self.assertIn(", i64 0, 1", text)
        self.assertIn("store { i64*, i64, i64 } %t", text)

    def test_push_allocates_copies_and_releases(self):
        m = lowered(
            "(mod m)(fn f :void () (letmut v (vec :i64) (make (vec :i64)))"
            " (vec-push v 1))"
        )
        fn = m.functions[0]
        names = callees(fn)
        self.assertIn("rt_alloc", names)
        self.assertIn("memcpy", names)
        self.assertIn("rt_free", names)
        # The path handed to the allocator is the push's own node path, so a
        # leak would name it.
        self.assertTrue(any(b"m/f/3\x00" == g.data for g in m.globals))

    def test_push_stores_the_new_descriptor_back_into_the_binding(self):
        text = emitted(
            "(mod m)(fn f :void () (letmut v (vec :i64) (make (vec :i64)))"
            " (vec-push v 7))"
        )
        stores = [
            line for line in text.splitlines() if "store { i64*, i64, i64 }" in line
        ]
        # One store from ``make`` and one from the push that grew.
        self.assertEqual(len(stores), 2)
        self.assertIn("store i64 7, i64*", text)

    def test_a_second_push_inside_the_capacity_does_not_allocate(self):
        # The growth branch is guarded by ``len == cap``; the second push of a
        # capacity-four vector runs no allocator.
        m = lowered(
            "(mod m)(fn f :void () (letmut v (vec :i64) (make (vec :i64)))"
            " (vec-push v 1) (vec-push v 2))"
        )
        self.assertEqual(callees(m.functions[0]).count("rt_alloc"), 2)

    def test_get_bounds_checks_before_the_load(self):
        m = lowered(
            "(mod m)(fn f :i64 ((v (vec :i64))) (ret (vec-get v 0)))"
        )
        fn = m.functions[0]
        self.assertIn("rt_panic", callees(fn))
        self.assertTrue(
            any(b"vector index out of bounds\x00" in g.data for g in m.globals)
        )

    def test_free_releases_the_buffer_and_zeroes_the_descriptor(self):
        text = emitted(
            "(mod m)(fn f :void () (letmut v (vec :i64) (make (vec :i64)))"
            " (vec-push v 1) (vec-free v))"
        )
        self.assertIn("bitcast i64* %t", text)
        self.assertIn("call void @rt_free(", text)

    def test_a_parameter_is_a_place_for_the_mutating_forms(self):
        # A parameter has its own alloca and the callee owns the value, so the
        # descriptor is written back into it and the buffer is released there.
        m = lowered(
            "(mod m)(fn f :void ((v (vec :i64))) (vec-push v 1) (vec-free v))"
        )
        fn = m.functions[0]
        self.assertIn("rt_free", callees(fn))
        text = llvm.emit(m)
        self.assertIn("alloca { i64*, i64, i64 }", text)
        self.assertIn("store { i64*, i64, i64 } zeroinitializer", text)

    def test_a_vector_of_a_struct_collects_the_struct(self):
        text = emitted(
            "(mod m)(type p (struct (x :i64)))"
            "(fn f :void () (letmut v (vec p) (make (vec p))))"
        )
        self.assertIn("%struct.p = type { i64 }", text)
        self.assertIn("{ %struct.p*, i64, i64 }", text)

    def test_a_vector_of_a_bad_shared_type_is_e0002(self):
        # The shared marker wraps a pointer or a synchronization handle; a
        # scalar behind it is refused where the vector's element is spelled,
        # with a path, rather than reaching the backend.
        error = fail_on_lower(
            "(mod m)(fn f :void ((v (vec (shared :i64)))) (ret))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message,
            "only a pointer, a mutex, a channel or an atomic can be shared",
        )


class FormattingLoweringTest(unittest.TestCase):
    """Each printable type reaches its own runtime entry point."""

    def test_an_i64_goes_through_rt_print_i64(self):
        m = lowered("(mod m)(fn f :void () (println 41))")
        self.assertIn("rt_print_i64", callees(m.functions[0]))
        self.assertIn("rt_println", callees(m.functions[0]))
        printed = [i for i in calls(m.functions[0]) if i[2] == "rt_print_i64"]
        self.assertEqual(printed[0][3], ("41",))

    def test_a_u64_goes_through_rt_print_u64(self):
        m = lowered("(mod m)(fn f :void ((n :u64)) (println n))")
        # The unsigned entry point exists because the signed one would print
        # the top half of the range as negative.
        self.assertIn("rt_print_u64", callees(m.functions[0]))

    def test_an_f64_goes_through_rt_print_f64(self):
        m = lowered("(mod m)(fn f :void () (println 1.5))")
        self.assertIn("rt_print_f64", callees(m.functions[0]))

    def test_a_bool_prints_true_or_false(self):
        m = lowered("(mod m)(fn f :void ((b :bool)) (println b))")
        fn = m.functions[0]
        self.assertEqual(callees(fn).count("rt_print_str"), 2)
        self.assertTrue(any(g.data == b"true" for g in m.globals))
        self.assertTrue(any(g.data == b"false" for g in m.globals))
        self.assertIn("rt_println", callees(fn))

    def test_a_str_goes_through_rt_print_str_with_its_own_length(self):
        m = lowered('(mod m)(fn f :void ((s :str)) (println s))')
        fn = m.functions[0]
        printed = [i for i in calls(fn) if i[2] == "rt_print_str"]
        self.assertEqual(len(printed), 1)
        self.assertEqual(len(printed[0][3]), 2)

    def test_print_does_not_write_a_newline(self):
        m = lowered('(mod m)(fn f :void () (print "x"))')
        self.assertNotIn("rt_println", callees(m.functions[0]))

    def test_a_float_literal_is_spelled_the_way_llvm_reads_it(self):
        text = emitted("(mod m)(fn f :void () (println 2.0))")
        self.assertIn("double 2.0", text)
        text = emitted("(mod m)(fn f :void () (println 1e300))")
        self.assertIn("double 1.0e+300", text)
        self.assertNotIn("1e+300", text.replace("1.0e+300", ""))

    def test_float_arithmetic_is_still_refused(self):
        # Formatting is in scope; float arithmetic is not, and emitting
        # ``add double`` would be invalid IR rather than a diagnostic.
        error = fail_on_lower("(mod m)(fn f :void () (println (+ 1.5 2.5)))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message, "'+' on :f64 is not lowered by this backend yet"
        )


class StringsExecTest(unittest.TestCase):
    """Real binaries: strings.logos at both optimisation levels."""

    CASES = {
        "multiline": ("line one\nline two\n", 0),
        "byte-count": (LONG + "\n300\n", 0),
        "multibyte-length": ("5\n", 0),
        "print-without-newline": ("ab", 0),
        "concat": ("abcd\nempty:\n", 0),
        "concat-grows": ("100\n" + "x" * 100 + "\n", 0),
        "slice": ("bcd\ndef\n\n", 0),
        "slice-of-a-computed-string": ("bcd\n", 0),
        "equality": ("true\nfalse\ntrue\nfalse\ntrue\ntrue\n", 0),
        "equality-through-names": ("true\nfalse\n", 0),
        "slice-out-of-bounds": ("", 2),
        "nul-byte": ("a\x00b\n3\n", 0),
        "nul-is-not-a-terminator": ("true\nfalse\ntrue\n", 0),
        "slice-reversed": ("", 2),
        "slice-negative": ("", 2),
        "formats-signed": ("0\n-1\n9223372036854775807\n-9223372036854775808\n", 0),
        "formats-unsigned": ("18446744073709551615\n0\n", 0),
        "formats-floats": ("3.5\n0.5\n2\n-1.25\n0.1\n1e+300\n", 0),
        "formats-bools": ("true\nfalse\ntrue", 0),
        "formats-a-binding": ("41\n", 0),
    }

    def run_case(
        self, source: Path, name: str, opt: str
    ) -> subprocess.CompletedProcess:
        target = str(ROOT / "build" / f"txt-{source.stem}-{name}-{opt[1:]}")
        driver.run_test(str(source), name, target, opt=opt)
        return subprocess.run([target], capture_output=True, text=True)

    def test_every_case(self):
        for name, (stdout, code) in self.CASES.items():
            for opt in ("-O0", "-O2"):
                with self.subTest(case=name, opt=opt):
                    done = self.run_case(STRINGS, name, opt)
                    self.assertEqual(done.stdout, stdout, done.stderr)
                    self.assertEqual(done.returncode, code, done.stderr)
                    self.assertNotIn("LEAK", done.stderr)

    def test_a_slice_out_of_bounds_names_its_node(self):
        done = self.run_case(STRINGS, "slice-out-of-bounds", "-O0")
        self.assertEqual(done.returncode, 2)
        self.assertIn("R0003: string slice out of bounds", done.stderr)
        self.assertTrue(done.stderr.startswith("strings/test/slice-out-of-bounds/"))


class VecExecTest(unittest.TestCase):
    """Real binaries: vec.logos at both optimisation levels."""

    CASES = {
        "empty": ("0\n", 0),
        "push-and-get": ("3\n1\n2\n3\n", 0),
        "growth": ("100\n0\n50\n99\n", 0),
        "set": ("9\n", 0),
        "strings": ("b\n2\n", 0),
        "free": ("ok\n", 0),
        "free-an-empty-vector": ("ok\n", 0),
        "read-across-a-call": ("10\n4\n", 0),
        "free-a-parameter": ("3\n", 0),
        "name-argument-through-a-relay": ("3\n", 0),
        "loop-move-panics-on-second-iteration": ("", 2),
        "move-then-free-in-both-branches": ("ok\n", 0),
        "small-elements": ("true\n3\n", 0),
        "struct-elements": ("4\n", 0),
        "nested": ("0\n", 0),
        "get-out-of-bounds": ("", 2),
        "set-out-of-bounds": ("", 2),
    }

    def run_case(self, name: str, opt: str) -> subprocess.CompletedProcess:
        target = str(ROOT / "build" / f"txt-vec-{name}-{opt[1:]}")
        driver.run_test(str(VEC), name, target, opt=opt)
        return subprocess.run([target], capture_output=True, text=True)

    def test_every_case(self):
        for name, (stdout, code) in self.CASES.items():
            for opt in ("-O0", "-O2"):
                with self.subTest(case=name, opt=opt):
                    done = self.run_case(name, opt)
                    self.assertEqual(done.stdout, stdout, done.stderr)
                    self.assertEqual(done.returncode, code, done.stderr)
                    # A debug build quarantines released blocks and reports
                    # every block still live; a vector that frees its buffer
                    # through rt_free must leave nothing behind.
                    self.assertNotIn("LEAK", done.stderr)

    def test_a_vector_buffer_is_tracked_by_the_leak_tracker(self):
        # A vector grows through rt_alloc/rt_free, so a buffer the program
        # never releases is a reported leak, named by the push that made it:
        # 4 elements of 8 bytes.
        done = self.run_case("growth-leak-is-reported", "-O0")
        self.assertEqual(done.returncode, 0)
        self.assertIn(
            "LEAK 32 bytes at vec/test/growth-leak-is-reported/", done.stderr
        )

    def test_a_pushed_vector_is_moved_and_its_buffer_is_stranded(self):
        # R5: the descriptor moved into the element, so the source is empty and
        # the element is the one descriptor. An element is not a place
        # ``vec-free`` can name, so the inner buffer is still live at exit and
        # the tracker reports it — that is the remaining hole, and this case
        # pins its shape rather than hiding it.
        done = self.run_case("nested-buffer-is-stranded", "-O0")
        self.assertEqual(done.stdout, "7\n", done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(
            "LEAK 32 bytes at vec/test/nested-buffer-is-stranded/", done.stderr
        )

    def test_a_vector_index_out_of_bounds_names_its_node(self):
        done = self.run_case("get-out-of-bounds", "-O0")
        self.assertEqual(done.returncode, 2)
        self.assertIn("R0003: vector index out of bounds", done.stderr)
        self.assertTrue(done.stderr.startswith("vec/test/get-out-of-bounds/"))


class ReadAllTest(unittest.TestCase):
    """``(read-all)``: every byte on standard input, as one ``:str``.

    The language had no input at all before this form. The three example
    programs in ``examples/`` read their input through it, so its contract is
    pinned here in three layers the way the rest of the string surface is: the
    checker's type, the lowerer's runtime call, and real binaries that see a
    pipe, an empty file and a NUL byte.
    """

    BUILD = ROOT / "build"

    SOURCE = """\
(mod read)

(test reads
  (expect (out "0\\n") (code 0))
  (println (str-len (read-all))))
"""

    def target(self, opt: str) -> str:
        self.BUILD.mkdir(parents=True, exist_ok=True)
        source = self.BUILD / "read-all.fixture.logos"
        source.write_text(self.SOURCE, encoding="utf-8")
        name = str(self.BUILD / f"read-all.fixture-{opt[1:]}")
        driver.run_test(str(source), "reads", name, opt=opt)
        return name

    def test_read_all_is_a_str(self):
        cm = checked("(mod m)(fn f :str () (ret (read-all)))")
        self.assertEqual(type_of(cm, "read-all"), Prim("str"))

    def test_read_all_takes_no_arguments(self):
        error = fail_on("(mod m)(fn f :str () (ret (read-all 1)))")
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "'read-all' takes no arguments")

    def test_the_lowering_calls_the_runtime(self):
        module = lowered("(mod m)(fn main :void () (println (read-all)))")
        main = next(fn for fn in module.functions if fn.name.endswith("main"))
        self.assertIn("rt_read_all", callees(main))

    def test_a_binary_reads_a_pipe_to_end_of_file(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = subprocess.run(
                    [self.target(opt)], input=b"a\x00b\n", capture_output=True
                )
                self.assertEqual(done.stdout, b"4\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn(b"LEAK", done.stderr)

    def test_empty_input_is_the_empty_string(self):
        done = subprocess.run([self.target("-O0")], input=b"", capture_output=True)
        self.assertEqual(done.stdout, b"0\n", done.stderr)

    def test_the_buffer_grows_past_its_first_capacity(self):
        # 200000 bytes is far beyond any first allocation, so this exercises
        # the runtime's growth loop rather than one lucky read.
        done = subprocess.run(
            [self.target("-O2")], input=b"x" * 200000, capture_output=True
        )
        self.assertEqual(done.stdout, b"200000\n", done.stderr)


if __name__ == "__main__":
    unittest.main()
