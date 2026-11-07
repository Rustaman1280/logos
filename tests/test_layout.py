"""Structs and fixed arrays: C layout, construction, access, mutation, bounds.

Three layers, one class each: the LLVM text a struct or an array is given, what
the checker accepts and rejects, and what a real binary prints at ``-O0`` and
``-O2``. The layout is pinned against C's rules — declaration order, natural
alignment, no reordering — because ``%struct.pair = type { i8, i64 }`` is the
same 16-byte aggregate a C compiler gives the equivalent struct, and a field
read is only correct if the field offsets are.
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
from logos.types import Arr, Prim, StructRef

ROOT = Path(__file__).resolve().parents[1]
STRUCTS = ROOT / "tests" / "exec" / "structs.logos"

#: One module that defines a padded struct and a struct that nests it.
PAIR = (
    "(mod m)"
    "(type pair (struct (a :u8) (b :i64)))"
    "(type line (struct (from pair) (n :i64)))"
    '(fn main :void () (let p pair (make pair 1 2)) (let l line (make line p 3))'
    ' (println "x"))'
)


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def emitted(source: str) -> str:
    return llvm.emit(lower.lower_module(checked(source)))


def emitted_type(name: str) -> str:
    """The emitted definition line for struct ``name`` in :data:`PAIR`'s module."""
    for line in emitted(PAIR).splitlines():
        if line.startswith(f"%struct.{name} = type"):
            return line
    raise AssertionError(f"no emitted type for struct '{name}'")


def path_of(cm: CheckedModule, kind: str) -> str:
    for node in cm.module.root.walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in the module")


def ops(fn: ir.IrFn) -> list[str]:
    return [instr[0] for block in fn.blocks for instr in block.instrs]


def instructions(fn: ir.IrFn, opcode: str) -> list[tuple]:
    return [
        instr for block in fn.blocks for instr in block.instrs if instr[0] == opcode
    ]


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


