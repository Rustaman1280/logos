"""Canonical printer.

Every file has one printed shape, so ``fmt`` output can be compared byte for
byte and works as canonical source an AI can rewrite without guessing.

Layout, in one rule: the head atom and every inline item (tokens, bare lists
and field lists) stay on the opening line; each form child after the first goes
on its own line at two spaces past the opening parenthesis. A form with a
single form child stays on one line, which keeps ``(ret (+ a b))`` compact, and
a ``fn`` or ``test`` always puts its body on its own line. ``struct`` and
``enum`` bodies are field and variant lists, so they stay inline.

Top-level forms start at column zero and are separated by one blank line. The
output ends in a newline.
"""

from __future__ import annotations

from logos.lexer import STRING, Token
from logos.nodes import FILE, Node

INDENT = 2

#: Forms whose body is a statement sequence: always broken, even with one
#: statement, so a body is visible at a glance.
BLOCK_KINDS = ("fn", "test")

#: Forms whose children are fields or variants: never broken.
FIELD_KINDS = ("struct", "enum")

_ESCAPES_OUT = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\0": "\\0",
}


def render(node: Node) -> str:
    """Return the canonical source for ``node``, trailing newline included."""
    if node.kind == FILE:
        return _render_file(node)
    return _render_form(node, 0) + "\n"


def _render_file(root: Node) -> str:
    parts: list[str] = []
    for item in root.items:
        if not isinstance(item, Node):
            continue
        if parts:
            parts.append("")
        parts.append(_render_form(item, 0))
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def _render_form(node: Node, column: int) -> str:
    head = node.kind
    children = node.items[1:] if head else node.items
    forms = [child for child in children if _is_form(child)]
    breaks = (
        head != ""
        and head not in FIELD_KINDS
        and (len(forms) > 1 or (head in BLOCK_KINDS and bool(forms)))
    )

    lines = ["(" + head]
    width = column + 1 + len(head)
    for position, child in enumerate(children):
        if breaks and position and _is_form(child):
            text = _render_item(child, column + INDENT)
            lines.append(" " * (column + INDENT) + text)
            width = column + INDENT + len(text)
        else:
            text = _render_item(child, width + 1)
            if lines[-1] == "(":
                lines[-1] += text
                width += len(text)
            else:
                lines[-1] += " " + text
                width += 1 + len(text)
    return "\n".join(lines) + ")"


def _is_form(item: Node | Token) -> bool:
    """True when a child is a named form rather than a token or bare list."""
    return isinstance(item, Node) and item.kind != ""


def _render_item(item: Node | Token, column: int) -> str:
    if isinstance(item, Token):
        return _render_token(item)
    return _render_form(item, column)


def _render_token(token: Token) -> str:
    if token.kind != STRING:
        return token.value
    body = "".join(_ESCAPES_OUT.get(ch, ch) for ch in token.value)
    return f'"{body}"'
