"""Tests for the generated manual, its manifest, and the drift check.

The manual exists so an AI can learn Logos in one pass, which only works if it
is true. These tests pin the three ways it could become false: a compiler table
growing without the manifest, a chapter that stops compiling or stops being
canonical, and ``docs/LOGOS.md`` drifting from what the generator writes.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from logos import check as checker
from logos import diag, fmt, guide, tester, types
from logos.cli import main
from logos.nodes import Node
from logos.parser import parse

ARITY = re.compile(r"\A\d+(-\d+)?\+?\Z")
MANIFEST_KEYS = {
    "forms",
    "types",
    "operators",
    "builtins",
    "reserved",
    "codes",
    "categories",
}


class ManifestTest(unittest.TestCase):
    """The JSON manifest is complete and sourced from the compiler's tables."""

    def test_keys_are_exactly_the_documented_set(self):
        self.assertEqual(set(guide.build_manifest()), MANIFEST_KEYS)

    def test_lists_every_diagnostic_code(self):
        codes = guide.build_manifest()["codes"]
        self.assertEqual(set(codes), set(diag.CODES))
        for code, meaning in diag.CODES.items():
            self.assertEqual(codes[code], meaning)

    def test_lists_every_composite_type_form(self):
        forms = guide.build_manifest()["forms"]
        names = {form["name"] for form in forms}
        self.assertTrue(set(types.FORMS) <= names)

    def test_every_form_has_a_name_arity_and_meaning(self):
        for form in guide.build_manifest()["forms"]:
            self.assertEqual(set(form), {"name", "arity", "meaning"})
            self.assertTrue(form["name"])
            self.assertRegex(form["arity"], ARITY)
            self.assertTrue(form["meaning"])
            self.assertNotIn("|", form["meaning"])

    def test_lists_every_primitive_through_integers_and_floats(self):
        manifest = guide.build_manifest()
        names = {entry["name"] for entry in manifest["types"]}
        self.assertEqual(names, {f":{name}" for name in types.PRIMITIVES})
        self.assertTrue({f":{name}" for name in types.INTEGERS} <= names)
        self.assertTrue({f":{name}" for name in types.FLOATS} <= names)

    def test_operators_come_from_the_arithmetic_table(self):
        manifest = guide.build_manifest()
        symbols = {operator["symbol"] for operator in manifest["operators"]}
        self.assertTrue(set(checker._ARITH) <= symbols)
        for suffix in ("<", "<=", ">", ">=", "=", "!=", "shl", "shr", "shr-u"):
            self.assertIn(suffix, symbols)
        self.assertIn("and", symbols)
        self.assertIn("or", symbols)
        self.assertIn("not", symbols)

    def test_equality_names_the_scalar_types(self):
        manifest = guide.build_manifest()
        equality = next(
            operator for operator in manifest["operators"] if operator["symbol"] == "="
        )
        for scalar in checker._SCALARS:
            self.assertIn(f":{scalar}", equality["meaning"])

    def test_builtins_are_the_checkers_builtins(self):
        names = [entry["name"] for entry in guide.build_manifest()["builtins"]]
        self.assertEqual(names, list(checker.BUILTINS))

    def test_reserved_names_are_the_checkers_reserved_names(self):
        reserved = guide.build_manifest()["reserved"]
        self.assertEqual(set(reserved["forms"]), set(checker.RESERVED_FORMS))
        self.assertEqual(tuple(reserved["prefixes"]), checker.RESERVED_PREFIXES)
        self.assertEqual(tuple(reserved["variants"]), checker.RESERVED_VARIANTS)

    def test_categories_are_the_harness_categories(self):
        self.assertEqual(
            guide.build_manifest()["categories"], list(tester.CATEGORIES)
        )

    def test_reads_tables_live_rather_than_copying_them(self):
        with patch.dict(diag.CODES, {"E9999": "a code only this test knows"}):
            self.assertIn("E9999", guide.build_manifest()["codes"])
        with patch.object(checker, "BUILTINS", ("print", "println", "log")):
            names = [entry["name"] for entry in guide.build_manifest()["builtins"]]
        self.assertIn("log", names)

    def test_is_json_serialisable(self):
        text = json.dumps(guide.build_manifest(), indent=2)
        self.assertEqual(json.loads(text), guide.build_manifest())