class LayoutTest(unittest.TestCase):
    """The struct definition line is C's layout, spelled as LLVM's."""

    def test_struct_layout_matches_c(self):
        # (type pair (struct (a :u8) (b :i64)))
        self.assertEqual(emitted_type("pair"), "%struct.pair = type { i8, i64 }")

    def test_fields_keep_declaration_order(self):
        text = emitted(
            "(mod m)(type odd (struct (b :i64) (a :u8)))"
            "(fn main :void () (let o odd (make odd 1 2)))"
        )
        self.assertIn("%struct.odd = type { i64, i8 }", text)

    def test_a_nested_struct_names_its_field_type(self):
        self.assertEqual(emitted_type("line"), "%struct.line = type { %struct.pair, i64 }")

    def test_definitions_come_before_the_functions_that_use_them(self):
        text = emitted(PAIR)
        self.assertLess(text.index("%struct.pair ="), text.index("define"))
        self.assertLess(text.index("%struct.line ="), text.index("define"))

    def test_struct_llty_is_readable_after_lowering(self):
        lower.lower_module(checked(PAIR))
        self.assertEqual(llvm.struct_llty("pair"), "%struct.pair = type { i8, i64 }")

    def test_struct_llty_describes_the_module_just_lowered(self):
        """A reused struct name answers for the module processed last."""
        first = (
            "(mod a)(type pair (struct (a :i64)))"
            '(fn main :void () (let p pair (make pair 1)) (println "x"))'
        )
        second = (
            "(mod b)(type pair (struct (a :u8) (b :i64)))"
            '(fn main :void () (let p pair (make pair 1 2)) (println "x"))'
        )
        lower.lower_module(checked(first))
        self.assertEqual(llvm.struct_llty("pair"), "%struct.pair = type { i64 }")
        lower.lower_module(checked(second))
        self.assertEqual(llvm.struct_llty("pair"), "%struct.pair = type { i8, i64 }")

    def test_struct_llty_describes_the_module_just_emitted(self):
        first = (
            "(mod a)(type pair (struct (a :i64)))"
            '(fn main :void () (let p pair (make pair 1)) (println "x"))'
        )
        second = (
            "(mod b)(type pair (struct (a :u8) (b :i64)))"
            '(fn main :void () (let p pair (make pair 1 2)) (println "x"))'
        )
        emitted(first)
        self.assertEqual(llvm.struct_llty("pair"), "%struct.pair = type { i64 }")
        emitted(second)
        self.assertEqual(llvm.struct_llty("pair"), "%struct.pair = type { i8, i64 }")

    def test_the_layout_is_the_one_clang_gives_the_c_twin(self):
        """Same fields, same order, same padding: the definitions must agree."""
        source = (
            "struct pair { unsigned char a; long long b; };\n"
            "struct line { struct pair from; long long n; };\n"
            "struct line l;\n"
            "void *f(void) { return &l.from.b; }\n"
        )
        done = subprocess.run(
            ["clang", "-S", "-emit-llvm", "-O0", "-x", "c", "-", "-o", "-"],
            input=source,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            self.skipTest("clang is not available")
        twin = {
            line.split(" = ")[0]: line
            for line in done.stdout.splitlines()
            if line.startswith("%struct.")
        }
        ours = {
            line.split(" = ")[0]: line
            for line in emitted(PAIR).splitlines()
            if line.startswith("%struct.")
        }
        self.assertEqual(ours["%struct.pair"], twin["%struct.pair"])
        self.assertEqual(ours["%struct.line"], twin["%struct.line"])

    def test_a_fixed_array_is_an_llvm_array(self):
        text = emitted(
            "(mod m)(fn main :void ()"
            " (let a (arr :i64 3) (make (arr :i64 3) 1 2 3)))"
        )
        self.assertIn("%v.0.addr = alloca [3 x i64]", text)
        self.assertIn("insertvalue [3 x i64] undef", text)


class StructDefinitionTest(unittest.TestCase):
    """Every struct a signature names is defined, however the type reaches it.

    A pointer, an array of pointers and a field that names an otherwise unused
    struct all spell ``%struct.X`` somewhere; clang rejects a module that uses
    an identified type it never defines, so collection has to descend through
    ``Ptr`` as well as ``StructRef`` and ``Arr``.
    """

    def test_a_struct_reached_through_a_pointer_parameter_is_defined(self):
        text = emitted(
            "(mod m)(type point (struct (x :i64)))"
            '(fn handle :void ((p (ptr point))) (println "h"))'
            '(fn main :void () (println "m"))'
        )
        self.assertIn("%struct.point = type { i64 }", text)
        self.assertIn("define internal void @lg.m.handle(%struct.point* %v.0)", text)

    def test_a_struct_inside_an_array_of_pointers_is_defined(self):
        text = emitted(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :void ((a (arr (ptr point) 2))) (ret))"
        )
        self.assertIn("%struct.point = type { i64 }", text)
        self.assertIn("[2 x %struct.point*] %v.0", text)

    def test_a_struct_named_only_by_an_unused_field_is_defined(self):
        text = emitted(
            "(mod m)(type point (struct (x :i64)))"
            "(type wrap (struct (head (ptr point))))"
            "(fn f :void ((w wrap)) (ret))"
        )
        self.assertIn("%struct.point = type { i64 }", text)
        self.assertIn("%struct.wrap = type { %struct.point* }", text)

    def test_a_struct_pointing_at_itself_is_defined_once(self):
        """A pointer cycle is finite, and collection must not follow it."""
        text = emitted(
            "(mod m)(type node (struct (v :i64) (head (ptr node))))"
            "(fn f :void ((n node)) (ret))"
        )
        self.assertEqual(text.count("%struct.node = type"), 1)
        self.assertIn("%struct.node = type { i64, %struct.node* }", text)


