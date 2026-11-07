"""The sexpr tree and its node paths.

A file is a ``file`` node whose items are the top-level forms. Every form is a
``Node`` whose ``kind`` is its head atom; ``kind`` is the empty string for a
bare list such as a parameter list. ``items`` holds every child in source
order, head included, and each child is a ``Node`` or a ``Token``.

Paths
-----
Paths are strictly positional. A named definition inserts its own name:
``(mod math)`` is ``math``, ``(fn add ...)`` inside it is ``math/add``, and a
test inserts ``test/<name>`` so a test and a function may share a name
(``math/test/add`` against ``math/add``). The module form owns the file's
top-level namespace, so the other top-level forms are numbered from zero below
it: ``math/0``, ``math/1``.

Every other node appends its position among its parent's children, starting at
zero for the first item after the head, and after the name as well for a named
definition. No shape heuristic renumbers anything: the parameter list of
``(fn f :i64 ((a :i64)) ...)`` is ``m/f/1`` and so is the parameter list of
``(fn f (vec :i64) ((a :i64)) ...)``, and the single ``ret`` of
``(fn f :i64 ((a :i64)) (ret (+ a 1)))`` is ``m/f/2``. Tokens occupy a
position too, so a return type in front of the body pushes the body along.
Paths never depend on the shape of a sibling, so editing one child moves no
other path.

A closure has no name to insert: ``(fn :i64 ((x :i64)) body)`` is an expression
whose second item is its return type, so it is numbered like any other unnamed
node — the return type is ``.../0``, the parameter list ``.../1`` and the body
``.../2``. Changing the return type from a token to a composite form therefore
moves no path below it.

Paths are assigned once, by ``assign_paths``, immediately after parsing. Later
passes must treat them as read-only, even though ``Node`` itself is mutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Union

from logos.lexer import ATOM, Token

FILE = "file"

#: Forms whose second item names a definition. ``use`` is not one of them: its
#: argument is a module, not a definition, so it is numbered positionally.
#: ``struct`` and ``enum`` are bodies of a ``type`` definition, not definitions
#: themselves: ``(type point (struct (x :i64) (y :i64)))``.
NAMED_KINDS = ("mod", "fn", "test", "type")


@dataclass(slots=True)
class Node:
    """One form, or the file that holds the top-level forms."""

    kind: str
    items: tuple[Union["Node", Token], ...]
    path: str = ""
    line: int = 0
    col: int = 0

    @property
    def name(self) -> str | None:
        """The definition name this form introduces, or ``None``.

        A type marker is not a name: ``(fn :i64 ((x :i64)) ...)`` is a closure
        expression, whose second item is its return type, so it has no name and
        its children are numbered positionally from that return type.
        """
        if self.kind in NAMED_KINDS and len(self.items) > 1:
            head = self.items[1]
            if (
                isinstance(head, Token)
                and head.kind == ATOM
                and not head.value.startswith(":")
            ):
                return head.value
        return None

    def walk(self) -> Iterator["Node"]:
        """Yield this node and every node below it in source order."""
        yield self
        for item in self.items:
            if isinstance(item, Node):
                yield from item.walk()


def find(root: Node, path: str) -> Node | None:
    """The node at ``path``, or ``None``.

    The empty path is the file itself. Every other path is compared against
    the ones :func:`assign_paths` set, so the two cannot drift.
    """
    if path == "":
        return root
    for node in root.walk():
        if node.path == path:
            return node
    return None


def assign_paths(root: Node) -> Node:
    """Set ``path`` on ``root`` and every node below it, and return ``root``."""
    root.path = ""
    module = _module(root)
    prefix = (module.name or "") if module is not None else ""
    slot = 0
    for item in root.items:
        if not isinstance(item, Node):
            continue
        if item is module:
            item.path = prefix
        else:
            item.path = _join(prefix, _segment(item) or str(slot))
            slot += 1
        _assign(item)
    return root


def _module(root: Node) -> Node | None:
    for item in root.items:
        if isinstance(item, Node) and item.kind == "mod" and item.name is not None:
            return item
    return None


def _segment(node: Node) -> str | None:
    """The path segment a named definition inserts, or ``None``."""
    name = node.name
    if name is None:
        return None
    if node.kind == "test":
        return f"test/{name}"
    return name


def _assign(node: Node) -> None:
    """Number the children of ``node`` and recurse."""
    if node.kind == "":
        children = node.items
    elif node.name is not None:
        children = node.items[2:]
    else:
        children = node.items[1:]
    for index, item in enumerate(children):
        if not isinstance(item, Node):
            continue
        item.path = _join(node.path, str(index))
        _assign(item)


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}/{segment}" if prefix else segment
