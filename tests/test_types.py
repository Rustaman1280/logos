"""Tests for the type model and the core checker.

``logos.types`` owns the vocabulary: it turns the syntax of a type into a value
and decides when two values are the same type. It does not know which names a
module declares; ``logos.check`` does, and reports an undeclared name as E0101.

The checker is a pure function of a resolved module. It records a type for
every node it visits under the node's path, exactly as the parser assigned it,
so a later pass can type any expression without re-deriving scope.

Type names follow the language's one spelling per name: an atom beginning with
``:`` is a primitive, and an atom without the colon is a declared type or a
type variable. ``(res :i64 parse-error)`` is therefore the shape of a composite
that names a user type.
"""

import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from logos.check import CheckedModule, check_module
from logos.diag import LogosError
from logos.nodes import Node
from logos.parser import parse
from logos.resolve import Module, resolve
from logos.types import (
    PRIMITIVES,
    Arr,
    EnumRef,
    FnTy,
    Opt,
    Own,
    Prim,
    Ptr,
    Res,
    Shared,
    StructRef,
    TypeVar,
    Vec,
    compatible,
    parse_type,
)

TYPE = Path(__file__).parent / "type"

#: A call that returns :i64 passed where :i32 is expected. The widening case
#: the brief draws with ``(deref p)``; deref arrives in a later task, a call
#: does not.
WIDENING = (
    "(mod m)"
    "(fn id64 :i64 ((x :i64)) (ret x))"
    "(fn take32 :i32 ((a :i32)) (ret a))"
    "(fn f :i64 () (ret (take32 (id64 1))))"
)


def resolve_src(src: str, files: dict[str, str] | None = None) -> Module:
    """Resolve ``src`` with ``files`` as the loader's store."""
    sources = dict(files or {})

    def loader(module: str):
        if module not in sources:
            raise FileNotFoundError(module)
        return parse(sources[module], f"{module}.logos")

    return resolve(parse(src, "a.logos"), loader, "a.logos")


def check_src(src: str, files: dict[str, str] | None = None) -> CheckedModule:
    """Check ``src`` and return the checked module."""
    return check_module(resolve_src(src, files))


def expect_error(
    src: str, code: str, files: dict[str, str] | None = None
) -> LogosError:
    """Check ``src`` and return the diagnostic it must raise."""
    try:
        check_src(src, files)
    except LogosError as error:
        if error.code != code:
            raise AssertionError(f"expected {code}, got {error.render()}") from None
        return error
    raise AssertionError(f"expected {code}, but the module checked")


def type_syntax(text: str):
    """The node (or token) that spells ``text`` in a return-type position."""
    root = parse(f"(mod m)(fn f {text} () (ret))", "m.logos")
    fn = root.items[1]
    assert isinstance(fn, Node)
    return fn.items[2]


