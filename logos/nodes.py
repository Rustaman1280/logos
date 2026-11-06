"""The sexpr tree and its node paths.

A file is a ``file`` node whose items are the top-level forms. Every form is a
``Node`` whose ``kind`` is its head atom; ``kind`` is the empty string for a
bare list such as a parameter list. ``items`` holds every child in source
order, head included, and each child is a ``Node`` or a ``Token``.

Paths
-----
A form that carries a name extends the path with that name: ``(mod math)`` is
``math``, ``(fn add ...)`` inside it is ``math/add``. Every other child appends
its index within its parent. The module form owns the module's top-level
namespace, so the other top-level forms are numbered from zero below it:
``math/0``, ``math/1``.

Statements inside a ``fn`` or a ``test`` are grouped under ``body`` because the
signature in front of them is not part of the body's sequence: the single
``ret`` in ``(fn add :i64 (...) (ret ...))`` is ``math/add/body/0``. Items
between the name and the body keep their position: the parameter list of that
``fn`` is ``math/add/0``.

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
        """The definition name this form introduces, or ``None``."""
        if self.kind in NAMED_KINDS and len(self.items) > 1:
            head = self.items[1]
            if isinstance(head, Token) and head.kind == ATOM:
                return head.value
        return None

    def walk(self) -> Iterator["Node"]:
        """Yield this node and every node below it in source order."""
        yield self
        for item in self.items:
            if isinstance(item, Node):
                yield from item.walk()


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
            item.path = _join(prefix, item.name or str(slot))
            slot += 1
        _assign(item)
    return root


def _module(root: Node) -> Node | None:
    for item in root.items:
        if isinstance(item, Node) and item.kind == "mod" and item.name is not None:
            return item
    return None


def _assign(node: Node) -> None:
    body = _body_start(node)
    slot = 0
    for index, item in enumerate(node.items):
        if not isinstance(item, Node):
            continue
        if body is not None and index >= body:
            item.path = f"{node.path}/body/{index - body}"
        elif node.name is not None and index >= 2:
            item.path = f"{node.path}/{slot}"
            slot += 1
        else:
            item.path = f"{node.path}/{index}"
        _assign(item)


def _body_start(node: Node) -> int | None:
    """Index in ``items`` where a definition's statement body begins."""
    if node.name is None:
        return None
    if node.kind == "test":
        return 2
    if node.kind == "fn":
        for index in range(2, len(node.items)):
            item = node.items[index]
            if isinstance(item, Node) and item.kind == "":
                return index + 1
        return len(node.items)
    return None


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}/{segment}" if prefix else segment
