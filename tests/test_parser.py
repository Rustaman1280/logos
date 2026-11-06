"""Tests for the lexer, the sexpr parser and node paths.

The parser is grammar-agnostic: it turns balanced parentheses into ``Node``
objects and knows nothing about ``fn``, ``ret`` or any other form. Paths are
assigned from the shape of the tree, so the assertions here are about shape,
not about meaning.
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

    def test_unterminated_string_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            tokenize('(println "oops)', "m.logos")
        self.assertEqual(caught.exception.code, "E0001")

    def test_unusual_whitespace_is_ignored(self):
        tokens = tokenize("(a\x0cb)", "m.logos")
        self.assertEqual([t.value for t in tokens], ["(", "a", "b", ")"])


class NodePathTest(unittest.TestCase):
    def test_paths(self):
        root = parse(SRC, "math.logos")
        self.assertEqual(
            [n.path for n in root.walk() if n.kind in ("mod", "fn", "ret")],
            ["math", "math/add", "math/add/body/0"],
        )

    def test_module_children_are_indexed_in_source_order(self):
        root = parse("(mod math)\n(use a)\n(use b)\n", "math.logos")
        self.assertEqual(
            [n.path for n in root.walk() if n.kind == "use"],
            ["math/0", "math/1"],
        )

    def test_expression_children_are_indexed_with_the_head(self):
        """The path Task 4 expects for the argument of a ``ret``."""
        root = parse("(mod m)(fn f :i64 ((a :i64)) (ret (+ a 1)))", "m.logos")
        plus = [n for n in root.walk() if n.kind == "+"]
        self.assertEqual([n.path for n in plus], ["m/f/body/0/1"])

    def test_named_nodes_use_their_name_at_every_depth(self):
        root = parse("(mod math)(type point (struct (x :i64) (y :i64)))", "math.logos")
        types = [n for n in root.walk() if n.kind == "type"]
        self.assertEqual([n.path for n in types], ["math/point"])
        fields = [n.path for n in types[0].walk() if n.kind in ("x", "y")]
        self.assertEqual(fields, ["math/point/0/1", "math/point/0/2"])

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
        self.assertEqual(caught.exception.code, "E0001")
        self.assertEqual(caught.exception.path, "x")

    def test_unbalanced_paren_reports_the_nested_path(self):
        with self.assertRaises(LogosError) as caught:
            parse("(mod m)\n(fn f :i64 ()\n", "m.logos")
        self.assertEqual(caught.exception.code, "E0001")
        self.assertEqual(caught.exception.path, "m/f")

    def test_stray_close_paren_is_a_diagnostic(self):
        with self.assertRaises(LogosError) as caught:
            parse("(mod x))", "x.logos")
        self.assertEqual(caught.exception.code, "E0001")


if __name__ == "__main__":
    unittest.main()