class TypeSyntaxTest(unittest.TestCase):
    def test_primitives_print_with_a_colon(self):
        for name in PRIMITIVES:
            with self.subTest(name=name):
                self.assertEqual(str(Prim(name)), f":{name}")

    def test_composites_print_logos_syntax(self):
        cases = [
            (Ptr(Prim("u8")), "(ptr :u8)"),
            (Own(Prim("i64")), "(own :i64)"),
            (Arr(Prim("i64"), 4), "(arr :i64 4)"),
            (Vec(Prim("i64")), "(vec :i64)"),
            (Vec(FnTy((Prim("i64"),), Prim("bool"))), "(vec (fn (:i64) :bool))"),
            (Res(Prim("i64"), StructRef("parse-error")), "(res :i64 parse-error)"),
            (Opt(Prim("str")), "(opt :str)"),
            (Shared(Prim("i64")), "(shared :i64)"),
            (FnTy((Prim("i64"), Prim("bool")), Prim("i64")), "(fn (:i64 :bool) :i64)"),
            (FnTy((), Prim("void")), "(fn () :void)"),
            (FnTy((Prim("i64"),), Prim("i64"), True), "(fn* (:i64) :i64)"),
            (StructRef("point"), "point"),
            (EnumRef("parse-error"), "parse-error"),
            (TypeVar("T"), "T"),
        ]
        for ty, text in cases:
            with self.subTest(text=text):
                self.assertEqual(str(ty), text)

    def test_types_are_frozen_value_objects(self):
        self.assertEqual(Vec(Prim("i64")), Vec(Prim("i64")))
        self.assertEqual(hash(Vec(Prim("i64"))), hash(Vec(Prim("i64"))))
        self.assertNotEqual(Vec(Prim("i64")), Vec(Prim("u8")))
        self.assertNotEqual(Arr(Prim("i64"), 4), Arr(Prim("i64"), 5))
        with self.assertRaises(FrozenInstanceError):
            Vec(Prim("i64")).elem = Prim("u8")

    def test_parse_type_reads_atoms(self):
        cases = [
            (":i64", Prim("i64")),
            (":str", Prim("str")),
            (":void", Prim("void")),
            ("point", StructRef("point")),
            ("parse-error", StructRef("parse-error")),
            ("U", TypeVar("U")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_type(type_syntax(text)), expected)

    def test_parse_type_reads_composites(self):
        cases = [
            ("(vec :i64)", Vec(Prim("i64"))),
            ("(arr :u8 16)", Arr(Prim("u8"), 16)),
            ("(ptr :u8)", Ptr(Prim("u8"))),
            ("(own :str)", Own(Prim("str"))),
            ("(opt :i64)", Opt(Prim("i64"))),
            ("(shared :i64)", Shared(Prim("i64"))),
            (
                "(res :i64 parse-error)",
                Res(Prim("i64"), StructRef("parse-error")),
            ),
            ("(fn (:i64 :bool) :i64)", FnTy((Prim("i64"), Prim("bool")), Prim("i64"))),
            ("(fn () :void)", FnTy((), Prim("void"))),
            ("(fn* (:i64) :i64)", FnTy((Prim("i64"),), Prim("i64"), True)),
            ("(vec (arr :i64 4))", Vec(Arr(Prim("i64"), 4))),
            # A composite parameter is wrapped once more: the list beside the
            # arrow always holds one entry per parameter.
            ("(fn ((vec :i64)) :i64)", FnTy((Vec(Prim("i64")),), Prim("i64"))),
            # The language prints a composite behind a type marker; the lexer
            # hands it over as a lone ':' token in front of the list.
            ("(fn (:(vec :i64)) :i64)", FnTy((Vec(Prim("i64")),), Prim("i64"))),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_type(type_syntax(text)), expected)

    def test_a_parsed_type_prints_itself_back(self):
        for text in (
            "(vec :i64)",
            "(arr :u8 16)",
            "(fn (:i64) :bool)",
            "(fn* (:i64) :bool)",
            "point",
        ):
            with self.subTest(text=text):
                self.assertEqual(str(parse_type(type_syntax(text))), text)

    def test_an_unknown_primitive_is_E0101(self):
        with self.assertRaises(LogosError) as caught:
            parse_type(type_syntax(":wat"), path="m/f")
        error = caught.exception
        self.assertEqual(error.code, "E0101")
        self.assertEqual(error.path, "m/f")
        self.assertIn(":wat", error.message)

    def test_an_unknown_type_form_is_E0101(self):
        for text in ("(wat :i64)", "(arr :i64 x)", "(arr :i64 -1)", "()"):
            with self.subTest(text=text):
                with self.assertRaises(LogosError) as caught:
                    parse_type(type_syntax(text))
                self.assertEqual(caught.exception.code, "E0101")

    def test_a_non_type_token_is_E0101(self):
        with self.assertRaises(LogosError) as caught:
            parse_type(type_syntax("2"))
        self.assertEqual(caught.exception.code, "E0101")


class CompatibleTest(unittest.TestCase):
    def test_identical_types_match(self):
        self.assertTrue(compatible(Prim("i64"), Prim("i64")))
        self.assertTrue(compatible(StructRef("p"), StructRef("p")))
        self.assertTrue(compatible(EnumRef("e"), EnumRef("e")))

    def test_widths_never_widen(self):
        self.assertFalse(compatible(Prim("i32"), Prim("i64")))
        self.assertFalse(compatible(Prim("i64"), Prim("i32")))
        self.assertFalse(compatible(Prim("i64"), Prim("f64")))
        self.assertFalse(compatible(Prim("u8"), Prim("i8")))

    def test_composites_compare_structurally(self):
        self.assertTrue(compatible(Vec(Prim("i64")), Vec(Prim("i64"))))
        self.assertTrue(
            compatible(
                Res(Prim("i64"), StructRef("e")), Res(Prim("i64"), StructRef("e"))
            )
        )
        self.assertFalse(compatible(Vec(Prim("i64")), Arr(Prim("i64"), 4)))
        self.assertFalse(compatible(Arr(Prim("i64"), 4), Arr(Prim("i64"), 5)))

    def test_a_named_type_is_nominal(self):
        self.assertFalse(compatible(StructRef("p"), StructRef("q")))
        self.assertFalse(compatible(StructRef("p"), EnumRef("p")))

    def test_no_widening_reaches_inside_a_composite(self):
        self.assertFalse(compatible(Vec(Prim("i32")), Vec(Prim("i64"))))

    def test_function_types_compare_parameter_by_parameter(self):
        fn = FnTy((Prim("i64"),), Prim("bool"))
        self.assertTrue(compatible(fn, FnTy((Prim("i64"),), Prim("bool"))))
        self.assertFalse(compatible(fn, FnTy((Prim("i64"), Prim("i64")), Prim("bool"))))
        self.assertFalse(compatible(fn, FnTy((Prim("i64"),), Prim("i64"))))
        self.assertFalse(compatible(fn, Prim("i64")))

    def test_a_capturing_function_type_is_not_a_plain_one(self):
        # R12: the two spellings are different types, which is what keeps a
        # capturing closure out of a plain function-typed parameter.
        plain = FnTy((Prim("i64"),), Prim("i64"))
        capture = FnTy((Prim("i64"),), Prim("i64"), True)
        self.assertFalse(compatible(plain, capture))
        self.assertFalse(compatible(capture, plain))
        self.assertTrue(compatible(capture, FnTy((Prim("i64"),), Prim("i64"), True)))
        self.assertNotEqual(plain, capture)

    def test_a_type_variable_is_not_a_wildcard(self):
        self.assertTrue(compatible(TypeVar("T"), TypeVar("T")))
        self.assertFalse(compatible(TypeVar("T"), Prim("i64")))
        self.assertFalse(compatible(Prim("i64"), TypeVar("T")))
        self.assertFalse(compatible(TypeVar("T"), Vec(Prim("i64"))))
        self.assertFalse(compatible(TypeVar("T"), TypeVar("U")))


class ModuleCheckTest(unittest.TestCase):
    def test_a_clean_module_produces_signatures_and_expression_types(self):
        module = resolve_src("(mod m)(fn f :i64 ((a :i64)) (ret (+ a 1)))")
        checked = check_module(module)
        self.assertIsInstance(checked, CheckedModule)
        self.assertIs(checked.module, module)
        self.assertEqual(checked.types, {})
        self.assertEqual(checked.fn_sigs, {"f": FnTy((Prim("i64"),), Prim("i64"))})
        # ``ret`` is a statement, so it records :void; its child is the sum.
        self.assertEqual(
            checked.expr_types, {"m/f/2": Prim("void"), "m/f/2/0": Prim("i64")}
        )

    def test_every_body_node_records_its_type(self):
        module = resolve_src(
            "(mod m)(fn f :i64 ((a :i64))"
            " (let b (* a 2))"
            " (letmut c :i64 b)"
            " (set c (+ c 1))"
            " (if (< c 10) (set c 10) (set c 20))"
            " (while (< a c) (set c (- c 1)))"
            " (ret c))"
        )
        checked = check_module(module)
        for item in module.fns["f"].items[4:]:
            assert isinstance(item, Node)
            for node in item.walk():
                self.assertIn(node.path, checked.expr_types)

    def test_a_literal_takes_the_type_it_is_checked_against(self):
        for src in (
            "(mod m)(fn f :i32 () (ret 0))",
            "(mod m)(fn f :u8 () (ret 1))",
            "(mod m)(fn f :f32 () (ret 1.5))",
            "(mod m)(fn f :f64 () (ret 1e3))",
            '(mod m)(fn f :str () (ret "s"))',
            "(mod m)(fn f :bool () (ret true))",
            "(mod m)(fn f :bool () (ret false))",
            "(mod m)(fn f :i64 () (let a 1) (let b 1.5) (ret a))",
        ):
            with self.subTest(src=src):
                check_src(src)

    def test_a_literal_does_not_become_a_bool(self):
        error = expect_error("(mod m)(fn f :bool () (ret 1))", "E0100")
        self.assertIn(":i64", error.message)

    def test_checking_is_pure_and_repeatable(self):
        module = resolve_src(
            "(mod m)(type p (struct))(fn f :i64 ((a :i64)) (ret (+ a 1)))"
        )
        before = [(node.path, node.kind) for node in module.root.walk()]
        first = check_module(module)
        second = check_module(module)
        self.assertEqual(first.expr_types, second.expr_types)
        self.assertEqual(first.fn_sigs, second.fn_sigs)
        self.assertEqual([(node.path, node.kind) for node in module.root.walk()], before)


class SignatureTest(unittest.TestCase):
    def test_an_unknown_parameter_type_is_E0101(self):
        error = expect_error("(mod m)(fn f :i64 ((a :wat)) (ret 0))", "E0101")
        self.assertEqual(error.path, "m/f/1/0")
        self.assertIn(":wat", error.message)

    def test_an_unknown_return_type_is_E0101(self):
        error = expect_error("(mod m)(fn f :wat () (ret 0))", "E0101")
        self.assertEqual(error.path, "m/f")

    def test_a_signature_is_checked_before_any_body(self):
        # The first function's body is wrong, the second's signature is wrong.
        # The signature wins, because every signature is checked first.
        error = expect_error(
            '(mod m)(fn f :i64 () (ret "s"))(fn g :i64 ((a :wat)) (ret 0))',
            "E0101",
        )
        self.assertEqual(error.path, "m/g/1/0")

    def test_parameters_are_immutable(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i64)) (set a 1) (ret a))", "E0104")
        self.assertEqual(error.path, "m/f/2")

    def test_a_void_parameter_is_rejected(self):
        error = expect_error("(mod m)(fn f :void ((a :void)) (println \"x\"))", "E0100")
        self.assertEqual(error.path, "m/f/1/0")
        self.assertEqual(error.message, "parameter 'a' cannot be :void")

    def test_a_repeated_parameter_is_a_duplicate(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i64) (a :i64)) (ret a))", "E0003")
        self.assertEqual(error.path, "m/f/1/1")

    def test_a_struct_and_an_enum_name_their_types(self):
        checked = check_src(
            "(mod m)"
            "(type point (struct (x :i64)))"
            "(type e (enum (a)))"
            "(fn f point ((p point) (x e)) (ret p))"
        )
        self.assertEqual(checked.types, {"point": StructRef("point"), "e": EnumRef("e")})
        self.assertEqual(
            checked.fn_sigs["f"],
            FnTy((StructRef("point"), EnumRef("e")), StructRef("point")),
        )

    def test_a_named_type_annotation_is_bare(self):
        check_src(
            "(mod m)(type p (struct))(fn f :i64 ((x p)) (let y p x) (ret 0))"
        )

    def test_a_composite_type_is_written_behind_the_type_marker(self):
        # ``:(vec :i64)`` is the language's spelling; the lexer splits the
        # marker into its own token, and both signature slots accept it.
        checked = check_src(
            "(mod m)(fn f :(vec :i64) ((a :(vec :i64)))"
            " (let b :(vec :i64) a) (ret b))"
        )
        self.assertEqual(
            checked.fn_sigs["f"],
            FnTy((Vec(Prim("i64")),), Vec(Prim("i64"))),
        )

    def test_a_type_definition_needs_a_struct_or_enum_body(self):
        error = expect_error("(mod m)(type p (wat))", "E0002")
        self.assertEqual(error.path, "m/p/0")

    def test_a_fn_needs_a_return_type_and_a_parameter_list(self):
        error = expect_error("(mod m)(fn f :i64 (ret 0))", "E0002")
        self.assertEqual(error.path, "m/f")

    def test_a_type_variable_in_a_signature_is_a_generic_parameter(self):
        # Task 10 replaces Task 4's refusal: a single capital letter in a
        # signature is a type parameter, and the body waits for a call to fix
        # it (see ``tests/test_generics.py`` for the substitution itself).
        checked = check_src("(mod m)(fn id :T ((a :T)) (ret a))")
        self.assertEqual(
            checked.fn_sigs["id"], FnTy((TypeVar("T"),), TypeVar("T"))
        )
        self.assertEqual(checked.generic, frozenset({"id"}))
        self.assertEqual(checked.instances, {})

    def test_a_generic_body_is_checked_per_instantiation(self):
        # ``(ret a)`` is only wrong in the copy where T is ``:str``: the
        # signature promises ``:i64``, so the copy returns the wrong type.
        error = expect_error(
            '(mod m)(fn id :i64 ((a T)) (ret a))(fn f :i64 () (ret (id "s")))',
            "E0100",
        )
        self.assertEqual(error.path, "m/id/2")
        self.assertEqual(error.message, "function 'id$str' returns :i64, found :str")

    def test_a_type_variable_in_a_body_is_unbound(self):
        error = expect_error(
            "(mod m)(fn f :i64 ((a :i64)) (let b T a) (ret b))", "E0501"
        )
        self.assertEqual(error.path, "m/f/2")
        self.assertIn("type variable T", error.message)

    def test_a_definition_name_may_not_start_with_a_colon(self):
        for src in (
            "(mod m)(fn :f :i64 () (ret 0))",
            "(mod m)(test :t)",
            "(mod m)(test t (fn :h :i64 () (ret 0)))",
            "(mod m)(type :p (struct))",
        ):
            with self.subTest(src=src):
                expect_error(src, "E0002")


