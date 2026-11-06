"""Name resolution: one module per file, its imports and its definitions.

The resolver runs after parsing and before the checker. It turns a parsed file
into a ``Module``: the module's own types, functions and tests keyed by name,
plus the modules it imports. Loading an import is a graph walk, so a cycle is
reported with the whole chain, and each module is loaded at most once per
``resolve`` call. An import is never transitive: a module sees the definitions
of the modules it names, not the ones they name.

Module vocabulary
-----------------
Only module-level forms are resolved here: ``mod``, ``use``, ``fn``, ``test``,
``type`` and the ``expect-error`` directive. Forms inside a ``fn`` or a
``test`` belong to the checker, so the resolver leaves them alone; the
module-level vocabulary grows with the language, one task at a time. A form
outside that vocabulary is E0002, reported at the form's path.

Names
-----
A module's name comes from its ``mod`` form, which must exist, carry a name,
and appear once. ``fn`` and ``type`` share one namespace, because both insert
their bare name into a path: ``math/add`` is one address, whether it holds a
function or a type. ``test`` has its own namespace, because a test inserts
``test/<name>``: ``(fn add ...)`` and ``(test add ...)`` coexist. An imported
definition is visible unqualified, so repeating its name locally, or importing
two definitions of one name, is E0003. The diagnostic points at the form that
introduced the second copy: the later definition, or the ``use`` that imported
it.

Errors
------
E0002 unknown node, E0003 duplicate definition, E0004 circular import,
E0005 name not found. The last covers a module the loader cannot supply and a
loaded module that declares a different name; the other codes the compiler
uses live in :mod:`logos.diag`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from logos.diag import CIRCULAR, DUPLICATE, NOT_FOUND, UNKNOWN_NODE, LogosError, diag
from logos.lexer import ATOM, Token
from logos.nodes import Node

#: ``loader(name)`` returns the parsed tree of another module.
Loader = Callable[[str], Node]


@dataclass(slots=True)
class Module:
    """One resolved module.

    ``types``, ``fns`` and ``tests`` hold this module's own definitions, keyed
    by name; ``imports`` holds the modules ``use`` named, keyed by module name.
    Imported definitions are therefore not copied into ``fns``: a consumer
    that needs a name searches this module, then ``imports``.
    """

    name: str
    root: Node
    types: dict[str, Node]
    fns: dict[str, Node]
    tests: dict[str, Node]
    imports: dict[str, "Module"] = field(default_factory=dict)


def resolve(root: Node, loader: Loader, file: str = "") -> Module:
    """Resolve ``root`` and everything it imports.

    ``loader`` supplies the parsed tree of another module and is called at most
    once per module name. A name it cannot supply raises E0005; a parse
    diagnostic raised by it propagates unchanged.

    The loader owns the file-name-equals-module-name rule: it decides which
    file answers a name, and may reject a name it cannot map to a file. The
    resolver checks the contract it is handed instead, that the tree the loader
    returns declares the name ``(use ...)`` asked for, and raises E0005 when it
    does not.

    ``file`` names the source ``root`` was parsed from, when the caller knows
    it. It is used only to locate a file that has no ``(mod ...)`` form at all,
    so that diagnostic carries ``file:line:column`` instead of no location.
    """
    return _Resolver(loader, file).module(root, None, "")


class _Resolver:
    def __init__(self, loader: Loader, file: str = "") -> None:
        self.loader = loader
        self.file = file
        self.done: dict[str, Module] = {}
        self.active: list[str] = []

    def module(self, root: Node, expected: str | None, origin: str) -> Module:
        """Resolve a parsed file, which must declare the expected name."""
        form = _module_form(root)
        if form is None:
            if expected is not None:
                raise diag(
                    UNKNOWN_NODE,
                    origin,
                    f"module '{expected}' has no (mod ...) form",
                )
            raise _missing_module(root, self.file)
        declared = form.name
        if declared is None:
            raise diag(UNKNOWN_NODE, form.path, "mod needs a name")
        if expected is not None and declared != expected:
            raise diag(
                NOT_FOUND,
                origin,
                f"module '{expected}' declares itself '{declared}'",
            )

        self.active.append(declared)
        try:
            module = self._build(root, form, declared)
        finally:
            self.active.pop()
        self.done[declared] = module
        return module

    def _build(self, root: Node, form: Node, name: str) -> Module:
        module = Module(name, root, {}, {}, {}, {})
        # ``defs`` is the module's fn/type namespace as it grows, imported
        # names included, so a later definition or import can be checked
        # against everything visible at that point.
        defs: dict[str, Node] = {}
        tests: dict[str, Node] = {}

        for item in root.items:
            if not isinstance(item, Node):
                continue
            kind = item.kind
            if kind == "mod":
                if item is not form:
                    _duplicate_module(item)
            elif kind == "use":
                self._import(module, item, defs)
            elif kind in ("fn", "type"):
                _define(module, item, kind, defs)
            elif kind == "test":
                _define_test(module, item, tests)
            elif kind == "expect-error":
                continue
            else:
                raise diag(UNKNOWN_NODE, item.path, f"unknown node '{kind}'")
        return module

    def _import(self, module: Module, node: Node, defs: dict[str, Node]) -> None:
        name = _module_argument(node)
        if name in module.imports:
            return
        if name in self.active:
            raise diag(CIRCULAR, node.path, "circular import: " + _cycle(self.active, name))

        imported = self.done.get(name)
        if imported is None:
            try:
                root = self.loader(name)
            except (KeyError, OSError):
                raise diag(NOT_FOUND, node.path, f"unknown module '{name}'") from None
            if not isinstance(root, Node):
                raise diag(NOT_FOUND, node.path, f"unknown module '{name}'")
            imported = self.module(root, name, node.path)

        _merge(imported, defs, node.path)
        module.imports[name] = imported


def _missing_module(root: Node, file: str) -> LogosError:
    """The E0002 for a file that never declares a module.

    The root's own position is the one location that always exists, so a caller
    that names its source gets ``file:line:column``; a caller that does not gets
    the bare message, as before.
    """
    if not file:
        return diag(UNKNOWN_NODE, "", "missing (mod ...) form")
    return diag(
        UNKNOWN_NODE,
        "",
        "missing (mod ...) form",
        file=file,
        line=root.line,
        col=root.col,
    )


def _module_form(root: Node) -> Node | None:
    """The file's ``mod`` form, whether or not it carries a name."""
    for item in root.items:
        if isinstance(item, Node) and item.kind == "mod":
            return item
    return None


