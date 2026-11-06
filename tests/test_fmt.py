"""Tests for the canonical printer."""

import pathlib
import unittest

from logos.fmt import render
from logos.parser import parse

SRC = '(mod math)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n'

CANONICAL = (
    "(mod math)\n"
    "\n"
    "(fn add :i64 ((a :i64) (b :i64))\n"
    "  (ret (+ a b)))\n"
)

FIXTURE = pathlib.Path(__file__).parent / "parse" / "fixture.logos"


class RenderTest(unittest.TestCase):
    def test_render_canonical_form(self):
        self.assertEqual(render(parse(SRC, "math.logos")), CANONICAL)

    def test_fmt_is_idempotent(self):
        once = render(parse(SRC, "math.logos"))
        self.assertEqual(render(parse(once, "math.logos")), once)

    def test_render_of_one_subtree(self):
        root = parse(SRC, "math.logos")
        ret = [n for n in root.walk() if n.kind == "ret"][0]
        self.assertEqual(render(ret), "(ret (+ a b))\n")

    def test_nested_blocks_indent_two_spaces_per_level(self):
        src = "(mod m)\n(if (< a b) (ret 1) (ret 2))\n"
        self.assertEqual(
            render(parse(src, "m.logos")),
            "(mod m)\n\n(if (< a b)\n  (ret 1)\n  (ret 2))\n",
        )

    def test_bindings_stay_on_one_line(self):
        src = "(mod m)\n(while (< a b)\n(let x :i64 (+ a b))\n(println x))\n"
        self.assertEqual(
            render(parse(src, "m.logos")),
            "(mod m)\n\n(while (< a b)\n  (let x :i64 (+ a b))\n  (println x))\n",
        )

    def test_strings_are_escaped_canonically(self):
        src = '(mod m)\n(println "a\tb\\"c")\n'
        self.assertEqual(
            render(parse(src, "m.logos")),
            '(mod m)\n\n(println "a\\tb\\"c")\n',
        )

    def test_empty_source_renders_empty(self):
        self.assertEqual(render(parse("", "empty.logos")), "")

    def test_fixture_is_already_canonical(self):
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(render(parse(text, "fixture.logos")), text)


if __name__ == "__main__":
    unittest.main()