class CallTest(unittest.TestCase):
    def test_a_call_takes_exactly_its_parameters(self):
        error = expect_error(
            "(mod m)(fn g :i64 ((a :i64))(ret a))(fn f :i64 ()(ret (g)))", "E0102"
        )
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "function 'g' takes 1 argument, got 0")

    def test_nested_calls_record_each_type(self):
        checked = check_src(
            "(mod m)(fn g :i64 ((a :i64)) (ret a))"
            "(fn f :i64 () (ret (g (g 1))))"
        )
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("i64"))
        self.assertEqual(checked.expr_types["m/f/2/0/0"], Prim("i64"))

    def test_an_unknown_callee_is_E0103(self):
        error = expect_error("(mod m)(fn f :i64 () (ret (nope 1)))", "E0103")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn("'nope'", error.message)

    def test_calling_a_value_is_E0100(self):
        error = expect_error("(mod m)(fn f :i64 () (let x 1) (ret (x 2)))", "E0100")
        self.assertEqual(error.path, "m/f/3/0")
        self.assertIn("not a function", error.message)
        self.assertIn(":i64", error.message)

    def test_no_implicit_widening(self):
        error = expect_error(WIDENING, "E0100")
        self.assertEqual(error.path, "m/f/2/0/0")
        self.assertIn(":i32", error.message)
        self.assertIn(":i64", error.message)

    def test_an_argument_atom_is_reported_at_the_call(self):
        error = expect_error(
            "(mod m)(fn g :i64 ((a :bool)) (ret 0))(fn f :i64 () (ret (g 1)))",
            "E0100",
        )
        self.assertEqual(error.path, "m/f/2/0")

    def test_a_call_may_target_a_later_definition(self):
        check_src("(mod m)(fn f :i64 () (ret (g 1)))(fn g :i64 ((a :i64)) (ret a))")

    def test_an_argument_literal_takes_the_parameter_type(self):
        check_src("(mod m)(fn g :i32 ((a :i32)) (ret a))(fn f :i32 () (ret (g 1)))")