class ManualTest(unittest.TestCase):
    """Every chapter is a complete, canonical module that runs as declared."""

    def _paths(self) -> list[Path]:
        return sorted(guide.MANUAL_DIR.glob("*.logos"))

    def test_every_chapter_has_a_title(self):
        stems = {path.stem for path in self._paths()}
        self.assertEqual(stems, set(guide.CHAPTER_TITLES))

    def test_every_chapter_is_canonical(self):
        for path in self._paths():
            text = path.read_text(encoding="utf-8")
            self.assertEqual(fmt.render(parse(text, str(path))), text, path.name)

    def test_every_chapter_has_one_test_with_an_output_directive(self):
        for path in self._paths():
            root = parse(path.read_text(encoding="utf-8"), str(path))
            tests = [
                item
                for item in root.items
                if isinstance(item, Node) and item.kind == "test"
            ]
            self.assertEqual(len(tests), 1, path.name)
            expectation, _legacy = tester.expectation_of(tests[0])
            assert expectation is not None, path.name
            self.assertIsNotNone(expectation.out, path.name)
            self.assertIsNotNone(expectation.code, path.name)

    def test_every_chapter_compiles_and_runs_at_both_levels(self):
        summary = tester.run_suite(str(guide.MANUAL_DIR), opts=["-O0", "-O2"])
        failures = [
            f"{case.id} {case.detail}"
            for case in summary.cases
            if case.status != "PASS"
        ]
        self.assertEqual(failures, [])
        self.assertEqual(summary.total, len(self._paths()))
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.skipped, [])
        self.assertEqual(summary.leaks, 0)

    def test_markdown_embeds_every_canonical_chapter(self):
        markdown = guide.build_markdown()
        for path in self._paths():
            source = fmt.render(parse(path.read_text(encoding="utf-8"), str(path)))
            self.assertIn(source.rstrip("\n"), markdown, path.name)


class CheatSheetTest(unittest.TestCase):
    """The first 40 lines alone are enough to start writing Logos."""

    def setUp(self):
        self.head = "\n".join(guide.build_markdown().splitlines()[:40])

    def test_shape_of_a_program_is_on_the_first_screen(self):
        self.assertIn("(mod ", self.head)
        self.assertIn("(fn ", self.head)
        self.assertIn("(test ", self.head)
        self.assertIn("(expect ", self.head)

    def test_every_rule_is_on_the_first_screen(self):
        for rule in guide.RULES:
            for keyword in rule["keywords"]:
                self.assertIn(keyword, self.head, rule["text"])

    def test_manifest_pointer_is_on_the_first_screen(self):
        self.assertIn("logosc guide --json", self.head)
        self.assertIn("logosc guide --check", self.head)

    def test_ten_rules(self):
        self.assertEqual(len(guide.RULES), 10)


class CheckTest(unittest.TestCase):
    """``--check`` passes on the committed file and fails loudly when stale."""

    def test_committed_manual_is_current(self):
        self.assertEqual(guide.check_current(), [])

    def test_committed_manual_is_in_the_tree(self):
        self.assertTrue(guide.DOC_PATH.is_file())

    def test_stale_manual_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "LOGOS.md"
            stale.write_text("# an old manual\n", encoding="utf-8")
            with patch.object(guide, "DOC_PATH", stale):
                problems = guide.check_current()
        self.assertTrue(problems)
        self.assertIn("stale", problems[0])
        self.assertIn("generated", "\n".join(problems))

    def test_missing_manual_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "LOGOS.md"
            with patch.object(guide, "DOC_PATH", missing):
                problems = guide.check_current()
        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0])

    def test_cli_check_passes(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guide", "--check"])
        self.assertEqual(code, 0)
        self.assertIn("current", out.getvalue())

    def test_cli_check_fails_with_a_diff_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "LOGOS.md"
            stale.write_text("# an old manual\n", encoding="utf-8")
            err = io.StringIO()
            with patch.object(guide, "DOC_PATH", stale), redirect_stderr(err):
                code = main(["guide", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("stale", err.getvalue())

    def test_cli_prints_the_markdown(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guide"])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("# Logos: the generated manual"))
        self.assertEqual(out.getvalue(), guide.build_markdown())

    def test_cli_prints_the_manifest_as_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["guide", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), guide.build_manifest())


if __name__ == "__main__":
    unittest.main()