def _module_argument(node: Node) -> str:
    """The one module name a ``use`` form names."""
    if len(node.items) == 2:
        argument = node.items[1]
        if isinstance(argument, Token) and argument.kind == ATOM:
            return argument.value
    raise diag(UNKNOWN_NODE, node.path, "use takes one module name")


def _duplicate_module(node: Node) -> None:
    name = node.name
    if name is None:
        raise diag(UNKNOWN_NODE, node.path, "mod needs a name")
    raise diag(DUPLICATE, node.path, f"duplicate module '{name}'")


def _define(module: Module, node: Node, kind: str, defs: dict[str, Node]) -> None:
    name = node.name
    if name is None:
        raise diag(UNKNOWN_NODE, node.path, f"{kind} needs a name")
    if name in defs:
        raise diag(DUPLICATE, node.path, f"duplicate definition '{name}'")
    defs[name] = node
    table = module.types if kind == "type" else module.fns
    table[name] = node


def _define_test(module: Module, node: Node, tests: dict[str, Node]) -> None:
    name = node.name
    if name is None:
        raise diag(UNKNOWN_NODE, node.path, "test needs a name")
    if name in tests:
        raise diag(DUPLICATE, node.path, f"duplicate definition '{name}'")
    tests[name] = node
    module.tests[name] = node


def _merge(imported: Module, defs: dict[str, Node], path: str) -> None:
    """Make the imported module's definitions visible, refusing collisions."""
    for table in (imported.types, imported.fns):
        for name in table:
            if name in defs:
                raise diag(DUPLICATE, path, f"duplicate definition '{name}'")
            defs[name] = table[name]


def _cycle(active: list[str], name: str) -> str:
    """Spell the cycle that importing ``name`` would close."""
    return " -> ".join(active[active.index(name) :] + [name])