class ArithmeticTest(unittest.TestCase):
    def test_arithmetic_returns_the_operand_type(self):
        checked = check_src("(mod m)(fn f :i64 ((a :i64) (b :i64)) (ret (- a b)))")
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("i64"))
        check_src("(mod m)(fn f :f64 ((a :f64)) (ret (/ a 2.0)))")
        check_src("(mod m)(fn f :i32 () (ret (+ 1 2)))")

    def test_arithmetic_refuses_to_mix_types(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i32) (b :i64)) (ret (+ a b)))", "E0100")
        self.assertEqual(error.message, "cannot add :i64 to :i32")
        self.assertEqual(error.path, "m/f/2/0")

    def test_modulo_is_arithmetic(self):
        checked = check_src("(mod m)(fn f :i64 () (ret (% 7 3)))")
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("i64"))

    def test_modulo_refuses_mixed_widths(self):
        error = expect_error(
            "(mod m)(fn f :i64 ((a :i64) (b :i32)) (ret (% a b)))", "E0100"
        )
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn("modulo", error.message)

    def test_a_leading_literal_takes_the_variable_type(self):
        checked = check_src("(mod m)(fn f :u8 ((a :u8)) (let b (+ 1 a)) (ret b))")
        self.assertEqual(checked.expr_types["m/f/2/1"], Prim("u8"))
        self.assertEqual(checked.expr_types["m/f/2"], Prim("void"))

    def test_a_leading_literal_takes_a_matching_integer_type(self):
        checked = check_src("(mod m)(fn f :u8 ((a :u8)) (ret (+ 1 a)))")
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("u8"))

    def test_a_leading_float_literal_may_not_enter_integer_arithmetic(self):
        error = expect_error("(mod m)(fn f :u8 ((a :u8)) (ret (+ 1.5 a)))", "E0100")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "cannot add :f64 to :u8")

    def test_a_leading_integer_literal_may_not_enter_float_arithmetic(self):
        error = expect_error("(mod m)(fn f :f64 ((a :f64)) (ret (+ 1 a)))", "E0100")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "cannot add :i64 to :f64")

    def test_literals_alone_default_to_i64(self):
        checked = check_src("(mod m)(fn f :i64 () (let c (+ 1 2)) (ret c))")
        self.assertEqual(checked.expr_types["m/f/2/1"], Prim("i64"))

    def test_arithmetic_needs_numbers(self):
        error = expect_error("(mod m)(fn f :i64 ((a :str)) (ret (+ a a)))", "E0100")
        self.assertIn("needs numbers", error.message)
        self.assertIn(":str", error.message)

    def test_arithmetic_takes_at_least_two_operands(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i64)) (ret (+ a)))", "E0102")
        self.assertEqual(error.path, "m/f/2/0")

    def test_a_return_does_not_widen_an_expression(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i32)) (ret (+ a 1)))", "E0100")
        self.assertEqual(error.path, "m/f/2")
        self.assertIn(":i32", error.message)
        self.assertIn(":i64", error.message)


