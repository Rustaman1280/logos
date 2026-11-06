"""Tests for name resolution: the module index, imports, duplicates and cycles.

The resolver runs on a parsed tree and answers what a module contains, what it
imports and whether any name means two things. Expressions are not its
business: the checker owns the body vocabulary, so a form inside a ``fn`` is
never an E0002 here.
"""

import unittest
from pathlib import Path

from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import Module, resolve

PARSE = Path(__file__).parent / "parse"


def resolve_src(src: str, files: dict[str, str] | None = None) -> Module:
    """Resolve ``src`` as module ``a``, with ``files`` as the loader's store."""
    sources = dict(files or {})

    def loader(module: str):
        if module not in sources:
            raise FileNotFoundError(module)
        return parse(sources[module], f"{module}.logos")

    return resolve(parse(src, "a.logos"), loader)


class ModuleTest(unittest.TestCase):
    def test_module_indexes_its_definitions(self):
        module = resolve_src(
            "(mod math)"
            "(type point (struct (x :i64)))"
            "(fn add :i64 ((a :i64) (b :i64)) (ret a))"
            "(test add (expect (code 0)))"
        )
        self.assertIsInstance(module, Module)
        self.assertEqual(module.name, "math")
        self.assertEqual(list(module.types), ["point"])
        self.assertEqual(list(module.fns), ["add"])
        self.assertEqual(list(module.tests), ["add"])
        self.assertEqual(module.fns["add"].path, "math/add")
        self.assertEqual(module.tests["add"].path, "math/test/add")
        self.assertEqual(module.imports, {})

    def test_a_test_and_a_fn_may_share_a_name(self):
        module = resolve_src("(mod m)(fn f :i64 () (ret 0))(test f (expect (code 0)))")
        self.assertEqual(list(module.fns), ["f"])
        self.assertEqual(list(module.tests), ["f"])

    def test_a_module_form_must_exist(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(fn f :i64 () (ret 0))")
        self.assertEqual(caught.exception.code, "E0002")
        self.assertIn("mod", caught.exception.message)

    def test_a_module_form_needs_a_name(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod)(fn f :i64 () (ret 0))")
        self.assertEqual(caught.exception.code, "E0002")
        self.assertIn("name", caught.exception.message)

    def test_a_second_module_form_is_a_duplicate(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(mod n)")
        self.assertEqual(caught.exception.code, "E0003")

    def test_expect_error_is_a_module_level_directive(self):
        module = resolve_src("(mod m)(expect-error E0002)")
        self.assertEqual((module.types, module.fns, module.tests), ({}, {}, {}))


class DuplicateTest(unittest.TestCase):
    def test_duplicate_fn_reports_the_second_path(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(fn f :i64 () (ret 0))(fn f :i64 () (ret 1))")
        error = caught.exception
        self.assertEqual(error.code, "E0003")
        self.assertEqual(error.path, "m/f")
        self.assertEqual(error.render(), "E0003 m/f: duplicate definition 'f'")

    def test_duplicate_type_reports_the_second_path(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(type p (struct))(type p (struct))")
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.path, "m/p")

    def test_a_fn_and_a_type_may_not_share_a_name(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(type p (struct))(fn p :i64 () (ret 0))")
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.path, "m/p")

    def test_duplicate_test_reports_its_path(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(test t (expect (code 0)))(test t (expect (code 1)))")
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.path, "m/test/t")

    def test_a_local_definition_may_not_repeat_an_imported_name(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src(
                "(mod a)(fn f :i64 () (ret 0))(use b)",
                {"b": "(mod b)(fn f :i64 () (ret 1))"},
            )
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.path, "a/1")

    def test_a_definition_after_an_import_is_the_duplicate(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src(
                "(mod a)(use b)(fn f :i64 () (ret 0))",
                {"b": "(mod b)(fn f :i64 () (ret 1))"},
            )
        self.assertEqual(caught.exception.code, "E0003")
        self.assertEqual(caught.exception.path, "a/f")


class ImportTest(unittest.TestCase):
    def test_an_import_is_resolved_and_recorded(self):
        module = resolve_src(
            "(mod a)(use b)(fn f :i64 () (ret 0))",
            {"b": "(mod b)(fn g :i64 () (ret 0))"},
        )
        self.assertEqual(list(module.imports), ["b"])
        imported = module.imports["b"]
        self.assertEqual(imported.name, "b")
        self.assertEqual(list(imported.fns), ["g"])
        # Imports are recorded, not copied: ``fns`` holds this module's own.
        self.assertEqual(list(module.fns), ["f"])

    def test_imports_are_not_transitive(self):
        module = resolve_src(
            "(mod a)(use b)",
            {
                "b": "(mod b)(use c)",
                "c": "(mod c)(fn h :i64 () (ret 0))",
            },
        )
        self.assertEqual(list(module.imports), ["b"])
        self.assertEqual(list(module.imports["b"].imports), ["c"])
        self.assertEqual(module.fns, {})

    def test_a_diamond_loads_each_module_once(self):
        loaded: list[str] = []
        sources = {
            "b": "(mod b)(use d)",
            "c": "(mod c)(use d)",
            "d": "(mod d)(fn h :i64 () (ret 0))",
        }

        def loader(name: str):
            loaded.append(name)
            return parse(sources[name], f"{name}.logos")

        module = resolve(parse("(mod a)(use b)(use c)", "a.logos"), loader)
        self.assertEqual(loaded, ["b", "d", "c"])
        self.assertIs(module.imports["b"].imports["d"], module.imports["c"].imports["d"])

    def test_the_same_import_twice_is_one_import(self):
        module = resolve_src(
            "(mod a)(use b)(use b)",
            {"b": "(mod b)(fn g :i64 () (ret 0))"},
        )
        self.assertEqual(list(module.imports), ["b"])

    def test_use_needs_one_module_name(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(use)")
        self.assertEqual(caught.exception.code, "E0002")
        self.assertEqual(caught.exception.path, "m/0")

    def test_an_unknown_module_is_a_name_not_found(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod a)(use b)")
        error = caught.exception
        self.assertEqual(error.code, "E0005")
        self.assertEqual(error.path, "a/0")
        self.assertIn("b", error.message)

    def test_a_module_that_declares_another_name_is_not_found(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod a)(use b)", {"b": "(mod c)"})
        self.assertEqual(caught.exception.code, "E0005")
        self.assertEqual(caught.exception.path, "a/0")


class CycleTest(unittest.TestCase):
    def test_circular_import_lists_the_cycle(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod a)(use b)", files={"b": "(mod b)(use a)"})
        error = caught.exception
        self.assertEqual(error.code, "E0004")
        self.assertIn("a -> b -> a", error.message)
        self.assertEqual(error.path, "b/0")

    def test_a_self_import_is_a_cycle(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod a)(use a)")
        error = caught.exception
        self.assertEqual(error.code, "E0004")
        self.assertIn("a -> a", error.message)

    def test_a_cycle_is_reported_from_its_first_module(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src(
                "(mod a)(use b)",
                {"b": "(mod b)(use c)", "c": "(mod c)(use b)"},
            )
        self.assertIn("b -> c -> b", caught.exception.message)


class UnknownNodeTest(unittest.TestCase):
    def test_an_unknown_module_level_form_is_E0002(self):
        with self.assertRaises(LogosError) as caught:
            resolve_src("(mod m)(wat 1)")
        error = caught.exception
        self.assertEqual(error.code, "E0002")
        self.assertEqual(error.path, "m/0")
        self.assertEqual(error.render(), "E0002 m/0: unknown node 'wat'")

    def test_the_negative_corpus_case_matches_its_expect_file(self):
        source = (PARSE / "unknown-node.logos").read_text(encoding="utf-8")
        expected = (PARSE / "unknown-node.expect").read_text(encoding="utf-8").strip()
        with self.assertRaises(LogosError) as caught:
            resolve_src(source)
        self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