class StructCheckTest(unittest.TestCase):
    """Construction and field access, and the two new field codes."""

    def test_make_records_the_struct_type(self):
        cm = checked(
            "(mod m)(type point (struct (x :i64) (y :i64)))"
            "(fn f :void () (let p point (make point 1 2)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "make")], StructRef("point"))

    def test_get_records_the_field_type(self):
        cm = checked(
            "(mod m)(type point (struct (x :i64) (y :u8)))"
            "(fn f :void ((p point)) (let y :u8 (get p y)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "get")], Prim("u8"))

    def test_an_unknown_field_is_e0200(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :i64 ((p point)) (ret (get p z)))"
        )
        self.assertEqual(error.code, "E0200")
        self.assertEqual(error.path, "m/f/2/0")
        self.assertEqual(error.message, "struct 'point' has no field 'z'")

    def test_an_unknown_field_on_set_field_is_e0200(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :void () (letmut p point (make point 1)) (set-field p z 2))"
        )
        self.assertEqual(error.code, "E0200")
        self.assertEqual(error.path, "m/f/3")
        self.assertEqual(error.message, "struct 'point' has no field 'z'")

    def test_make_takes_one_value_per_field(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64) (y :i64)))"
            "(fn f :void () (let p point (make point 1)))"
        )
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "struct 'point' takes 2 fields, got 1")

    def test_a_field_argument_of_the_wrong_type_is_e0201(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            '(fn f :void () (let p point (make point "s")))'
        )
        self.assertEqual(error.code, "E0201")
        self.assertEqual(error.message, "field 'x' of 'point' expects :i64, found :str")

    def test_set_field_checks_the_value_against_the_field(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            '(fn f :void () (letmut p point (make point 1)) (set-field p x "s"))'
        )
        self.assertEqual(error.code, "E0201")
        self.assertEqual(error.message, "field 'x' of 'point' expects :i64, found :str")

    def test_set_field_needs_a_mutable_binding(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :void () (let p point (make point 1)) (set-field p x 2))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertEqual(error.message, "cannot set immutable binding 'p'")

    def test_set_field_takes_a_name(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64)))"
            "(fn f :void ((p point)) (set-field (make point 1) x 2))"
        )
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "set-field takes a mutable name, a field and a value")

    def test_get_needs_a_struct(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (get n x)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'get' needs a struct, found :i64")

    def test_make_needs_a_struct_or_an_array(self):
        error = fail_on("(mod m)(fn f :i64 () (ret (make :i64 1)))")
        self.assertEqual(error.code, "E0101")
        self.assertEqual(error.message, "make needs a struct or array type, found :i64")

    def test_make_of_an_enum_is_refused_until_results_exist(self):
        error = fail_on(
            "(mod m)(type e (enum (a)))"
            "(fn f :void () (let x e (make e)))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "'make' of enum 'e' is not lowered yet")

    def test_a_duplicate_field_is_e0003(self):
        error = fail_on(
            "(mod m)(type point (struct (x :i64) (x :u8)))"
            "(fn f :void ())"
        )
        self.assertEqual(error.code, "E0003")
        self.assertEqual(error.path, "m/point/0/1")
        self.assertEqual(error.message, "duplicate field 'x'")

    def test_a_malformed_field_is_e0002(self):
        error = fail_on("(mod m)(type point (struct (x)))")
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/point/0/0")
        self.assertEqual(error.message, "a struct field must be (name type)")

    def test_an_imported_struct_field_is_visible(self):
        cm = check_module(
            resolve(
                parse("(mod a)(use b)(fn f :i64 ((p point)) (ret (get p x)))", "a.logos"),
                lambda name: parse("(mod b)(type point (struct (x :i64)))", "b.logos"),
            )
        )
        self.assertEqual(cm.expr_types[path_of(cm, "get")], Prim("i64"))


class ArrayCheckTest(unittest.TestCase):
    """``arr-get``/``arr-set`` and array construction."""

    def test_make_builds_a_fixed_array(self):
        cm = checked(
            "(mod m)(fn f :void ()"
            " (let a (arr :i64 3) (make (arr :i64 3) 1 2 3)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "make")], Arr(Prim("i64"), 3))

    def test_arr_get_reads_the_element_type(self):
        cm = checked(
            "(mod m)(fn f :u8 ()"
            " (let a (arr :u8 2) (make (arr :u8 2) 1 2)) (ret (arr-get a 1)))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "arr-get")], Prim("u8"))

    def test_arr_get_needs_an_array(self):
        error = fail_on("(mod m)(fn f :i64 ((n :i64)) (ret (arr-get n 0)))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "'arr-get' needs a fixed array, found :i64")

    def test_an_array_index_must_be_an_integer(self):
        error = fail_on(
            "(mod m)(fn f :i64 ()"
            ' (let a (arr :i64 2) (make (arr :i64 2) 1 2)) (ret (arr-get a "x")))'
        )
        self.assertEqual(error.code, "E0400")
        self.assertEqual(error.message, "array index must be an integer, found :str")

    def test_arr_set_needs_a_mutable_binding(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            " (let a (arr :i64 2) (make (arr :i64 2) 1 2)) (arr-set a 0 9))"
        )
        self.assertEqual(error.code, "E0104")
        self.assertEqual(error.message, "cannot set immutable binding 'a'")

    def test_arr_set_checks_the_value_against_the_element(self):
        error = fail_on(
            "(mod m)(fn f :void ()"
            ' (letmut a (arr :i64 2) (make (arr :i64 2) 1 2)) (arr-set a 0 "s"))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "cannot store :str in (arr :i64 2)")

    def test_make_takes_one_value_per_element(self):
        error = fail_on(
            "(mod m)(fn f :void () (let a (arr :i64 3) (make (arr :i64 3) 1 2)))"
        )
        self.assertEqual(error.code, "E0102")
        self.assertEqual(error.message, "array '(arr :i64 3)' takes 3 elements, got 2")

    def test_an_array_element_of_the_wrong_type_is_e0100(self):
        error = fail_on(
            '(mod m)(fn f :void () (let a (arr :i64 2) (make (arr :i64 2) 1 "s")))'
        )
        self.assertEqual(error.code, "E0100")
        self.assertIn("cannot store :str", error.message)


class StructLoweringTest(unittest.TestCase):
    """The aggregate is built, read and mutated field by field."""

    def test_make_inserts_each_field_in_declaration_order(self):
        fn = lower.lower_module(
            checked(
                "(mod m)(type point (struct (x :i64) (y :u8)))"
                "(fn f :void () (let p point (make point 1 2)))"
            )
        ).functions[0]
        inserts = instructions(fn, "insertvalue")
        self.assertEqual([instr[6] for instr in inserts], [0, 1])
        self.assertEqual([instr[4] for instr in inserts], [Prim("i64"), Prim("u8")])
        self.assertEqual(inserts[0][2], StructRef("point"))

    def test_get_extracts_the_field_at_its_index(self):
        fn = lower.lower_module(
            checked(
                "(mod m)(type point (struct (x :i64) (y :u8)))"
                "(fn f :u8 ((p point)) (ret (get p y)))"
            )
        ).functions[0]
        extracts = instructions(fn, "extractvalue")
        self.assertEqual(extracts[-1][5], 1)
        self.assertEqual(extracts[-1][3], Prim("u8"))

    def test_set_field_stores_through_a_gep(self):
        text = emitted(
            "(mod m)(type point (struct (x :i64) (y :i64)))"
            "(fn f :void () (letmut p point (make point 1 2)) (set-field p y 5))"
        )
        self.assertIn(
            "= getelementptr %struct.point, %struct.point* %v.0.addr, i32 0, i32 1",
            text,
        )
        self.assertIn("store i64 5, i64* %t", text)

    def test_a_shared_scalar_field_is_e0002_at_the_declaration(self):
        # ``(vec T)``, ``(opt T)`` and ``(shared T)`` are lowerable now; only
        # ``(shared T)`` around a scalar is meaningless, and it is refused
        # where the field's type is spelled.
        error = fail_on_lower(
            "(mod m)(type box (struct (v (shared :i64))))"
            "(fn f :void ((b box)) (ret))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message,
            "only a pointer, a mutex, a channel or an atomic can be shared",
        )
        self.assertEqual(error.path, "m/box/0/0/0")

    def test_a_void_field_is_e0002_at_the_use(self):
        error = fail_on_lower(
            "(mod m)(type box (struct (v :void)))"
            "(fn f :void ((b box)) (ret))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, ":void is not lowered yet")

    def test_an_empty_struct_is_an_empty_aggregate(self):
        text = emitted(
            "(mod m)(type e (struct))"
            "(fn main :void () (let x e (make e)) (println \"x\"))"
        )
        self.assertIn("%struct.e = type { }", text)
        self.assertIn("store %struct.e undef, %struct.e* %v.0.addr", text)

    def test_an_enum_is_a_tag_and_a_payload_area(self):
        # Task 11 gives an enum a layout: one i32 tag and an area sized for
        # its widest variant, so a value of any variant is one LLVM type.
        text = emitted(
            "(mod m)(type e (enum (a :i64) (b)))"
            "(fn f :void ((x e)) (ret))"
        )
        self.assertIn("define internal void @lg.m.f({ i32, [1 x i64] } %v.0) {", text)
        self.assertIn("{ i32, [1 x i64] }* %v.0.addr", text)

    def test_a_struct_that_contains_itself_is_refused(self):
        error = fail_on_lower(
            "(mod m)(type node (struct (next node)))"
            "(fn f :void ((n node)) (ret))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.message, "struct 'node' contains itself by value")

    def test_a_struct_value_survives_a_function_call(self):
        text = emitted(
            "(mod m)(type point (struct (x :i64)))"
            "(fn id point ((p point)) (ret p))"
            "(fn main :void () (let p point (make point 1)) (let q point (id p))"
            ' (println "x"))'
        )
        self.assertIn("define internal %struct.point @lg.m.id(%struct.point %v.0)", text)
        self.assertIn("call %struct.point @lg.m.id(%struct.point %t", text)


class ArrayLoweringTest(unittest.TestCase):
    """Bounds checks, then a typed load or store through a gep."""

    def test_arr_get_checks_the_index_before_reading(self):
        m = lower.lower_module(
            checked(
                "(mod m)(fn f :i64 ()"
                " (let a (arr :i64 3) (make (arr :i64 3) 1 2 3)) (ret (arr-get a 1)))"
            )
        )
        fn = m.functions[0]
        self.assertTrue(
            any(instr[2] == "ult" and instr[5] == "3" for instr in instructions(fn, "icmp"))
        )
        self.assertIn("rt_panic", [c[2] for c in instructions(fn, "call")])
        self.assertTrue(any(g.data == b"R0003\x00" for g in m.globals))
        self.assertEqual(ops(fn)[-2:], ["gep", "load"])

    def test_arr_get_widens_a_narrow_index_before_the_unsigned_test(self):
        fn = lower.lower_module(
            checked(
                "(mod m)(type point (struct (n :i32)))"
                "(fn f :i64 ((p point))"
                " (let a (arr :i64 3) (make (arr :i64 3) 1 2 3))"
                " (ret (arr-get a (get p n))))"
            )
        ).functions[0]
        self.assertEqual(instructions(fn, "sext")[-1][3], Prim("i64"))

    def test_arr_set_stores_the_element_through_a_gep(self):
        text = emitted(
            "(mod m)(fn f :void ((i :i64))"
            " (letmut a (arr :i64 3) (make (arr :i64 3) 0 0 0))"
            " (arr-set a i 9))"
        )
        self.assertIn("= getelementptr [3 x i64], [3 x i64]* %v.1.addr, i64 0, i64 %t", text)
        self.assertIn("store i64 9, i64* %t", text)

    def test_a_temporary_array_is_spilled_to_a_slot_before_indexing(self):
        text = emitted(
            "(mod m)(fn f :i64 () (ret (arr-get (make (arr :i64 2) 4 5) 1)))"
        )
        self.assertIn("= alloca [2 x i64]", text)
        self.assertIn("store [2 x i64] %t", text)

    def test_a_zero_length_array_is_constructed_with_no_inserts(self):
        text = emitted(
            "(mod m)(fn main :void () (let a (arr :i64 0) (make (arr :i64 0))))"
        )
        self.assertIn("%v.0.addr = alloca [0 x i64]", text)
        self.assertIn("store [0 x i64] undef, [0 x i64]* %v.0.addr", text)
        self.assertNotIn("insertvalue", text)

    def test_an_index_with_a_side_effect_is_lowered_once(self):
        text = emitted(
            "(mod m)(fn loud :i64 ((i :i64)) (println \"idx\") (ret i))"
            "(fn f :i64 () (let a (arr :i64 3) (make (arr :i64 3) 1 2 3))"
            " (ret (arr-get a (loud 1))))"
        )
        self.assertEqual(text.count("call i64 @lg.m.loud"), 1)


class StructExecTest(unittest.TestCase):
    """Real binaries at both optimisation levels, output and status compared."""

    def check(self, test: str, out: str, code: int) -> None:
        binary = str(ROOT / "build" / f"layout-{test}")
        for opt in ("-O0", "-O2"):
            with self.subTest(test=test, opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(STRUCTS), test, target, opt=opt)
                done = subprocess.run([target], capture_output=True, text=True)
                self.assertEqual(done.stdout, out, done.stderr)
                self.assertEqual(done.returncode, code, done.stderr)

    def test_make_and_get(self):
        self.check("make-and-get", "one\ntwo\n", 0)

    def test_a_nested_struct(self):
        self.check("nested-struct", "cell\none\n", 0)

    def test_set_field_mutates_in_place(self):
        self.check("set-field", "two\n", 0)

    def test_arrays_read_and_write(self):
        self.check("array-get-and-set", "two\nnine\n", 0)

    def test_an_array_inside_a_struct(self):
        self.check("array-in-struct", "three\n", 0)

    def test_an_index_past_the_end_panics_with_r0003(self):
        binary = str(ROOT / "build" / "layout-bounds")
        case = "array-bounds-panic"
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(STRUCTS), case, target, opt=opt)
                done = subprocess.run([target], capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn("R0003: array index out of bounds", done.stderr)

    def test_a_negative_index_panics_too(self):
        binary = str(ROOT / "build" / "layout-negative")
        case = "negative-index-panics"
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                target = f"{binary}-{opt[1:]}"
                driver.run_test(str(STRUCTS), case, target, opt=opt)
                done = subprocess.run([target], capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("R0003: array index out of bounds", done.stderr)

    def panics(self, case: str, binary: str) -> None:
        """Build ``case`` at both opts and require the R0003 panic."""
        for opt in ("-O0", "-O2"):
            with self.subTest(case=case, opt=opt):
                target = f"{str(ROOT / 'build' / binary)}-{opt[1:]}"
                driver.run_test(str(STRUCTS), case, target, opt=opt)
                done = subprocess.run([target], capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn("R0003: array index out of bounds", done.stderr)

    def test_an_index_far_past_the_end_panics(self):
        self.panics("far-index-panics", "layout-far")

    def test_an_empty_array_panics_on_any_index(self):
        self.panics("empty-array-panics", "layout-empty")

    def test_an_array_of_structs(self):
        self.check("array-of-structs", "three\n", 0)

    def test_set_field_replaces_a_whole_array_field(self):
        self.check("set-field-replaces-an-array", "four\n", 0)

    def test_an_index_with_a_side_effect_runs_once(self):
        self.check("index-is-evaluated-once", "idx\ntwo\n", 0)


if __name__ == "__main__":
    unittest.main()