class ComparisonTest(unittest.TestCase):
    def test_a_comparison_yields_bool(self):
        checked = check_src(
            "(mod m)(fn f :bool ((a :i64) (b :i64)) (ret (< a b)))"
        )
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("bool"))
        # ``:str`` equality is accepted since Task 9: it is byte equality, and
        # the lowerer emits the comparison that means it.
        strings = check_src(
            '(mod m)(fn f :bool ((a :str) (b :str)) (ret (= a b)))'
        )
        self.assertEqual(strings.expr_types["m/f/2/0"], Prim("bool"))

    def test_a_comparison_needs_matching_types(self):
        error = expect_error(
            "(mod m)(fn f :bool ((a :i32) (b :i64)) (ret (< a b)))", "E0100"
        )
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn("cannot compare", error.message)

    def test_a_leading_literal_takes_the_variable_type(self):
        checked = check_src("(mod m)(fn f :bool ((a :u8)) (ret (< 1 a)))")
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("bool"))

    def test_equality_is_scalar_only(self):
        error = expect_error(
            "(mod m)(fn f :bool () (letmut a :i64 1)"
            " (ret (= (set a 1) (set a 2))))",
            "E0100",
        )
        self.assertEqual(error.path, "m/f/3/0/0")
        self.assertIn("scalar", error.message)
        self.assertIn(":void", error.message)

    def test_equality_rejects_named_types(self):
        error = expect_error(
            "(mod m)(type p (struct))"
            "(fn f :bool ((a p) (b p)) (ret (= a b)))",
            "E0100",
        )
        self.assertIn("scalar", error.message)

    def test_ordering_needs_numbers(self):
        error = expect_error(
            "(mod m)(fn f :bool ((a :bool) (b :bool)) (ret (< a b)))", "E0100"
        )
        self.assertIn("needs numbers", error.message)

    def test_a_comparison_takes_two_operands(self):
        error = expect_error("(mod m)(fn f :bool ((a :i64)) (ret (< a)))", "E0102")
        self.assertEqual(error.path, "m/f/2/0")


