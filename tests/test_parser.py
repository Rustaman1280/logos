"""Tests for the lexer, the sexpr parser and node paths.

The parser is grammar-agnostic: it turns balanced parentheses into ``Node``
objects and knows nothing about ``fn``, ``ret`` or any other form. Paths are
assigned from position alone, so the assertions here are about position, not
about the shape or meaning of a form.
"""

import unittest

from logos.diag import LogosError
from logos.lexer import tokenize
from logos.parser import parse

SRC = '(mod math)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n'


class LexerTest(unittest.TestCase):
    def test_token_kinds(self):
        tokens = tokenize('(mod m)\n(+ 1 2.5 "hi")', "m.logos")
        self.assertEqual(
            [t.kind for t in tokens],
            [
                "LPAREN",
                "ATOM",
                "ATOM",
                "RPAREN",
                "LPAREN",
                "ATOM",
                "NUMBER",
                "NUMBER",
                "STRING",
                "RPAREN",
            ],
        )
        self.assertEqual([t.value for t in tokens[:3]], ["(", "mod", "m"])

    def test_token_positions_are_one_based(self):
        tokens = tokenize("(mod\n  m)", "m.logos")
        self.assertEqual([(t.line, t.col) for t in tokens], [(1, 1), (1, 2), (2, 3), (2, 4)])

    def test_number_forms(self):
        tokens = tokenize("0 -1 2.5 +3 1e9 -x +", "m.logos")
        self.assertEqual(
            [t.kind for t in tokens],
            ["NUMBER", "NUMBER", "NUMBER", "NUMBER", "NUMBER", "ATOM", "ATOM"],
        )

    def test_string_escapes_are_decoded(self):
        tokens = tokenize('"a\\nb\\"c\\\\d"', "m.logos")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, "STRING")
        self.assertEqual(tokens[0].value, 'a\nb"c\\d')

    def test_unterminated_string_is_a_lexical_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            tokenize('(println "oops)', "m.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0006")
        self.assertEqual(error.path, "")
        self.assertEqual(error.render(), "E0006 m.logos:1:10: unterminated string")

    def test_unknown_escape_is_a_lexical_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            tokenize('"a\\qb"', "m.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0006")
        self.assertEqual(error.path, "")
        self.assertEqual(error.render(), "E0006 m.logos:1:3: unknown escape \\q")

    def test_unusual_whitespace_is_ignored(self):
        tokens = tokenize("(a\x0cb)", "m.logos")
        self.assertEqual([t.value for t in tokens], ["(", "a", "b", ")"])


class NodePathTest(unittest.TestCase):
    def test_paths(self):
        root = parse(SRC, "math.logos")
        self.assertEqual(
            [n.path for n in root.walk() if n.kind in ("mod", "fn", "ret")],
            ["math", "math/add", "math/add/2"],
        )

    def test_module_children_are_indexed_in_source_order(self):
        root = parse("(mod math)\n(use a)\n(use b)\n", "math.logos")
        self.assertEqual(
            [n.path for n in root.walk() if n.kind == "use"],
            ["math/0", "math/1"],
        )

    def test_expression_children_are_indexed_without_the_head(self):
        root = parse("(mod m)(fn f :i64 ((a :i64)) (ret (+ a 1)))", "m.logos")
        plus = [n for n in root.walk() if n.kind == "+"]
        self.assertEqual([n.path for n in plus], ["m/f/2/0"])

    def test_signature_paths_depend_only_on_position(self):
        """The parameter list is ``m/f/1`` whether the return type is one token or a form."""
        for return_type in (":i64", "(vec :i64)"):
            src = f"(mod m)(fn f {return_type} ((a :i64)) (ret 0))"
            root = parse(src, "m.logos")
            params = [n for n in root.walk() if n.kind == ""]
            self.assertEqual([n.path for n in params], ["m/f/1"])
            self.assertEqual([n.path for n in root.walk() if n.kind == "ret"], ["m/f/2"])

    def test_composite_return_type_is_positional(self):
        root = parse("(mod m)(fn f (vec :i64) ((a :i64)) (ret 0))", "m.logos")
        vectors = [n for n in root.walk() if n.kind == "vec"]
        self.assertEqual([n.path for n in vectors], ["m/f/0"])

    def test_fn_and_test_with_the_same_name_have_distinct_paths(self):
        root = parse("(mod math)(fn add :i64 ()(ret 0))(test add (println 1))", "math.logos")
        self.assertEqual([n.path for n in root.walk() if n.kind == "fn"], ["math/add"])
        self.assertEqual([n.path for n in root.walk() if n.kind == "test"], ["math/test/add"])

    def test_named_nodes_use_their_name_at_every_depth(self):
        root = parse("(mod math)(type point (struct (x :i64) (y :i64)))", "math.logos")
        types = [n for n in root.walk() if n.kind == "type"]
        self.assertEqual([n.path for n in types], ["math/point"])
        fields = [n.path for n in types[0].walk() if n.kind in ("x", "y")]
        self.assertEqual(fields, ["math/point/0/0", "math/point/0/1"])

    def test_walk_covers_every_node_in_source_order(self):
        kinds = [n.kind for n in parse(SRC, "math.logos").walk()]
        self.assertEqual(kinds[0], "file")
        self.assertEqual([k for k in kinds if k in ("mod", "fn", "ret")], ["mod", "fn", "ret"])

    def test_node_positions(self):
        root = parse(SRC, "math.logos")
        fns = {n.kind: n for n in root.walk() if n.kind in ("fn", "ret")}
        self.assertEqual((fns["fn"].line, fns["fn"].col), (2, 1))
        self.assertEqual((fns["ret"].line, fns["ret"].col), (3, 3))

    def test_empty_source_is_an_empty_file(self):
        self.assertEqual([n.kind for n in parse("", "empty.logos").walk()], ["file"])


class ParserDiagnosticTest(unittest.TestCase):
    def test_unbalanced_paren_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            parse("(mod x", "x.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0001")
        self.assertEqual(error.path, "x")
        self.assertEqual(error.render(), "E0001 x: unbalanced '('")

    def test_unbalanced_paren_reports_the_nested_path(self):
        with self.assertRaises(LogosError) as caught:
            parse("(mod m)\n(fn f :i64 ()\n", "m.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0001")
        self.assertEqual(error.path, "m/f")

    def test_stray_close_paren_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            parse("(mod x))", "x.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0001")
        self.assertEqual(error.path, "")
        self.assertEqual(error.render(), "E0001 x.logos:1:8: unbalanced ')'")

    def test_form_outside_parentheses_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            parse("mod", "x.logos")
        error = caught.exception
        self.assertEqual(error.code, "E0001")
        self.assertEqual(error.path, "")
        self.assertEqual(error.render(), "E0001 x.logos:1:1: form outside parentheses")


if __name__ == "__main__":
    unittest.main()
