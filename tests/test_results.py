"""Results, optionals and enums: the Task 11 surface, in three layers.

The checker's half — ``(res T E)``, ``(opt T)``, declared error enums with
payloads, the five result forms and the three codes results add: E0600 for a
match that misses a variant, E0601 for a ``try`` whose error types differ, and
E0602 for a result nobody handles. The lowerer's half — a result as
``{ i1, T, E }``, an optional as ``{ i1, T }``, an enum as a tag and a payload
area — and real binaries at ``-O0`` and ``-O2``.

The negative corpus lives in ``tests/type``: one file per diagnostic, its
``.expect`` naming the code, exactly as the rest of the checker's refusals do.
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
from logos.types import EnumRef, Opt, Prim, Res, StructRef

ROOT = Path(__file__).resolve().parents[1]
EXEC = ROOT / "tests" / "exec" / "results.logos"

#: A declared error enum, so every inline result can name one.
ERR = "(type err (enum (bad :i64) (eof)))"


def _loader(name: str):
    raise FileNotFoundError(name)


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""
    return check_module(resolve(parse(source, "m.logos"), _loader))


def fail_on(source: str) -> LogosError:
    """The diagnostic checking ``source`` raises, asserted to be one."""
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
    found = [fn.symbol for fn in module.functions]
    raise AssertionError(f"no function '{symbol}' in {found}")


def ops(fn: ir.IrFn) -> list[tuple]:
    return [instr for block in fn.blocks for instr in block.instrs]


class EnumDeclarationTest(unittest.TestCase):
    def test_a_declared_enum_records_its_variants_in_order(self):
        cm = checked("(mod m)(type e (enum (bad :u64) (eof)))")
        self.assertEqual(
            cm.enums["e"], (("bad", Prim("u64")), ("eof", None))
        )

    def test_a_payload_resolves_its_named_type(self):
        cm = checked("(mod m)(type box (struct (x :i64)))"
                     "(type e (enum (a box) (b)))")
        self.assertEqual(cm.enums["e"][0], ("a", StructRef("box")))

    def test_an_enum_is_a_named_type(self):
        cm = checked("(mod m)(type e (enum (a)))")
        self.assertEqual(cm.types["e"], EnumRef("e"))

    def test_a_duplicate_variant_is_e0003(self):
        error = fail_on("(mod m)(type e (enum (a) (a)))")
        self.assertEqual(error.code, "E0003")
        self.assertEqual(error.message, "duplicate variant 'a'")

    def test_a_reserved_variant_name_is_e0003(self):
        for name in ("ok", "err", "some", "none", "try"):
            with self.subTest(variant=name):
                error = fail_on(f"(mod m)(type e (enum ({name})))")
                self.assertEqual(error.code, "E0003")
                self.assertIn("reserved", error.message)

    def test_an_empty_enum_is_e0002(self):
        error = fail_on("(mod m)(type e (enum))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "an enum needs at least one variant")

    def test_a_malformed_variant_is_e0002(self):
        error = fail_on("(mod m)(type e (enum (a :i64 :i64)))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "an enum variant must be (name) or (name type)")

    def test_a_void_payload_is_e0100(self):
        error = fail_on("(mod m)(type e (enum (a :void)))")
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot hold :void", error.message)

    def test_a_vector_payload_is_e0002(self):
        error = fail_on("(mod m)(type e (enum (a (vec :i64))))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "vector payloads are not supported yet")

    def test_a_type_variable_payload_is_e0501(self):
        error = fail_on("(mod m)(type e (enum (a T)))")
        self.assertEqual(error.code, "E0501")


class ResultTypeTest(unittest.TestCase):
    def test_the_error_slot_must_be_a_declared_enum(self):
        error = fail_on("(mod m)(fn f :(res :i64 :i64) () (ret (ok 1)))")
        self.assertEqual(error.code, "E0101")
        self.assertIn("must be a declared enum", error.message)

    def test_a_res_of_a_vector_is_refused(self):
        error = fail_on("(mod m)" + ERR + "(fn f :(res (vec :i64) err) () (ret))")
        self.assertIn(error.code, ("E0002",))
        self.assertIn("vector", error.message)

    def test_an_optional_of_a_vector_is_refused(self):
        error = fail_on("(mod m)(fn f :(opt (vec :i64)) () (ret))")
        self.assertEqual(error.code, "E0002")
        self.assertIn("vector", error.message)

    def test_ok_takes_the_result_type_the_return_annotation_names(self):
        cm = checked("(mod m)(type e (enum (a)))"
                     "(fn f :(res :i64 e) () (ret (ok 1)))")
        self.assertEqual(cm.fn_sigs["f"].ret, Res(Prim("i64"), EnumRef("e")))
        self.assertEqual(cm.expr_types["m/f/3/0"], Res(Prim("i64"), EnumRef("e")))

    def test_ok_without_a_result_annotation_is_e0100(self):
        error = fail_on("(mod m)(type e (enum (a)))"
                        "(fn f :i64 () (ret (ok 1)))")
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot infer the result type of 'ok'", error.message)

    def test_ok_checks_its_value_against_the_ok_type(self):
        error = fail_on("(mod m)(type e (enum (a)))"
                        "(fn f :(res :i64 e) () (ret (ok \"x\")))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'ok' expects :i64, found :str")

    def test_err_checks_its_value_against_the_error_enum(self):
        error = fail_on("(mod m)(type e (enum (a)))"
                        "(fn f :(res :i64 e) () (ret (err 1)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'err' expects e, found :i64")

    def test_some_infers_its_element_type(self):
        cm = checked("(mod m)(fn f :void () (let o (some 5)) (println 1))")
        self.assertEqual(cm.expr_types["m/f/2/1"], Opt(Prim("i64")))

    def test_some_takes_the_element_of_its_annotation(self):
        cm = checked("(mod m)(fn f :void () (let o :(opt :u8) (some 5)) (println 1))")
        self.assertEqual(cm.declared["m/f/2"], Opt(Prim("u8")))

    def test_some_checks_its_value_against_the_element(self):
        error = fail_on(
            "(mod m)(fn f :void () (let o :(opt :i64) (some \"x\")) (println 1))"
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'some' expects :i64, found :str")

    def test_none_needs_an_annotation(self):
        error = fail_on("(mod m)(fn f :void () (let o (none)) (println 1))")
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot infer the element type of 'none'", error.message)

    def test_none_takes_no_values(self):
        error = fail_on(
            "(mod m)(fn f :void () (let o :(opt :i64) (none 1)) (println 1))"
        )
        self.assertEqual(error.code, "E0102")

    def test_a_result_whose_ok_type_differs_is_still_a_result(self):
        # The plan's spelling of the type is swapped: `(res E T)` is still a
        # result, and only the error slot has to be an enum.
        cm = checked("(mod m)(type e (enum (a)))"
                     "(fn f :(res :str e) () (ret (ok \"x\")))")
        self.assertEqual(cm.fn_sigs["f"].ret, Res(Prim("str"), EnumRef("e")))

    def test_a_vector_cannot_be_carried_in_a_result(self):
        error = fail_on("(mod m)(type e (enum (a)))(fn f :void ()"
                        " (letmut v (vec :i64) (make (vec :i64)))"
                        " (let r :(res (vec :i64) e) (ok v)) (println 1))")
        self.assertIn(error.code, ("E0002", "E0402"))


class TryTest(unittest.TestCase):
    def test_try_yields_the_ok_type(self):
        cm = checked("(mod m)" + ERR +
                     "(fn g :(res :i64 err) () (ret (ok 1)))"
                     "(fn f :(res :str err) () (let v (try (g))) (ret (ok \"x\")))")
        self.assertEqual(cm.expr_types["m/f/3/1"], Prim("i64"))

    def test_try_propagates_an_exactly_matching_error_type(self):
        cm = checked("(mod m)" + ERR +
                     "(fn g :(res :i64 err) () (ret (ok 1)))"
                     "(fn f :(res :i64 err) () (let v (try (g))) (ret (ok v)))")
        self.assertEqual(cm.expr_types["m/f/3/1"], Prim("i64"))

    def test_a_different_error_type_is_e0601(self):
        error = fail_on("(mod m)" + ERR +
                        "(type other (enum (nope)))"
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :(res :i64 other) () (let v (try (g))) (ret (ok v)))")
        self.assertEqual(error.code, "E0601")
        self.assertEqual(error.path, "m/f/3/1")
        self.assertIn("cannot 'try'", error.message)

    def test_a_try_in_a_function_without_a_result_is_e0601(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :i64 () (ret (try (g))))")
        self.assertEqual(error.code, "E0601")
        self.assertIn("cannot propagate", error.message)

    def test_a_try_of_a_non_result_is_e0601(self):
        error = fail_on("(mod m)(fn f :i64 () (ret (try 1)))")
        self.assertEqual(error.code, "E0601")
        self.assertEqual(error.message, "'try' needs a result, found :i64")

    def test_try_takes_exactly_one_operand(self):
        error = fail_on("(mod m)" + ERR + "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :(res :i64 err) () (let v (try (g) (g))) (ret (ok v)))")
        self.assertEqual(error.code, "E0102")

    def test_the_corpus_case_reports_e0601(self):
        case = ROOT / "tests" / "type" / "try-mismatch.logos"
        expected = case.with_suffix(".expect").read_text(encoding="utf-8").strip()
        with self.assertRaises(LogosError) as caught:
            checked(case.read_text(encoding="utf-8"))
        error = caught.exception
        self.assertEqual(error.code, expected)
        self.assertEqual(error.path.split("/")[0], "try-mismatch")


class UnhandledResultTest(unittest.TestCase):
    def test_a_result_in_statement_position_is_e0602(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :void () (g))")
        self.assertEqual(error.code, "E0602")
        self.assertEqual(error.path, "m/f/2")
        self.assertIn("never handled", error.message)

    def test_a_result_binding_that_is_never_handled_is_e0602(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :void () (let r (g)) (println 1))")
        self.assertEqual(error.code, "E0602")
        self.assertEqual(error.path, "m/f/2")
        self.assertIn("'r'", error.message)

    def test_a_matched_binding_is_handled(self):
        checked("(mod m)" + ERR +
                "(fn g :(res :i64 err) () (ret (ok 1)))"
                "(fn f :void () (let r (g))"
                " (match r ((ok v) (println v)) ((err e) (println 0))))")

    def test_a_tried_binding_is_handled(self):
        checked("(mod m)" + ERR +
                "(fn g :(res :i64 err) () (ret (ok 1)))"
                "(fn f :(res :i64 err) () (let r (g)) (ret (ok (try r))))")

    def test_a_returned_result_is_handled(self):
        checked("(mod m)" + ERR +
                "(fn g :(res :i64 err) () (ret (ok 1)))"
                "(fn f :(res :i64 err) () (let r (g)) (ret r))")

    def test_a_result_passed_to_a_callee_is_handled(self):
        checked("(mod m)" + ERR +
                "(fn g :(res :i64 err) () (ret (ok 1)))"
                "(fn take :void ((r :(res :i64 err))) (match r ((ok v) (println v))"
                " ((err e) (println 0))))"
                "(fn f :void () (let r (g)) (take r))")

    def test_a_result_parameter_must_be_handled(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn take :void ((r :(res :i64 err))) (println 1))")
        self.assertEqual(error.code, "E0602")
        self.assertIn("'r'", error.message)

    def test_a_set_gives_the_binding_a_new_obligation(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :void () (letmut r (g))"
                        " (match r ((ok v) (println v)) ((err e) (println 0)))"
                        " (set r (g)))")
        self.assertEqual(error.code, "E0602")

    def test_a_closure_that_captures_a_result_must_handle_it(self):
        checked("(mod m)" + ERR +
                "(fn g :(res :i64 err) () (ret (ok 1)))"
                "(fn f :i64 () (let r (g))"
                " (let h (fn :i64 () (ret (match r ((ok v) v) ((err e) 0)))))"
                " (ret (h)))")

    def test_a_result_copied_inside_a_closure_is_that_closure_s_obligation(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :i64 () (let r (g))"
                        " (let h (fn :i64 () (let x r) (ret 1))) (ret (h)))")
        self.assertEqual(error.code, "E0602")
        self.assertIn("'x'", error.message)

    def test_an_optional_carries_no_obligation(self):
        checked("(mod m)(fn f :void () (let o (some 1)) (println 1))")

    def test_a_result_inside_a_block_ends_with_it(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :void () (if true (let r (g)) (println 1)))")
        self.assertEqual(error.code, "E0602")
        self.assertEqual(error.path, "m/f/2/1")

    def test_a_result_stored_in_a_field_is_refused(self):
        error = fail_on("(mod m)" + ERR +
                        "(type box (struct (r (res :i64 err))))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "result fields are not supported yet")

    def test_a_result_stored_in_a_vector_is_refused(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn f :void () (let v (vec (res :i64 err))"
                        " (make (vec (res :i64 err)))) (ret))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "a result cannot be a vector element yet")

    def test_a_result_stored_in_an_optional_is_refused(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn f :void () (let o :(opt (res :i64 err)) (none)) (ret))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "a result cannot be inside an optional yet")

    def test_a_result_in_an_enum_payload_is_refused(self):
        error = fail_on("(mod m)" + ERR +
                        "(type t (enum (p (res :i64 err))))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "result payloads are not supported yet")

    def test_a_result_carried_in_an_optional_value_is_e0602(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :void () (let o (some (g))) (println 1))")
        self.assertEqual(error.code, "E0602")
        self.assertIn("cannot be carried in an optional", error.message)

    def test_the_corpus_case_reports_e0602(self):
        case = ROOT / "tests" / "type" / "unhandled-result.logos"
        expected = case.with_suffix(".expect").read_text(encoding="utf-8").strip()
        with self.assertRaises(LogosError) as caught:
            checked(case.read_text(encoding="utf-8"))
        self.assertEqual(caught.exception.code, expected)


class ExhaustiveMatchTest(unittest.TestCase):
    SOURCE = ("(mod m)" + ERR + "(fn f :i64 ((e err))"
              " (ret (match e {arms})))")

    def test_a_match_over_every_variant_checks(self):
        checked(self.SOURCE.format(arms="((bad n) n) (eof 0)"))

    def test_a_missing_variant_is_e0600(self):
        error = fail_on(self.SOURCE.format(arms="((bad n) n)"))
        self.assertEqual(error.code, "E0600")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(
            error.message, "this match on err does not handle 'eof'"
        )

    def test_a_wildcard_covers_the_rest(self):
        checked(self.SOURCE.format(arms="((bad n) n) (_ 0)"))

    def test_a_result_match_must_handle_err(self):
        error = fail_on("(mod m)" + ERR +
                        "(fn g :(res :i64 err) () (ret (ok 1)))"
                        "(fn f :i64 () (ret (match (g) ((ok v) v))))")
        self.assertEqual(error.code, "E0600")
        self.assertEqual(
            error.message, "this match on (res :i64 err) does not handle 'err'"
        )

    def test_an_optional_match_must_handle_none(self):
        error = fail_on("(mod m)(fn f :i64 () (ret (match (some 1) ((some v) v))))")
        self.assertEqual(error.code, "E0600")
        self.assertIn("'none'", error.message)

    def test_a_second_arm_for_one_variant_is_e0002(self):
        error = fail_on(self.SOURCE.format(arms="((bad n) n) ((bad m) m) (eof 0)"))
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "an arm for 'bad' can never match")

    def test_an_unknown_variant_is_e0100(self):
        error = fail_on(self.SOURCE.format(arms="((nope n) n) (eof 0)"))
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "enum 'err' has no variant 'nope'")

    def test_a_payload_variant_needs_a_binding(self):
        error = fail_on(self.SOURCE.format(arms="(bad 1) (eof 0)"))
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "variant 'bad' holds a value: write (bad name)")

    def test_a_payload_less_variant_takes_no_binding(self):
        error = fail_on(self.SOURCE.format(arms="((bad n) n) ((eof x) 0)"))
        self.assertEqual(error.code, "E0102")

    def test_a_payload_binding_is_an_immutable_local_of_the_arm(self):
        checked(self.SOURCE.format(arms="((bad n) n) (eof 0)"))

    def test_an_integer_match_still_needs_no_more_arms(self):
        checked("(mod m)(fn f :i64 ((x :i64)) (ret (match x (1 1))))")

    def test_the_corpus_case_reports_e0600(self):
        case = ROOT / "tests" / "type" / "non-exhaustive.logos"
        expected = case.with_suffix(".expect").read_text(encoding="utf-8").strip()
        with self.assertRaises(LogosError) as caught:
            checked(case.read_text(encoding="utf-8"))
        self.assertEqual(caught.exception.code, expected)


class VariantConstructorTest(unittest.TestCase):
    def test_a_payload_variant_has_the_enum_type(self):
        cm = checked("(mod m)(type e (enum (bad :i64) (eof)))"
                     "(fn f e () (ret (bad 3)))")
        self.assertEqual(cm.expr_types["m/f/2/0"], EnumRef("e"))

    def test_a_payload_less_variant_is_a_bare_value(self):
        cm = checked("(mod m)(type e (enum (bad :i64) (eof)))"
                     "(fn f e () (let x eof) (ret eof))")
        self.assertEqual(cm.declared["m/f/2"], EnumRef("e"))

    def test_the_payload_is_checked_against_its_declared_type(self):
        error = fail_on("(mod m)(type e (enum (bad :i64)))"
                        "(fn f e () (ret (bad \"x\")))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "variant 'bad' holds :i64, found :str")

    def test_a_payload_variant_takes_one_value(self):
        error = fail_on("(mod m)(type e (enum (bad :i64)))"
                        "(fn f e () (ret (bad)))")
        self.assertEqual(error.code, "E0102")

    def test_a_payload_less_variant_takes_no_value(self):
        error = fail_on("(mod m)(type e (enum (eof)))"
                        "(fn f e () (ret (eof 1)))")
        self.assertEqual(error.code, "E0102")

    def test_a_binding_shadows_a_variant_of_the_same_name(self):
        # A parameter or local called `b` is the binding, not the variant `b`:
        # scope is searched before the variant set, in the checker and in the
        # lowerer, so the read is a load of `:i64` and not a tag.
        source = ("(mod m)(type e (enum (a :i64) (b)))"
                  "(fn f :i64 ((b :i64)) (ret b))")
        self.assertEqual(checked(source).fn_sigs["f"].ret, Prim("i64"))
        kinds = [i[0] for i in ops(fn_named(lowered(source), "lg.m.f"))]
        self.assertNotIn("insertvalue", kinds)

    def test_an_ambiguous_variant_is_e0005(self):
        error = fail_on("(mod m)(type a (enum (same))) (type b (enum (same)))"
                        "(fn f a () (ret (same)))")
        self.assertEqual(error.code, "E0005")
        self.assertIn("more than one enum", error.message)


class ResultLoweringTest(unittest.TestCase):
    def test_a_result_is_a_tag_and_two_slots(self):
        self.assertEqual(
            llvm.type_text(Res(Prim("i64"), Prim("i32"))), "{ i1, i64, i32 }"
        )

    def test_an_optional_is_a_tag_and_one_slot(self):
        self.assertEqual(llvm.type_text(Opt(Prim("str"))), "{ i1, { i8*, i64 } }")

    def test_ok_sets_the_tag(self):
        module = lowered("(mod m)" + ERR +
                         "(fn f :(res :i64 err) () (ret (ok 1)))")
        kinds = [i[0] for i in ops(fn_named(module, "lg.m.f"))]
        self.assertIn("insertvalue", kinds)
        tags = [i[5] for i in ops(fn_named(module, "lg.m.f"))
                if i[0] == "insertvalue" and i[6] == 0]
        self.assertIn("true", tags)

    def test_err_clears_the_tag(self):
        module = lowered("(mod m)" + ERR +
                         "(fn f :(res :i64 err) () (ret (err eof)))")
        tags = [i[5] for i in ops(fn_named(module, "lg.m.f"))
                if i[0] == "insertvalue" and i[6] == 0]
        self.assertIn("false", tags)

    def test_none_clears_the_tag(self):
        module = lowered("(mod m)(fn f :(opt :i64) () (ret (none)))")
        tags = [i[5] for i in ops(fn_named(module, "lg.m.f"))
                if i[0] == "insertvalue" and i[6] == 0]
        self.assertIn("false", tags)

    def test_some_sets_the_tag(self):
        module = lowered("(mod m)(fn f :(opt :i64) () (ret (some 1)))")
        tags = [i[5] for i in ops(fn_named(module, "lg.m.f"))
                if i[0] == "insertvalue" and i[6] == 0]
        self.assertIn("true", tags)

    def test_an_enum_is_a_tag_and_a_payload_area(self):
        text = emitted("(mod m)(type e (enum (bad :i64) (eof)))"
                       "(fn f e ((x :i64)) (ret (bad x)))")
        self.assertIn("{ i32, [1 x i64] }", text)
        self.assertIn("bitcast [1 x i64]*", text)

    def test_a_match_over_an_optional_tests_the_tag(self):
        module = lowered("(mod m)(fn f :i64 ((o :(opt :i64)))"
                         " (ret (match o ((some v) v) ((none) 0))))")
        kinds = [i[0] for i in ops(fn_named(module, "lg.m.f"))]
        self.assertIn("extractvalue", kinds)
        self.assertIn("icmp", kinds)

    def test_try_returns_the_error_on_the_error_path(self):
        module = lowered("(mod m)" + ERR +
                         "(fn g :(res :i64 err) () (ret (ok 1)))"
                         "(fn f :(res :i64 err) () (let v (try (g))) (ret (ok v)))")
        fn = fn_named(module, "lg.m.f")
        error_block = next(b for b in fn.blocks if b.label.startswith("try"))
        self.assertEqual(error_block.term[0], "ret")
        self.assertEqual(error_block.term[1], Res(Prim("i64"), EnumRef("err")))

    def test_a_result_with_no_layout_is_e0002(self):
        error = None
        try:
            lowered("(mod m)(type e (enum (a)))"
                    "(type box (struct (r (res (shared :i64) e))))"
                    "(fn f :void ((b box)) (ret))")
        except LogosError as caught:
            error = caught
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "E0002")


class ResultsExecTest(unittest.TestCase):
    """Real binaries: results.logos at both optimisation levels."""

    CASES = {
        "match-ok": "7\n",
        "match-err-payload": "42\n",
        "match-str-payload": "empty\n",
        "try-success": "14\n",
        "try-propagates": "42\n",
        "optional-some": "5\n",
        "optional-none": "0\n",
        "optional-binding": "9\n",
        "enum-payload-less": "99\n",
        "enum-in-a-struct-field": "7\n",
        "result-through-a-parameter": "5\n",
        "result-in-a-binding": "7\n",
        "match-produces-a-value": "0\n",
        "wildcard-covers-an-enum": "7\n",
        "enum-payload-of-an-enum": "5\n",
        "enum-in-a-vector": "2\n1\n",
        "alloc-of-an-enum": "7\n",
        "try-inside-one-expression": "14\n",
        "try-inside-a-loop": "21\n",
    }

    def test_every_case(self):
        for name, stdout in self.CASES.items():
            for opt in ("-O0", "-O2"):
                with self.subTest(case=name, opt=opt):
                    target = str(ROOT / "build" / f"results-{name}-{opt[1:]}")
                    driver.run_test(str(EXEC), name, target, opt=opt)
                    done = subprocess.run([target], capture_output=True, text=True)
                    self.assertEqual(done.stdout, stdout, done.stderr)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertNotIn("LEAK", done.stderr)


if __name__ == "__main__":
    unittest.main()