class ControlFlowTest(unittest.TestCase):
    def test_if_requires_a_bool_condition(self):
        error = expect_error("(mod m)(fn f :i64 ((a :i64)) (ret (if a 1 2)))", "E0100")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn(":bool", error.message)

    def test_if_branches_must_agree(self):
        error = expect_error(
            '(mod m)(fn f :i64 ((a :bool)) (ret (if a 1 "s")))', "E0100"
        )
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn("branches", error.message)

    def test_an_if_branch_takes_the_expected_type(self):
        checked = check_src("(mod m)(fn f :i32 ((a :bool)) (ret (if a 0 1)))")
        self.assertEqual(checked.expr_types["m/f/2/0"], Prim("i32"))

    def test_if_takes_three_operands(self):
        error = expect_error("(mod m)(fn f :i64 ((a :bool)) (ret (if a 1)))", "E0102")
        self.assertEqual(error.path, "m/f/2/0")

    def test_while_needs_a_bool_condition(self):
        error = expect_error("(mod m)(fn f :i64 () (while 1 (ret 0)) (ret 0))", "E0100")
        self.assertEqual(error.path, "m/f/2")

    def test_while_checks_its_body_and_is_void(self):
        checked = check_src(
            "(mod m)(fn f :i64 () (letmut i :i64 0)"
            " (while (< i 10) (set i (+ i 1))) (ret i))"
        )
        self.assertEqual(checked.expr_types["m/f/3"], Prim("void"))
        self.assertEqual(checked.expr_types["m/f/3/1"], Prim("void"))

    def test_a_while_body_must_be_forms(self):
        error = expect_error("(mod m)(fn f :i64 () (while true 1) (ret 0))", "E0002")
        self.assertEqual(error.path, "m/f/2")


class BindingTest(unittest.TestCase):
    def test_let_infers_and_letmut_allows_set(self):
        checked = check_src(
            "(mod m)(fn f :i64 () (let a 1) (letmut b :i64 a) (set b 2) (ret b))"
        )
        self.assertEqual(checked.expr_types["m/f/2"], Prim("void"))
        self.assertEqual(checked.expr_types["m/f/4"], Prim("void"))

    def test_set_requires_a_mutable_binding(self):
        error = expect_error(
            "(mod m)(fn f :i64 () (let a 1) (set a 2) (ret a))", "E0104"
        )
        self.assertEqual(error.path, "m/f/3")
        self.assertIn("immutable", error.message)

    def test_set_checks_the_value_type(self):
        error = expect_error(
            '(mod m)(fn f :i64 () (letmut a :i64 1) (set a "s") (ret a))', "E0100"
        )
        self.assertEqual(error.path, "m/f/3")
        self.assertIn("cannot set 'a'", error.message)

    def test_a_binding_annotation_is_checked(self):
        check_src("(mod m)(fn f :i64 ((x :i64)) (let y :i64 x) (ret y))")
        error = expect_error("(mod m)(fn f :i64 () (let y :i64 1.0) (ret y))", "E0100")
        self.assertEqual(error.path, "m/f/2")
        self.assertIn(":f64", error.message)

    def test_a_void_value_cannot_be_bound(self):
        # ``print``, ``set``, ``while`` and a void ``if`` are statements: a
        # binding of one has nothing to store and no type to allocate.
        for source in (
            '(mod m)(fn f :void () (let x (println "a")))',
            "(mod m)(fn f :void () (letmut x :i64 1) (let y (set x 2)))",
            "(mod m)(fn f :void () (let y (while true (println \"a\"))))",
        ):
            with self.subTest(source=source):
                error = expect_error(source, "E0100")
                self.assertIn("cannot bind", error.message)

    def test_a_second_binding_of_one_name_is_a_duplicate(self):
        error = expect_error("(mod m)(fn f :i64 () (let a 1) (let a 2) (ret a))", "E0003")
        self.assertEqual(error.path, "m/f/3")

    def test_setting_an_unknown_name_is_name_not_found(self):
        error = expect_error("(mod m)(fn f :i64 () (set q 1) (ret 0))", "E0005")
        self.assertEqual(error.path, "m/f/2")

    def test_let_without_a_value_is_wrong_arity(self):
        for src in (
            "(mod m)(fn f :i64 () (let a) (ret 0))",
            "(mod m)(fn f :i64 () (let a :i64) (ret 0))",
            "(mod m)(fn f :i64 () (let a :(vec :i64)) (ret 0))",
        ):
            with self.subTest(src=src):
                error = expect_error(src, "E0102")
                self.assertEqual(error.path, "m/f/2")


class ReturnTest(unittest.TestCase):
    def test_ret_checks_the_declared_type(self):
        error = expect_error('(mod m)(fn f :i64 () (ret "s"))', "E0100")
        self.assertEqual(error.path, "m/f/2")
        self.assertEqual(error.message, "function 'f' returns :i64, found :str")

    def test_a_void_function_returns_no_value(self):
        check_src("(mod m)(fn f :void () (ret))")
        error = expect_error("(mod m)(fn f :void () (ret 0))", "E0100")
        self.assertEqual(error.path, "m/f/2")

    def test_ret_takes_at_most_one_value(self):
        error = expect_error("(mod m)(fn f :i64 () (ret 1 2))", "E0102")
        self.assertEqual(error.path, "m/f/2")

    def test_a_non_void_function_must_return(self):
        error = expect_error("(mod m)(fn f :i64 ())", "E0105")
        self.assertEqual(error.path, "m/f")
        self.assertIn(":i64", error.message)

    def test_a_void_function_needs_no_return(self):
        check_src("(mod m)(fn f :void ())")

    def test_a_return_inside_a_branch_counts(self):
        check_src("(mod m)(fn f :i64 ((a :bool)) (if a (ret 1) (ret 2)))")

    def test_a_conditional_return_needs_both_arms(self):
        # Presence is not termination: the else arm falls off the end, and
        # nothing gives the caller a value on that path.
        error = expect_error(
            '(mod m)(fn f :i64 ((a :bool)) (if a (ret 1) (println "x")))',
            "E0105",
        )
        self.assertEqual(error.path, "m/f")
        self.assertIn("falls off the end", error.message)

    def test_a_match_must_return_from_every_arm(self):
        error = expect_error(
            '(mod m)(fn f :i64 ((n :i64))'
            ' (match n (0 (ret 1)) (_ (println "x"))))',
            "E0105",
        )
        self.assertEqual(error.path, "m/f")

    def test_a_match_whose_arms_all_return_satisfies_e0105(self):
        check_src(
            "(mod m)(fn f :i64 ((n :i64))"
            " (match n (0 (ret 1)) (1 (ret 2)) (_ (ret 3))))"
        )

    def test_a_constant_true_loop_never_reaches_the_end(self):
        check_src(
            '(mod m)(fn spin :i64 () (while true (println "x")))'
        )

    def test_a_constant_true_loop_with_a_break_still_falls_through(self):
        error = expect_error(
            "(mod m)(fn f :i64 () (while true (break)))", "E0105"
        )
        self.assertEqual(error.path, "m/f")

    def test_an_i64_main_must_return_on_every_path(self):
        # main is the C entry point, so a fall-through used to exit 0 with a
        # value nobody wrote.
        error = expect_error(
            '(mod m)(fn main :i64 ((a :bool)) (if a (ret 1) (println "x")))',
            "E0105",
        )
        self.assertIn("function 'main'", error.message)
        check_src("(mod m)(fn main :void () (println \"x\"))")


class ImportTest(unittest.TestCase):
    def test_imported_fns_and_types_are_visible(self):
        # Visible, though not callable: a call to an imported function is
        # E0103 because this module does not lower it (test_emit covers that).
        checked = check_src(
            "(mod a)(use b)(fn f :i64 ((x :i64)) (ret x))",
            {"b": "(mod b)(type p (struct))(fn g :i64 ((x :i64)) (ret x))"},
        )
        self.assertIn("g", checked.fn_sigs)
        self.assertIn("p", checked.types)

    def test_an_imported_type_can_annotate_a_local_signature(self):
        checked = check_src(
            "(mod a)(use b)(fn f :i64 ((p point)) (ret 0))",
            {"b": "(mod b)(type point (struct (x :i64)))"},
        )
        self.assertEqual(
            checked.fn_sigs["f"], FnTy((StructRef("point"),), Prim("i64"))
        )

    def test_imports_are_not_transitive(self):
        error = expect_error(
            "(mod a)(use b)(fn f :i64 () (ret (h 1)))",
            "E0103",
            {"b": "(mod b)(use c)", "c": "(mod c)(fn h :i64 ((x :i64)) (ret x))"},
        )
        self.assertEqual(error.path, "a/f/2/0")

    def test_an_imported_signature_is_checked(self):
        error = expect_error(
            "(mod a)(use b)",
            "E0101",
            {"b": "(mod b)(fn g :i64 ((x :wat)) (ret x))"},
        )
        self.assertEqual(error.path, "b/g/1/0")


class TestNodeTest(unittest.TestCase):
    def test_a_test_body_is_checked(self):
        error = expect_error(
            "(mod m)(test t (let x :i64 0) (set x 1) (expect (code 0)))", "E0104"
        )
        self.assertEqual(error.path, "m/test/t/1")

    def test_expect_nodes_are_directives_not_expressions(self):
        checked = check_src('(mod m)(test t (expect (out "x") (code 0)) (let x :i64 1))')
        self.assertNotIn("m/test/t/0", checked.expr_types)
        self.assertEqual(checked.expr_types["m/test/t/1"], Prim("void"))

    def test_a_helper_fn_inside_a_test_is_refused(self):
        # No definition of a helper is emitted, so a call could never link.
        # Refused at the node, before any call is checked.
        error = expect_error(
            "(mod m)(test t (fn twice :i64 ((x :i64)) (ret (* x 2)))"
            " (let y :i64 (twice 2)))",
            "E0002",
        )
        self.assertEqual(
            error.message, "test-local helper functions are not lowered yet"
        )
        self.assertEqual(error.path, "m/test/t/0")

    def test_a_test_returns_no_value(self):
        error = expect_error("(mod m)(test t (ret 1))", "E0100")
        self.assertEqual(error.path, "m/test/t/0")
        self.assertIn("test 't'", error.message)

    def test_a_helper_fn_is_refused_even_when_nothing_calls_it(self):
        # The refusal is structural: the node has no lowered definition at all,
        # so it does not matter whether anything would call it.
        error = expect_error(
            "(mod m)(test t (fn helper :i64 () (ret 0)))",
            "E0002",
        )
        self.assertEqual(error.path, "m/test/t/0")


class UnknownFormTest(unittest.TestCase):
    def test_an_empty_form_is_unknown(self):
        error = expect_error("(mod m)(fn f :i64 () () (ret 0))", "E0002")
        self.assertEqual(error.path, "m/f/2")

    def test_a_bare_atom_is_not_a_statement(self):
        error = expect_error("(mod m)(fn f :i64 () 5 (ret 0))", "E0002")
        self.assertEqual(error.path, "m/f")

    def test_spawn_of_a_name_that_is_not_a_function_is_e0103(self):
        # Every language form now has a rule; ``spawn`` names a function, and
        # a name that is not one is the ordinary unknown-function diagnostic
        # at the spawn node.
        error = expect_error(
            "(mod m)(fn f :i64 ((p :i32)) (ret (spawn p)))", "E0103"
        )
        self.assertEqual(error.path, "m/f/2/0")
        self.assertIn("'p'", error.message)


class CorpusTest(unittest.TestCase):
    def test_every_case_matches_its_expect_file(self):
        cases = sorted(TYPE.glob("*.logos"))
        self.assertTrue(cases, "tests/type holds the negative cases")
        for case in cases:
            expected = case.with_suffix(".expect").read_text(encoding="utf-8").strip()
            with self.subTest(case=case.name):
                source = case.read_text(encoding="utf-8")
                with self.assertRaises(LogosError) as caught:
                    check_src(source)
                error = caught.exception
                self.assertEqual(error.code, expected, error.render())
                self.assertTrue(error.path, "every type error names a node")


if __name__ == "__main__":
    unittest.main()
