"""The type checker: one resolved module in, one checked module out.

Scope
-----
This pass checks what the language has so far: primitives, arithmetic,
comparisons, ``and``/``or``/``not``, ``let``/``letmut``/``set``, ``if``,
``while``, ``match``, ``break``/``continue``, ``ret``, calls, struct field
access, fixed arrays, manual memory, strings and growable vectors. A form from
a later task (``ok``) is not special yet, so it reaches the call path and is
reported as an unknown function: the vocabulary grows one task at a time, and a
name is only a built-in once this module has a rule for it. ``print`` and
``println`` are the built-in functions: each takes one value and writes it,
formatted by the runtime. The value is any scalar with a formatter of its own —
``:i64``, ``:u64``, ``:f64``, ``:bool``, ``:str`` — and nothing is converted to
reach one, so a ``:i32`` is a type mismatch and not an implicit widening.

Strings and vectors
-------------------
``:str`` is a fat pointer of bytes: ``(str-len s)`` is its byte count,
``(str-concat a b)`` builds a new one, ``(str-slice s start end)`` is the bytes
of ``[start, end)``, and ``(str-eq a b)`` is byte equality — the same answer
``=`` gives, because ``=`` accepts ``:str`` too. ``(vec T)`` is a growable
array: it starts empty at ``(make (vec T))``, grows with ``(vec-push v x)``,
reads with ``(vec-len v)`` and ``(vec-get v i)``, and releases its buffer with
``(vec-free v)``. The mutating forms — push, set, free — take a ``letmut``
binding, because they rewrite the three-word descriptor that lives in it,
exactly as ``arr-set`` rewrites the array in its own slot. An index that is not
an integer is E0400 and a string operand that is not a ``:str`` is E0401.

Memory and ownership
--------------------
``(alloc T n)`` has type ``(own T)``: a block of n elements the program must
release with ``(free p)``. ``(ptr p)`` gives the same address as a raw
``(ptr T)``, which may be walked with ``(ptr-add p i)`` and read or written
with ``(deref p)``/``(set-deref p v)``; only the owner may free, so a raw
pointer is E0301 there.

An owning value is not copied, it moves: into a binding, into a call's
``(own T)`` parameter, into a struct field, out of a ``ret``, or through
``free``. The binding left behind holds nothing, and reading it is E0300 —
which is what makes a double free a compile error in the direct case. A move
is only legal out of a ``letmut`` or a parameter (E0302): a plain ``let``
promised to hold the same value for its whole scope, while ``free`` destroys
the value rather than transferring it and so is allowed on any owning binding.
Moves are tracked per binding and conservatively across branches: each
``if`` branch, ``while`` body and ``match`` arm is checked from the ownership
state as it was before the first branch, so one branch's move cannot make a
sibling's use illegal. At the join a name counts as moved if *any* branch moved
it, because the alternative would accept a second free on the path that did.
``set`` does not re-arm a moved binding: assigning to one is E0300, since one
path may already have released the old value and no path-local assignment can
be trusted to have replaced it. A copy the checker cannot see — through an
aggregate, or from C — is what the runtime's double-free panic is for.

Structs and fixed arrays
------------------------
``(type point (struct (x :i64) (y :i64)))`` introduces a struct whose fields
are read once, in declaration order, by :func:`struct_shape`. ``(make T v...)``
builds a struct or an array and checks one value per field or element;
``(get e f)`` reads a field at its own type; ``(set-field name f v)`` and
``(arr-set name i v)`` mutate a binding and therefore need a ``letmut``.
``(arr-get a i)`` reads an element; its index must be an integer and its bounds
are checked at run time. An unknown field is E0200 and a value that does not
fit its field is E0201.

Control flow
------------
``if`` is an expression: both branches must have the same type and that type is
the ``if``'s. ``while`` is a statement. ``match`` is an expression over an
integer scrutinee whose arms are ``(pattern body...)``; a pattern is an integer
literal or ``_``, every arm must have the same type, and the last item of a
body is the arm's value. ``break`` and ``continue`` are statements with no
operand and are only legal inside a loop, which this pass tracks as a depth on
the enclosing environment: a helper ``fn`` starts at zero, because it is not
inside the loop that lexically contains it.

Purity
------
``check_module`` reads a ``Module`` and returns a new ``CheckedModule``. It
does not write to the tree, does not touch the filesystem, and makes no choice
that depends on anything but the module's contents: a second call over the same
module returns an equal result. Every diagnostic it raises carries the path of
the node that caused it, so ``LogosError.render`` never has to fall back to a
bare message.

Two passes
----------
Every signature is built before any body is checked. A module's own functions
and the functions of its direct imports therefore all have a type by the time a
call is checked, which makes source order irrelevant: a function can call one
defined later. Because imports are not merged into ``Module.fns``, walking
``Module.imports`` is what builds the visible namespace, and a caller sees the
definitions of the modules it names, never the ones they name.

What a binding is
-----------------
Bindings are block-scoped. A ``let``/``letmut`` is visible only inside the
block that declares it: a function body, an ``if`` branch, a ``while`` body or
a ``match`` arm. Reading the name outside that block is E0005. The function's
parameters form its outermost block, so a second binding of one name in that
same block is a duplicate, while a nested block may shadow an outer binding.
Parameters are immutable; only ``letmut`` allows ``set``.

Literals
--------
A numeric literal has no intrinsic width: it takes the type it is checked
against when that type is a matching primitive (an integer literal against an
integer type, a float literal against a float type) and falls back to ``:i64``
or ``:f64`` otherwise. A literal beside a non-literal operand looks ahead to
that operand's type, in arithmetic and in comparisons, so ``(< 1 a)`` with
``a :u8`` checks as ``:u8``. This is not a conversion: nothing is cast at run
time, and a value that already has a type never changes under ``compatible``.
The adopted type is also the literal's range: an integer literal that does not
fit its resolved type is E0106, never a silent truncation.

Returning
---------
A function whose declared return type is not ``:void`` must contain a ``ret``
somewhere in its body; otherwise it is E0105. A test is ``:void`` by
construction and needs none.

Tests
-----
Test bodies are checked too, because they are ordinary grammar. ``expect`` and
``expect-leak`` are harness directives, not expressions, so their children are
left alone; a helper ``fn`` inside a test is checked in that test's scope and
is invisible outside it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

from logos.diag import (
    CIRCULAR,
    DUPLICATE,
    FIELD_TYPE_MISMATCH,
    FREE_NON_OWN,
    INDEX_TYPE,
    LITERAL_RANGE,
    MISSING_RETURN,
    MOVE_IMMUTABLE,
    NOT_A_STR,
    NOT_FOUND,
    NOT_MUTABLE,
    TYPE_MISMATCH,
    UNKNOWN_FIELD,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    UNKNOWN_TYPE,
    USE_AFTER_MOVE,
    WRONG_ARITY,
    diag,
)
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.resolve import Module
from logos.types import (
    FLOATS,
    INTEGERS,
    NUMBERS,
    Arr,
    EnumRef,
    FnTy,
    Opt,
    Own,
    Prim,
    Ptr,
    Res,
    Shared,
    StructRef,
    Ty,
    TypeVar,
    Vec,
    compatible,
    parse_type,
)


@dataclass(frozen=True, slots=True)
class CheckedModule:
    """A module plus everything the checker learned about it.

    ``types`` and ``fn_sigs`` are the namespace the module can see: its own
    definitions and those of the modules it imports, which the resolver refused
    to duplicate. ``expr_types`` is keyed by node path, one entry per node the
    checker visited, so a later pass can type an expression without repeating
    scope analysis. ``structs`` holds each visible struct's fields in
    declaration order, the layout the backend needs and that the checker has
    already validated.
    """

    module: Module
    types: dict[str, Ty]
    fn_sigs: dict[str, FnTy]
    expr_types: dict[str, Ty]
    structs: dict[str, tuple[tuple[str, Ty], ...]] = field(default_factory=dict)


def check_module(m: Module) -> CheckedModule:
    """Check ``m`` and return its checked form, or raise ``LogosError``."""
    return _Checker(m).run()


def fn_shape(node: Node, types: dict[str, Ty]) -> tuple[Ty, list[tuple[str, Ty]], int]:
    """A ``fn``'s return type, its parameters and the index of its first statement.

    The signature has three spellings — ``(fn f :i64 ((a :i64)) ...)``,
    ``(fn f :(vec :i64) ((a :i64)) ...)`` and a parameter list whose type is
    marked — and the return type, the parameter list and the body are three
    items apart, not two. One function reads that shape because two readings
    would drift: the checker validates the signature with it and the lowerer
    re-reads the same signature with it.

    ``types`` is the module namespace a named type resolves against. The
    duplicate-parameter check lives here too, so a second reader inherits it.
    """
    if len(node.items) < 3:
        raise diag(
            UNKNOWN_NODE, node.path, "fn needs a return type and a parameter list"
        )
    ret, index = _read_type_at(node.items, 2, types, node.path)
    if index >= len(node.items):
        raise diag(UNKNOWN_NODE, node.path, "fn needs a parameter list")
    params: list[tuple[str, Ty]] = []
    names: set[str] = set()
    for param in _param_nodes(node.items[index], node.path):
        name, item = _param_parts(param)
        if name in names:
            raise diag(DUPLICATE, param.path, f"duplicate definition '{name}'")
        names.add(name)
        ty = _read_type(item, types, param.path)
        if compatible(ty, Prim("void")):
            # A parameter is a storage location, and no ``void`` value exists
            # to put in it; a backend that tried would emit ``void %v.0``.
            raise diag(
                TYPE_MISMATCH, param.path, f"parameter '{name}' cannot be :void"
            )
        params.append((name, ty))
    return ret, params, index + 1


def fn_param_nodes(node: Node) -> list[Node]:
    """The ``(name type)`` forms of a ``fn``, each carrying its own path.

    :func:`fn_shape` returns the resolved parameter types; this returns the
    nodes that declared them, so a pass can locate a diagnostic at the
    parameter rather than at the whole ``fn``. The shape is read once, here.
    """
    return _param_nodes(_params_item(node), node.path)


def let_shape(
    node: Node, types: dict[str, Ty]
) -> tuple[str, Ty | None, Node | Token]:
    """A binding's name, its declared type and the item holding its initialiser.

    ``declared`` is ``None`` for the unannotated spelling. The three spellings
    are ``(let x v)``, ``(let x :i64 v)`` and ``(let x :(vec :i64) v)``; the
    third splits into a marker, a list and the initialiser, which is why the
    initialiser's index is not fixed.
    """
    items = node.items
    shape = f"{node.kind} {_LET_SHAPE}"
    if len(items) < 3:
        raise diag(WRONG_ARITY, node.path, shape)
    name = _name(items[1], node.path, node.kind)
    if len(items) == 3:
        if _clearly_type(items[2]):
            # The annotation is there, the initialiser is missing.
            raise diag(WRONG_ARITY, node.path, shape)
        return name, None, items[2]
    if not _is_type_slot(items[2]):
        raise diag(WRONG_ARITY, node.path, shape)
    declared, index = _read_type_at(items, 2, types, node.path)
    if len(items) != index + 1:
        raise diag(WRONG_ARITY, node.path, shape)
    return name, declared, items[index]


def match_shape(
    node: Node,
) -> tuple[Node | Token, list[tuple[Node | Token, tuple[Node | Token, ...]]]]:
    """A ``match``'s scrutinee and its arms, each a pattern and its body items.

    The spelling is ``(match e (pat body...)...)``: every item after the
    scrutinee is one arm, its first item is the pattern and the rest is the
    body, whose last item is the arm's value. One reader because two readings
    would drift: the checker validates the spine with it and the lowerer
    re-reads the same spine with it.
    """
    items = node.items
    if len(items) < 3:
        raise diag(
            WRONG_ARITY, node.path, "'match' takes a scrutinee and at least one arm"
        )
    arms: list[tuple[Node | Token, tuple[Node | Token, ...]]] = []
    for item in items[2:]:
        if not isinstance(item, Node):
            raise diag(
                UNKNOWN_NODE, node.path, "a match arm must be (pattern body...)"
            )
        if not item.items:
            raise diag(
                WRONG_ARITY, item.path, "a match arm needs a pattern and a body"
            )
        pattern = item.items[0]
        if isinstance(pattern, Node):
            raise diag(
                UNKNOWN_NODE,
                pattern.path,
                "a match pattern is an integer literal or '_'",
            )
        if len(item.items) < 2:
            raise diag(
                WRONG_ARITY, item.path, "a match arm needs a pattern and a body"
            )
        if arms and is_wildcard(arms[-1][0]):
            # ``_`` matches everything, so a later arm is dead source. Say so
            # instead of compiling it: arms are tested in order and the reader
            # is entitled to believe the last one they wrote can run.
            raise diag(UNKNOWN_NODE, item.path, "an arm after '_' can never match")
        arms.append((pattern, item.items[1:]))
    return items[1], arms


def is_wildcard(pattern: Node | Token) -> bool:
    """Whether a match pattern is ``_``, the arm that always matches.

    Public because the lowerer branches on it too: ``_`` is the one pattern
    with no literal to compare against, so it never becomes a test block.
    """
    return isinstance(pattern, Token) and pattern.kind == ATOM and pattern.value == "_"


def struct_shape(node: Node, types: dict[str, Ty]) -> tuple[tuple[str, Ty], ...]:
    """The fields of a ``(struct ...)`` body, in declaration order.

    One reader because two would drift: the checker validates every struct with
    it and the lowerer reads the same list to build the aggregate, so field
    order — which is the whole of the C layout — cannot disagree between them.

    A field is ``(name type)``. A repeated name is E0003, a malformed field is
    E0002, and an unresolvable type is E0101, each at the field's own path.
    """
    fields: list[tuple[str, Ty]] = []
    names: set[str] = set()
    for field in node.items[1:]:
        if not isinstance(field, Node):
            raise diag(UNKNOWN_NODE, node.path, "a struct field must be (name type)")
        items = field.items
        if len(items) == 2:
            name_item, type_item = items
        elif len(items) == 3 and _marker(items[1]):
            name_item, type_item = items[0], items[2]
        else:
            raise diag(
                UNKNOWN_NODE, field.path, "a struct field must be (name type)"
            )
        name = _name(name_item, field.path, "a field")
        if name in names:
            raise diag(DUPLICATE, field.path, f"duplicate field '{name}'")
        names.add(name)
        fields.append((name, _read_type(type_item, types, field.path)))
    return tuple(fields)


@dataclass(slots=True)
class _Var:
    """One binding: its type, and what the checker knows about its value.

    ``moved`` is the ownership state of an ``(own T)`` binding: once its value
    has been transferred or released, the binding holds nothing and any read of
    it is E0300. ``param`` records that the name arrived as a parameter, which
    the callee owns outright — a parameter may be moved on, while a plain
    ``let`` needs ``letmut`` before its value can be transferred (E0302).
    """

    ty: Ty
    mutable: bool
    moved: bool = False
    param: bool = False


class _Env:
    """One function's scope: its bindings, visible functions and return type.

    Bindings live in a stack of block scopes, innermost last. The first scope
    is the function block, which holds the parameters and the bindings of the
    body; every ``if`` branch, ``while`` body and ``match`` arm pushes one of
    its own. A name is looked up from the innermost scope outwards, so a
    nested block may shadow an outer binding, and a name declared inside a
    block is gone when that block ends. That is what keeps a read after a join
    from reaching a slot the branch never stored: scope, not liveness, decides
    visibility.
    """

    def __init__(self, fns: dict[str, FnTy], *, ret: Ty, label: str) -> None:
        self.fns = dict(fns)
        self.scopes: list[dict[str, _Var]] = [{}]
        self.ret = ret
        self.label = label
        #: How many loops enclose the form being checked. ``break`` and
        #: ``continue`` are legal only when it is non-zero.
        self.loop = 0

    @contextmanager
    def block(self) -> Iterator[None]:
        """Check the form inside ``with`` as one nested block."""
        self.scopes.append({})
        try:
            yield
        finally:
            self.scopes.pop()

    def define(
        self, name: str, ty: Ty, *, mutable: bool, path: str, param: bool = False
    ) -> None:
        block = self.scopes[-1]
        if name in block:
            raise diag(DUPLICATE, path, f"duplicate definition '{name}'")
        block[name] = _Var(ty, mutable, param=param)

    def lookup(self, name: str) -> _Var | None:
        for block in reversed(self.scopes):
            var = block.get(name)
            if var is not None:
                return var
        return None

    def snapshot(self) -> list[tuple[_Var, bool]]:
        """The moved flag of every visible binding, outermost first.

        Sibling branches are checked from one snapshot: restoring it before
        each branch means one branch's move is invisible to the next, and the
        join can union what each branch moved without having lost the state
        they all started from.
        """
        return [(var, var.moved) for block in self.scopes for var in block.values()]

    @staticmethod
    def restore(snapshot: list[tuple[_Var, bool]]) -> None:
        """Put every binding in ``snapshot`` back the way it was recorded."""
        for var, moved in snapshot:
            var.moved = moved


@dataclass(frozen=True, slots=True)
class _Visible:
    """The names one module can see.

    ``own_*`` are this module's definitions; ``types``, ``fns`` and ``structs``
    add the own definitions of each direct import, and nothing deeper, because
    an import is not transitive.
    """

    own_types: dict[str, Ty]
    own_fns: dict[str, FnTy]
    own_structs: dict[str, tuple[tuple[str, Ty], ...]]
    types: dict[str, Ty]
    fns: dict[str, FnTy]
    structs: dict[str, tuple[tuple[str, Ty], ...]]


#: ``expect`` and ``expect-leak`` are harness directives, not expressions. The
#: lowerer skips them too, so the list is public rather than copied.
DIRECTIVES = ("expect", "expect-leak")

#: The two printers. They are the only names the language provides without a
#: definition, and they are overloaded over every scalar the runtime knows how
#: to format, so their rule lives in :meth:`_Checker._printer` rather than in a
#: single signature. A module-level ``fn`` of one of these names shadows the
#: builtin, because ``env.fns`` is consulted first.
PRINTERS = ("print", "println")

#: What a printer accepts. Exactly the widths the runtime has a formatter for:
#: ``:i64``, ``:u64``, ``:f64``, ``:bool`` and the ``:str`` it already printed.
#: A narrower integer needs an explicit cast, because there is no implicit
#: conversion anywhere in the language and printing is no exception.
PRINTABLE = ("i64", "u64", "f64", "bool", "str")

#: Built-in function names. A name is here once this module has a rule for it.
BUILTINS = PRINTERS

_ARITH = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide", "%": "modulo"}
_ORDERED = ("<", "<=", ">", ">=")
_EQUALITY = ("=", "!=")
#: Short-circuiting boolean operators. They are not arithmetic: both operands
#: must be ``:bool`` and so is the result.
_LOGICAL = ("and", "or")
#: The only types ``=`` and ``!=`` accept: scalars, never :void or a function.
#: ``:str`` is one of them: equality is byte comparison, and the lowerer emits
#: the length test and the byte comparison that mean it.
_SCALARS = INTEGERS + FLOATS + ("bool", "str")

#: Every integer width's inclusive range, so a literal can be refused at check
#: time instead of being truncated by the backend.
_INT_RANGE = {
    "i8": (-(2**7), 2**7 - 1),
    "i16": (-(2**15), 2**15 - 1),
    "i32": (-(2**31), 2**31 - 1),
    "i64": (-(2**63), 2**63 - 1),
    "u8": (0, 2**8 - 1),
    "u16": (0, 2**16 - 1),
    "u32": (0, 2**32 - 1),
    "u64": (0, 2**64 - 1),
}

_TYPE_FORMS = ("arr", "vec", "ptr", "own", "fn", "res", "opt", "shared")
_LET_SHAPE = "takes a name, an optional type and a value"


class _Checker:
    def __init__(self, module: Module) -> None:
        self.module = module
        self.expr_types: dict[str, Ty] = {}
        self._types: dict[str, Ty] = {}
        self._structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
        self._seen: dict[str, _Visible] = {}
        self._active: list[str] = []

    def run(self) -> CheckedModule:
        visible = self._visible(self.module)
        self._types = visible.types
        self._structs = visible.structs
        for node in self.module.fns.values():
            self._check_fn(node, visible.fns)
        for node in self.module.tests.values():
            self._check_test(node, visible.fns)
        return CheckedModule(
            self.module,
            dict(visible.types),
            dict(visible.fns),
            self.expr_types,
            dict(visible.structs),
        )

    # -- namespaces ------------------------------------------------------

    def _visible(self, module: Module) -> _Visible:
        """Build (once) the names ``module`` can see."""
        seen = self._seen.get(module.name)
        if seen is not None:
            return seen
        if module.name in self._active:
            raise diag(
                CIRCULAR,
                _module_path(module),
                "circular import: " + _cycle(self._active, module.name),
            )
        self._active.append(module.name)
        try:
            inherited: dict[str, Ty] = {}
            inherited_fns: dict[str, FnTy] = {}
            inherited_structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
            for imported in module.imports.values():
                # Only the import's own names cross the boundary: a module
                # never inherits its imports' imports.
                visible = self._visible(imported)
                inherited.update(visible.own_types)
                inherited_fns.update(visible.own_fns)
                inherited_structs.update(visible.own_structs)
            own_types = {
                name: _declared(name, node) for name, node in module.types.items()
            }
            types = dict(inherited)
            types.update(own_types)
            # Field types resolve against the complete namespace, so a struct
            # may name a type declared after it. The fields themselves are read
            # second, once every name they might mention exists.
            own_structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
            for name, node in module.types.items():
                body = node.items[2] if len(node.items) > 2 else None
                if isinstance(body, Node) and body.kind == "struct":
                    own_structs[name] = struct_shape(body, types)
            structs = dict(inherited_structs)
            structs.update(own_structs)
            own_fns: dict[str, FnTy] = {}
            for name, node in module.fns.items():
                if name.startswith("rt_"):
                    # The rt_ prefix belongs to the C runtime. A program
                    # definition would be emitted as a symbol the runtime
                    # already owns, so it is refused here rather than at link
                    # time.
                    raise diag(DUPLICATE, node.path, "rt_ names are reserved")
                own_fns[name] = self._signature(node, types)
            fns = dict(inherited_fns)
            fns.update(own_fns)
            visible = _Visible(own_types, own_fns, own_structs, types, fns, structs)
        finally:
            self._active.pop()
        self._seen[module.name] = visible
        return visible

    def _signature(self, node: Node, types: dict[str, Ty]) -> FnTy:
        if node.name is None or node.name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        ret, params, _ = fn_shape(node, types)
        return FnTy(tuple(ty for _, ty in params), ret)

    # -- bodies ----------------------------------------------------------

    def _check_fn(self, node: Node, fns: dict[str, FnTy]) -> None:
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        sig = fns.get(name)
        if sig is None:
            raise diag(UNKNOWN_NODE, node.path, f"no signature for '{name}'")
        env = _Env(fns, ret=sig.ret, label=f"function '{name}'")
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            env.define(
                param_name,
                sig.params[index],
                mutable=False,
                path=param.path,
                param=True,
            )
        self._body(node, _body_start(node), env)

    def _check_test(self, node: Node, fns: dict[str, FnTy]) -> None:
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "test needs a name")
        env = _Env(fns, ret=Prim("void"), label=f"test '{name}'")
        for item in node.items[2:]:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, node.path, "a test body must be a form")
            if item.kind in DIRECTIVES:
                continue
            if item.kind == "fn":
                self._check_helper(item, env)
                continue
            self._expr(item, env)

    def _check_helper(self, node: Node, env: _Env) -> None:
        """A ``fn`` private to one test: same rules, test-local visibility."""
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        if name in env.fns:
            raise diag(DUPLICATE, node.path, f"duplicate definition '{name}'")
        sig = self._signature(node, self._types)
        env.fns[name] = sig
        inner = _Env(env.fns, ret=sig.ret, label=f"function '{name}'")
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            inner.define(
                param_name,
                sig.params[index],
                mutable=False,
                path=param.path,
                param=True,
            )
        self._body(node, _body_start(node), inner)

    def _body(self, node: Node, start: int, env: _Env) -> None:
        statements = node.items[start:]
        for item in statements:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, node.path, "a statement must be a form")
            self._expr(item, env)
        if env.ret != Prim("void") and not _contains_ret(statements):
            raise diag(
                MISSING_RETURN,
                node.path,
                f"{env.label} must return {env.ret} but has no 'ret'",
            )

    # -- expressions -----------------------------------------------------

    def _expr(self, node: Node, env: _Env, expected: Ty | None = None) -> Ty:
        ty = self._dispatch(node, env, expected)
        self.expr_types[node.path] = ty
        return ty

    def _dispatch(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        kind = node.kind
        if kind == "":
            if not node.items:
                raise diag(UNKNOWN_NODE, node.path, "empty form")
            raise diag(UNKNOWN_NODE, node.path, "a form must start with a name")
        if kind in _ARITH:
            return self._arith(node, env, expected)
        if kind in _ORDERED or kind in _EQUALITY:
            return self._compare(node, env)
        if kind in _LOGICAL:
            return self._logical(node, env)
        if kind == "not":
            return self._not(node, env)
        if kind in ("let", "letmut"):
            return self._let(node, env, mutable=kind == "letmut")
        if kind == "set":
            return self._set(node, env)
        if kind == "if":
            return self._if(node, env, expected)
        if kind == "while":
            return self._while(node, env)
        if kind == "match":
            return self._match(node, env, expected)
        if kind in ("break", "continue"):
            return self._loop_jump(node, env)
        if kind == "ret":
            return self._ret(node, env)
        if kind == "make":
            return self._make(node, env)
        if kind == "get":
            return self._get(node, env)
        if kind == "set-field":
            return self._set_field(node, env)
        if kind == "arr-get":
            return self._arr_get(node, env)
        if kind == "arr-set":
            return self._arr_set(node, env)
        if kind == "str-len":
            return self._str_len(node, env)
        if kind == "str-concat":
            return self._str_concat(node, env)
        if kind == "str-eq":
            return self._str_eq(node, env)
        if kind == "str-slice":
            return self._str_slice(node, env)
        if kind == "vec-push":
            return self._vec_push(node, env)
        if kind == "vec-len":
            return self._vec_len(node, env)
        if kind == "vec-get":
            return self._vec_get(node, env)
        if kind == "vec-set":
            return self._vec_set(node, env)
        if kind == "vec-free":
            return self._vec_free(node, env)
        if kind == "alloc":
            return self._alloc(node, env)
        if kind == "free":
            return self._free(node, env)
        if kind == "ptr":
            return self._ptr(node, env)
        if kind == "deref":
            return self._deref(node, env)
        if kind == "set-deref":
            return self._set_deref(node, env)
        if kind == "ptr-add":
            return self._ptr_add(node, env)
        head = node.items[0] if node.items else None
        if isinstance(head, Token) and head.kind == ATOM:
            return self._call(node, env)
        raise diag(UNKNOWN_NODE, node.path, f"unknown node '{kind}'")

    def _value(
        self, item: Node | Token, env: _Env, expected: Ty | None, where: str
    ) -> Ty:
        if isinstance(item, Node):
            return self._expr(item, env, expected)
        if isinstance(item, Token):
            return self._token(item, env, expected, where)
        raise diag(UNKNOWN_NODE, where, "expected a value")

    def _token(
        self, token: Token, env: _Env, expected: Ty | None, where: str
    ) -> Ty:
        if token.kind == NUMBER:
            return _number(token, expected, where)
        if token.kind == STRING:
            return Prim("str")
        if token.kind == ATOM:
            if token.value.startswith(":"):
                raise diag(UNKNOWN_NODE, where, f"type name '{token.value}' is not a value")
            var = env.lookup(token.value)
            if var is not None:
                if var.moved:
                    # The binding is empty: whatever it used to own has been
                    # transferred or released, so reading it would read a
                    # pointer into freed memory.
                    raise diag(
                        USE_AFTER_MOVE, where, f"use after move of '{token.value}'"
                    )
                return var.ty
            if token.value in ("true", "false"):
                return Prim("bool")
            raise diag(NOT_FOUND, where, f"unknown name '{token.value}'")
        raise diag(UNKNOWN_NODE, where, "expected a value")

    def _arith(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        args = node.items[1:]
        if len(args) < 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes at least two operands")
        if _literal(args[0]) and not _literal(args[1]):
            # Look ahead: a literal has no width of its own, so the operand
            # beside it decides the type of the whole form.
            ty = self._value(args[1], env, expected, node.path)
            if not _numeric(ty):
                raise diag(
                    TYPE_MISMATCH,
                    _at(args[1], node),
                    f"'{node.kind}' needs numbers, found {ty}",
                )
            literal = self._value(args[0], env, ty, node.path)
            if not compatible(ty, literal):
                # A literal that cannot take the adopted type has not been
                # widened by it: a float in integer arithmetic (or an integer
                # in float arithmetic) is still a mismatch.
                raise diag(
                    TYPE_MISMATCH,
                    _at(args[0], node),
                    _operand_message(node.kind, ty, literal),
                )
            rest = args[2:]
        else:
            ty = self._value(args[0], env, expected, node.path)
            if not _numeric(ty):
                raise diag(
                    TYPE_MISMATCH,
                    _at(args[0], node),
                    f"'{node.kind}' needs numbers, found {ty}",
                )
            rest = args[1:]
        for arg in rest:
            operand = self._value(arg, env, ty, node.path)
            if compatible(ty, operand):
                continue
            raise diag(
                TYPE_MISMATCH,
                _at(arg, node),
                _operand_message(node.kind, ty, operand),
            )
        return ty

    def _compare(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if len(args) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        left_item, right_item = args
        if _literal(left_item) and not _literal(right_item):
            right = self._value(right_item, env, None, node.path)
            left = self._value(left_item, env, right, node.path)
        elif _literal(right_item) and not _literal(left_item):
            left = self._value(left_item, env, None, node.path)
            right = self._value(right_item, env, left, node.path)
        else:
            left = self._value(left_item, env, None, node.path)
            right = self._value(right_item, env, left, node.path)
        if not compatible(left, right):
            raise diag(
                TYPE_MISMATCH,
                _at(right_item, node),
                f"cannot compare {right} with {left}",
            )
        if node.kind in _ORDERED and not _numeric(left):
            raise diag(
                TYPE_MISMATCH,
                _at(left_item, node),
                f"'{node.kind}' needs numbers, found {left}",
            )
        if node.kind in _EQUALITY and not _scalar(left):
            raise diag(
                TYPE_MISMATCH,
                _at(left_item, node),
                f"'{node.kind}' needs scalar operands, found {left}",
            )
        return Prim("bool")

    def _let(self, node: Node, env: _Env, *, mutable: bool) -> Ty:
        name, declared, init = let_shape(node, self._types)
        ty = self._value(init, env, declared, node.path)
        if declared is not None and not compatible(declared, ty):
            raise diag(
                TYPE_MISMATCH,
                _at(init, node),
                f"{node.kind} '{name}' has type {declared}, found {ty}",
            )
        if _is_void(ty):
            # ``let x (println "a")`` has nothing to store: every ``:void``
            # form is a statement, and a backend that bound one would emit an
            # ``alloca void`` that LLVM refuses.
            raise diag(
                TYPE_MISMATCH,
                _at(init, node),
                f"cannot bind '{name}' to a :void value",
            )
        if isinstance(ty, Own):
            # An owning pointer is not copied into a second binding: the
            # initialiser's value moves here, and the old name is empty.
            self._consume(init, env, node.path)
        env.define(
            name,
            declared if declared is not None else ty,
            mutable=mutable,
            path=node.path,
        )
        return Prim("void")

    def _set(self, node: Node, env: _Env) -> Ty:
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "set takes a name and a value")
        name = _name(items[1], node.path, "set")
        var = env.lookup(name)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if not var.mutable:
            raise diag(NOT_MUTABLE, node.path, f"cannot set immutable binding '{name}'")
        value = self._value(items[2], env, var.ty, node.path)
        if not compatible(var.ty, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"cannot set '{name}' of type {var.ty} to {value}",
            )
        if isinstance(var.ty, Own):
            if var.moved:
                # The old value may already have been released, and a write
                # that happens to be on this path does not undo that: one
                # branch's ``set`` cannot re-arm a binding another branch freed.
                raise diag(
                    USE_AFTER_MOVE, node.path, f"set of moved binding '{name}'"
                )
            # Assigning to an owning binding moves the new value in. The old
            # value is gone, which is why this needs no ``letmut`` check on it.
            self._consume(items[2], env, node.path)
        return Prim("void")

    def _branches(self, env: _Env, branches: list[Callable[[], Ty]]) -> list[Ty]:
        """Check sibling branches from one ownership state and join them.

        Every branch starts from the state before the first branch, so a move
        in one cannot make a sibling's use illegal: ``(if c (free p) (free p))``
        is two independent releases on two paths, not a double free. After the
        last branch the enclosing bindings are put back in the state they
        started from, then marked moved if *any* branch moved them — whichever
        path runs, the value may be gone, and the conservative join is the only
        one that never accepts a read of memory that path released.
        """
        before = env.snapshot()
        moved: set[int] = set()
        results: list[Ty] = []
        for branch in branches:
            env.restore(before)
            results.append(branch())
            moved.update(id(var) for var, _ in before if var.moved)
        env.restore(before)
        for var, _ in before:
            if id(var) in moved:
                var.moved = True
        return results

    def _if(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        args = node.items[1:]
        if len(args) != 3:
            raise diag(WRONG_ARITY, node.path, "if takes a condition, a then and an else")
        condition = self._value(args[0], env, Prim("bool"), node.path)
        if not compatible(condition, Prim("bool")):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"condition must be :bool, found {condition}",
            )

        def then_branch() -> Ty:
            with env.block():
                return self._value(args[1], env, expected, node.path)

        def else_branch() -> Ty:
            with env.block():
                return self._value(args[2], env, expected, node.path)

        then, otherwise = self._branches(env, [then_branch, else_branch])
        if not compatible(then, otherwise):
            raise diag(
                TYPE_MISMATCH,
                _at(args[2], node),
                f"if branches do not match: {then} and {otherwise}",
            )
        return then

    def _while(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if not args:
            raise diag(
                WRONG_ARITY, node.path, "while takes a condition and an optional body"
            )
        condition = self._value(args[0], env, Prim("bool"), node.path)
        if not compatible(condition, Prim("bool")):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"condition must be :bool, found {condition}",
            )

        def body() -> Ty:
            # The body is inside a loop, so ``break`` and ``continue`` are
            # legal here and return to zero as soon as the body ends: the depth
            # is a property of the enclosing form, not of the function.
            env.loop += 1
            try:
                with env.block():
                    for statement in args[1:]:
                        if not isinstance(statement, Node):
                            raise diag(
                                UNKNOWN_NODE, node.path, "a statement must be a form"
                            )
                        self._expr(statement, env)
            finally:
                env.loop -= 1
            return Prim("void")

        # One "branch": the body runs zero or more times, and a value it freed
        # may have been released on the iteration that ran, so after the loop a
        # binding moved in the body counts as moved (the second iteration's
        # free is then the runtime's R0002 to catch, since the loop is checked
        # once).
        self._branches(env, [body])
        return Prim("void")

    def _loop_jump(self, node: Node, env: _Env) -> Ty:
        """``(break)`` and ``(continue)``: no operand, no value, a live loop."""
        if len(node.items) != 1:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes no operands")
        if env.loop == 0:
            raise diag(UNKNOWN_NODE, node.path, f"'{node.kind}' outside a loop")
        return Prim("void")

    def _logical(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if len(args) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        for arg in args:
            operand = self._value(arg, env, Prim("bool"), node.path)
            if not compatible(operand, Prim("bool")):
                raise diag(
                    TYPE_MISMATCH,
                    _at(arg, node),
                    f"'{node.kind}' needs :bool operands, found {operand}",
                )
        return Prim("bool")

    def _not(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if len(args) != 1:
            raise diag(WRONG_ARITY, node.path, "'not' takes one operand")
        operand = self._value(args[0], env, Prim("bool"), node.path)
        if not compatible(operand, Prim("bool")):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"'not' needs a :bool operand, found {operand}",
            )
        return Prim("bool")

    def _match(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        scrutinee, arms = match_shape(node)
        ty = self._value(scrutinee, env, None, node.path)
        if not _integer(ty):
            raise diag(
                TYPE_MISMATCH,
                _at(scrutinee, node),
                f"'match' needs an integer, found {ty}",
            )
        result: Ty | None = None
        branches: list[Callable[[], Ty]] = []
        for pattern, body in arms:
            self._pattern(pattern, ty, node)
            branches.append(
                lambda body=body: self._arm(body, env, expected, node)
            )
        # Every arm is checked from the pre-match state and the arms' moves are
        # unioned at the join: exactly one arm runs, but which one is not known
        # here, so a value freed in any arm is gone afterwards.
        for arm in self._branches(env, branches):
            if result is None:
                result = arm
            elif not compatible(result, arm):
                raise diag(
                    TYPE_MISMATCH,
                    node.path,
                    f"match arms do not agree: {result} and {arm}",
                )
        # ``arms`` is non-empty: ``match_shape`` refuses a match without one.
        return result  # type: ignore[return-value]

    def _pattern(self, pattern: Node | Token, ty: Ty, owner: Node) -> None:
        """One arm's pattern: an integer literal that fits ``ty``, or ``_``."""
        where = _at(pattern, owner)
        if not isinstance(pattern, Token):
            raise diag(UNKNOWN_NODE, where, "a match pattern is an integer literal or '_'")
        if pattern.kind == NUMBER:
            got = _number(pattern, ty, where)
            if not compatible(ty, got):
                raise diag(
                    TYPE_MISMATCH, where, f"a {got} pattern cannot test a {ty}"
                )
            return
        if pattern.kind == ATOM and pattern.value == "_":
            return
        raise diag(UNKNOWN_NODE, where, "a match pattern is an integer literal or '_'")

    def _arm(
        self,
        body: tuple[Node | Token, ...],
        env: _Env,
        expected: Ty | None,
        owner: Node,
    ) -> Ty:
        """The type of one arm: its last item, after the statements before it.

        The arm is one block: a ``let`` in it is visible to the items after it
        and to nothing outside the arm.
        """
        with env.block():
            for statement in body[:-1]:
                if not isinstance(statement, Node):
                    raise diag(UNKNOWN_NODE, owner.path, "a statement must be a form")
                self._expr(statement, env)
            return self._value(body[-1], env, expected, owner.path)

    def _ret(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if len(args) > 1:
            raise diag(WRONG_ARITY, node.path, "ret takes at most one value")
        if not args:
            if not compatible(env.ret, Prim("void")):
                raise diag(
                    TYPE_MISMATCH,
                    node.path,
                    f"{env.label} returns {env.ret}, found :void",
                )
            return Prim("void")
        value = self._value(args[0], env, env.ret, node.path)
        if not compatible(env.ret, value):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"{env.label} returns {env.ret}, found {value}",
            )
        if isinstance(env.ret, Own):
            # Returning an owning pointer hands it to the caller.
            self._consume(args[0], env, node.path)
        return Prim("void")

    # -- structs and fixed arrays ----------------------------------------

    def _fields(self, ty: StructRef, where: str) -> tuple[tuple[str, Ty], ...]:
        """The fields of a struct the namespace declares, or E0101."""
        fields = self._structs.get(ty.name)
        if fields is None:
            raise diag(UNKNOWN_TYPE, where, f"unknown type '{ty.name}'")
        return fields

    def _make(self, node: Node, env: _Env) -> Ty:
        """``(make T v...)``: an aggregate with one value per field or element.

        ``T`` is read the same way an annotation is, so ``(make point 1 2)`` and
        ``(make (arr :i64 3) 1 2 3)`` both name their type. Every argument takes
        the field's or element's type, which is what lets an integer literal sit
        in a ``:u8`` field without inventing a width.
        """
        items = node.items
        if len(items) < 2:
            raise diag(WRONG_ARITY, node.path, "make takes a type and its fields")
        ty, index = _read_type_at(items, 1, self._types, node.path)
        values = items[index:]
        if isinstance(ty, Vec):
            # A vector is born empty. There is no ``make``-with-elements form
            # because the element count would then be a second way to spell
            # what ``vec-push`` already says.
            if values:
                raise diag(
                    WRONG_ARITY,
                    node.path,
                    "a vector starts empty: make takes no elements",
                )
            return ty
        if isinstance(ty, StructRef):
            fields = self._fields(ty, node.path)
            if len(values) != len(fields):
                raise diag(
                    WRONG_ARITY,
                    node.path,
                    f"struct '{ty.name}' takes {_count(len(fields), 'field')}, "
                    f"got {len(values)}",
                )
            for item, (field, fty) in zip(values, fields):
                got = self._value(item, env, fty, node.path)
                if not compatible(fty, got):
                    raise diag(
                        FIELD_TYPE_MISMATCH,
                        _at(item, node),
                        f"field '{field}' of '{ty.name}' expects {fty}, found {got}",
                    )
                if isinstance(fty, Own):
                    self._consume(item, env, node.path)
            return ty
        if isinstance(ty, Arr):
            if len(values) != ty.n:
                raise diag(
                    WRONG_ARITY,
                    node.path,
                    f"array '{ty}' takes {_count(ty.n, 'element')}, "
                    f"got {len(values)}",
                )
            for item in values:
                got = self._value(item, env, ty.elem, node.path)
                if not compatible(ty.elem, got):
                    raise diag(
                        TYPE_MISMATCH,
                        _at(item, node),
                        f"cannot store {got} in {ty}",
                    )
                if isinstance(ty.elem, Own):
                    self._consume(item, env, node.path)
            return ty
        if isinstance(ty, EnumRef):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'make' of enum '{ty.name}' is not lowered yet",
            )
        raise diag(
            UNKNOWN_TYPE, node.path, f"make needs a struct or array type, found {ty}"
        )

    def _get(self, node: Node, env: _Env) -> Ty:
        """``(get e field)``: one field of a struct value, at its own type."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'get' takes a struct and a field")
        ty = self._value(items[1], env, None, node.path)
        if not isinstance(ty, StructRef):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"'get' needs a struct, found {ty}",
            )
        name = _name(items[2], node.path, "get")
        for field, fty in self._fields(ty, node.path):
            if field == name:
                return fty
        raise diag(UNKNOWN_FIELD, node.path, f"struct '{ty.name}' has no field '{name}'")

    def _set_field(self, node: Node, env: _Env) -> Ty:
        """``(set-field name field value)``: mutate one field in place.

        The target is a binding, not an arbitrary expression: a temporary
        aggregate has no storage to write through, and pretending it did would
        silently discard the write. Mutability is the binding's, so a ``let``
        cannot be field-assigned while a ``letmut`` can.
        """
        items = node.items
        if (
            len(items) != 4
            or not isinstance(items[1], Token)
            or items[1].kind != ATOM
        ):
            raise diag(
                WRONG_ARITY,
                node.path,
                "set-field takes a mutable name, a field and a value",
            )
        name = _name(items[1], node.path, "set-field")
        var = env.lookup(name)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if not var.mutable:
            raise diag(NOT_MUTABLE, node.path, f"cannot set immutable binding '{name}'")
        if not isinstance(var.ty, StructRef):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"'set-field' needs a struct, found {var.ty}",
            )
        field = _name(items[2], node.path, "set-field")
        for fname, fty in self._fields(var.ty, node.path):
            if fname != field:
                continue
            got = self._value(items[3], env, fty, node.path)
            if not compatible(fty, got):
                raise diag(
                    FIELD_TYPE_MISMATCH,
                    _at(items[3], node),
                    f"field '{field}' of '{var.ty.name}' expects {fty}, found {got}",
                )
            if isinstance(fty, Own):
                self._consume(items[3], env, node.path)
            return Prim("void")
        raise diag(
            UNKNOWN_FIELD,
            node.path,
            f"struct '{var.ty.name}' has no field '{field}'",
        )

    def _arr_get(self, node: Node, env: _Env) -> Ty:
        """``(arr-get a i)``: the element type, indexed at run time."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'arr-get' takes an array and an index")
        ty = self._value(items[1], env, None, node.path)
        if not isinstance(ty, Arr):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"'arr-get' needs a fixed array, found {ty}",
            )
        index = self._value(items[2], env, None, node.path)
        if not _integer(index):
            raise diag(
                INDEX_TYPE,
                _at(items[2], node),
                f"array index must be an integer, found {index}",
            )
        return ty.elem

    def _arr_set(self, node: Node, env: _Env) -> Ty:
        """``(arr-set name i v)``: mutate one element of a binding's array."""
        items = node.items
        if (
            len(items) != 4
            or not isinstance(items[1], Token)
            or items[1].kind != ATOM
        ):
            raise diag(
                WRONG_ARITY,
                node.path,
                "arr-set takes a mutable name, an index and a value",
            )
        name = _name(items[1], node.path, "arr-set")
        var = env.lookup(name)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if not var.mutable:
            raise diag(NOT_MUTABLE, node.path, f"cannot set immutable binding '{name}'")
        if not isinstance(var.ty, Arr):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"'arr-set' needs a fixed array, found {var.ty}",
            )
        index = self._value(items[2], env, None, node.path)
        if not _integer(index):
            raise diag(
                INDEX_TYPE,
                _at(items[2], node),
                f"array index must be an integer, found {index}",
            )
        got = self._value(items[3], env, var.ty.elem, node.path)
        if not compatible(var.ty.elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(items[3], node),
                f"cannot store {got} in {var.ty}",
            )
        if isinstance(var.ty.elem, Own):
            self._consume(items[3], env, node.path)
        return Prim("void")

    # -- strings ----------------------------------------------------------

    def _str_operand(self, item: Node | Token, env: _Env, node: Node, what: str) -> Ty:
        """One operand that must be a ``:str``, or E0401 at that operand."""
        ty = self._value(item, env, None, node.path)
        if not (isinstance(ty, Prim) and ty.name == "str"):
            raise diag(NOT_A_STR, _at(item, node), f"'{what}' takes a :str, found {ty}")
        return ty

    def _index(self, item: Node | Token, env: _Env, node: Node, what: str) -> Ty:
        """One operand that must be an integer width, or E0400 at it.

        A separate code from a type mismatch because every indexing operation
        shares it: an array index, a vector index and a slice bound are the
        same rule, and a program that gets one wrong is told the same thing.
        """
        ty = self._value(item, env, None, node.path)
        if not _integer(ty):
            raise diag(
                INDEX_TYPE, _at(item, node), f"{what} must be an integer, found {ty}"
            )
        return ty

    def _str_len(self, node: Node, env: _Env) -> Ty:
        """``(str-len s)``: the byte count, as ``:i64``.

        Bytes, not characters: the language's strings are UTF-8 and its length
        has always counted bytes, so ``"é→"`` is five.
        """
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "'str-len' takes a :str")
        self._str_operand(items[1], env, node, "str-len")
        return Prim("i64")

    def _str_concat(self, node: Node, env: _Env) -> Ty:
        """``(str-concat a b)``: a new string holding both, in order."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'str-concat' takes two strings")
        self._str_operand(items[1], env, node, "str-concat")
        self._str_operand(items[2], env, node, "str-concat")
        return Prim("str")

    def _str_eq(self, node: Node, env: _Env) -> Ty:
        """``(str-eq a b)``: byte equality, the same answer as ``=``."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'str-eq' takes two strings")
        self._str_operand(items[1], env, node, "str-eq")
        self._str_operand(items[2], env, node, "str-eq")
        return Prim("bool")

    def _str_slice(self, node: Node, env: _Env) -> Ty:
        """``(str-slice s start end)``: bytes ``[start, end)`` as a view.

        Both bounds are checked against the length and against each other at
        run time (R0003), because a slice of a string the program computed is
        not something the checker can know.
        """
        items = node.items
        if len(items) != 4:
            raise diag(
                WRONG_ARITY, node.path, "'str-slice' takes a :str, a start and an end"
            )
        self._str_operand(items[1], env, node, "str-slice")
        self._index(items[2], env, node, "slice index")
        self._index(items[3], env, node, "slice index")
        return Prim("str")

    # -- vectors ----------------------------------------------------------

    def _vec_operand(self, item: Node | Token, env: _Env, node: Node, what: str) -> Vec:
        """One operand that must be a ``(vec T)``, or E0100 at it."""
        ty = self._value(item, env, None, node.path)
        if not isinstance(ty, Vec):
            raise diag(
                TYPE_MISMATCH, _at(item, node), f"'{what}' needs a (vec T), found {ty}"
            )
        return ty

    def _vec_target(
        self, node: Node, env: _Env, *, arity: int, shape: str, verb: str
    ) -> tuple[_Var, Vec]:
        """The mutable vector binding a mutating vector form writes through.

        A push may reallocate, so it changes the descriptor; a set writes
        through the buffer but the descriptor stays. Either way the target is
        a binding rather than a temporary — a temporary has no storage the new
        descriptor could be written back to — and it needs ``letmut``, exactly
        like ``arr-set``.
        """
        items = node.items
        if len(items) != arity:
            raise diag(WRONG_ARITY, node.path, shape)
        if not isinstance(items[1], Token) or items[1].kind != ATOM:
            raise diag(WRONG_ARITY, node.path, shape)
        name = _name(items[1], node.path, node.kind)
        var = env.lookup(name)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if not var.mutable:
            raise diag(
                NOT_MUTABLE, node.path, f"cannot {verb} immutable binding '{name}'"
            )
        if not isinstance(var.ty, Vec):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"'{node.kind}' needs a (vec T), found {var.ty}",
            )
        return var, var.ty

    def _vec_push(self, node: Node, env: _Env) -> Ty:
        """``(vec-push v x)``: append one element, growing the buffer."""
        var, ty = self._vec_target(
            node,
            env,
            arity=3,
            shape="vec-push takes a mutable name and a value",
            verb="push to",
        )
        got = self._value(node.items[2], env, ty.elem, node.path)
        if not compatible(ty.elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(node.items[2], node),
                f"cannot push {got} into {ty}",
            )
        if isinstance(ty.elem, Own):
            self._consume(node.items[2], env, node.path)
        return Prim("void")

    def _vec_len(self, node: Node, env: _Env) -> Ty:
        """``(vec-len v)``: how many elements the vector holds, as ``:i64``."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "'vec-len' takes a vector")
        self._vec_operand(items[1], env, node, "vec-len")
        return Prim("i64")

    def _vec_get(self, node: Node, env: _Env) -> Ty:
        """``(vec-get v i)``: one element, bounds-checked at run time."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'vec-get' takes a vector and an index")
        ty = self._vec_operand(items[1], env, node, "vec-get")
        self._index(items[2], env, node, "vector index")
        return ty.elem

    def _vec_set(self, node: Node, env: _Env) -> Ty:
        """``(vec-set v i x)``: replace one element in place."""
        var, ty = self._vec_target(
            node,
            env,
            arity=4,
            shape="vec-set takes a mutable name, an index and a value",
            verb="set",
        )
        self._index(node.items[2], env, node, "vector index")
        got = self._value(node.items[3], env, ty.elem, node.path)
        if not compatible(ty.elem, got):
            raise diag(
                TYPE_MISMATCH, _at(node.items[3], node), f"cannot store {got} in {ty}"
            )
        if isinstance(ty.elem, Own):
            self._consume(node.items[3], env, node.path)
        return Prim("void")

    def _vec_free(self, node: Node, env: _Env) -> Ty:
        """``(vec-free v)``: release the buffer, leaving the vector empty.

        The descriptor is zeroed rather than left dangling, so a second free
        releases nothing — ``rt_free`` treats NULL as free(NULL) — and a
        subsequent length reads zero instead of a stale count.
        """
        self._vec_target(
            node,
            env,
            arity=2,
            shape="vec-free takes a mutable name",
            verb="free",
        )
        return Prim("void")

    # -- memory ----------------------------------------------------------

    def _alloc(self, node: Node, env: _Env) -> Ty:
        """``(alloc T n)``: ``(own T)``, n elements of T on the heap.

        The count is in elements, not bytes: the lowerer multiplies by the
        element's size, which is the one place that knows the layout. Nothing
        is initialised, because a block the program has not written is not a
        value the language has a spelling for.
        """
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "alloc takes a type and an element count"
            )
        elem, _ = _read_type_at(items, 1, self._types, node.path)
        if _is_void(elem):
            raise diag(TYPE_MISMATCH, node.path, "cannot allocate :void")
        count = self._value(items[2], env, None, node.path)
        if not _integer(count):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"alloc count must be an integer, found {count}",
            )
        return Own(elem)

    def _free(self, node: Node, env: _Env) -> Ty:
        """``(free p)``: release a block and empty the binding that owned it.

        Releasing is not a transfer, so it is legal on a plain ``let``: the
        value is destroyed where it stands and nothing takes its place. A raw
        pointer is E0301, because it is a view of memory somebody else owns and
        freeing it would release a block twice or release what was never
        allocated.
        """
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "free takes one pointer")
        ty = self._value(items[1], env, None, node.path)
        if isinstance(ty, Ptr):
            raise diag(
                FREE_NON_OWN,
                _at(items[1], node),
                f"cannot free {ty}: it does not own its memory",
            )
        if not isinstance(ty, Own):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"free needs an (own T), found {ty}",
            )
        self._consume(items[1], env, node.path, release=True)
        return Prim("void")

    def _ptr(self, node: Node, env: _Env) -> Ty:
        """``(ptr p)``: an owning pointer seen as a raw one.

        The conversion is what makes a borrow explicit: the raw pointer may be
        handed to code that cannot release, and the owner is still the one that
        frees. Nothing moves, so the own binding stays usable.
        """
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "ptr takes one owning pointer")
        ty = self._value(items[1], env, None, node.path)
        if isinstance(ty, Own):
            return Ptr(ty.elem)
        raise diag(
            TYPE_MISMATCH,
            _at(items[1], node),
            f"ptr takes an owning pointer, found {ty}",
        )

    def _deref(self, node: Node, env: _Env) -> Ty:
        """``(deref p)``: the T a pointer to T points at."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "deref takes one pointer")
        ty = self._value(items[1], env, None, node.path)
        elem = _pointee(ty)
        if elem is None:
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"deref needs a pointer, found {ty}",
            )
        return elem

    def _set_deref(self, node: Node, env: _Env) -> Ty:
        """``(set-deref p v)``: store v where p points.

        The binding that holds the pointer is not mutated — the memory behind
        the pointer is — so this needs no ``letmut``, exactly like a field
        write through a C pointer.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "set-deref takes a pointer and a value")
        ty = self._value(items[1], env, None, node.path)
        elem = _pointee(ty)
        if elem is None:
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"deref needs a pointer, found {ty}",
            )
        got = self._value(items[2], env, elem, node.path)
        if not compatible(elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"cannot store {got} through {ty}",
            )
        if isinstance(elem, Own):
            self._consume(items[2], env, node.path)
        return Prim("void")

    def _ptr_add(self, node: Node, env: _Env) -> Ty:
        """``(ptr-add p n)``: the pointer n elements along.

        The result is raw: an offset may point past the end of the block or
        into the middle of an element, and only the caller knows which offsets
        it will read, so the arithmetic always leaves the owning pointer behind.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "ptr-add takes a pointer and an offset")
        ty = self._value(items[1], env, None, node.path)
        elem = _pointee(ty)
        if elem is None:
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"ptr-add needs a pointer, found {ty}",
            )
        offset = self._value(items[2], env, None, node.path)
        if not _integer(offset):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"pointer offset must be an integer, found {offset}",
            )
        return Ptr(elem)

    def _consume(
        self, item: Node | Token, env: _Env, where: str, *, release: bool = False
    ) -> None:
        """Move the value ``item`` names out of its binding.

        Only a name has a binding to empty; a temporary is already unattached,
        so a form is consumed by nobody and this is a no-op for it. A plain
        ``let`` cannot be transferred from — the binding promised to hold the
        same value for its whole scope — unless the move is a release, which
        destroys the value rather than handing it on. A parameter may always be
        moved, because its value was given up by the caller already.
        """
        if not (isinstance(item, Token) and item.kind == ATOM):
            return
        var = env.lookup(item.value)
        if var is None or not isinstance(var.ty, Own) or var.moved:
            return
        if not release and not var.mutable and not var.param:
            raise diag(
                MOVE_IMMUTABLE,
                where,
                f"cannot move out of immutable binding '{item.value}'",
            )
        var.moved = True

    def _printer(self, node: Node, env: _Env) -> Ty:
        """``print``/``println`` of one scalar the runtime formats.

        Overloaded rather than converted: the argument keeps its own type and
        the backend picks the entry point for it. A ``:i32`` is refused, not
        widened, because the language has no implicit conversion — the day a
        cast exists, the program says which width it means.
        """
        name = node.kind
        args = node.items[1:]
        if len(args) != 1:
            raise diag(
                WRONG_ARITY,
                node.path,
                f"function '{name}' takes 1 argument, got {len(args)}",
            )
        ty = self._value(args[0], env, None, node.path)
        if not (isinstance(ty, Prim) and ty.name in PRINTABLE):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"{name} takes one of :i64, :u64, :f64, :bool or :str, found {ty}",
            )
        return Prim("void")

    def _call(self, node: Node, env: _Env) -> Ty:
        name = node.kind
        var = env.lookup(name)
        if var is not None:
            if not isinstance(var.ty, FnTy):
                raise diag(
                    TYPE_MISMATCH, node.path, f"'{name}' is not a function: found {var.ty}"
                )
            sig = var.ty
        else:
            found = env.fns.get(name)
            if found is None:
                if name in PRINTERS:
                    return self._printer(node, env)
                raise diag(UNKNOWN_FUNCTION, node.path, f"unknown function '{name}'")
            sig = found
        args = node.items[1:]
        if len(args) != len(sig.params):
            raise diag(
                WRONG_ARITY,
                node.path,
                f"function '{name}' takes {_count(len(sig.params), 'argument')}, "
                f"got {len(args)}",
            )
        for index, (arg, param) in enumerate(zip(args, sig.params), start=1):
            ty = self._value(arg, env, param, node.path)
            if not compatible(param, ty):
                raise diag(
                    TYPE_MISMATCH,
                    _at(arg, node),
                    f"argument {index} of '{name}' expects {param}, found {ty}",
                )
            if isinstance(param, Own):
                # The callee owns what it is handed: the argument moves into
                # the parameter and the caller's binding is empty afterwards.
                self._consume(arg, env, node.path)
        return sig.ret


def _operand_message(kind: str, ty: Ty, operand: Ty) -> str:
    """The E0100 text for an arithmetic operand that is not ``ty``."""
    if kind == "%":
        return f"cannot take the modulo of {ty} by {operand}"
    return f"cannot {_ARITH[kind]} {operand} to {ty}"


def _declared(name: str, node: Node) -> Ty:
    """The type a ``(type name ...)`` definition introduces."""
    if name.startswith(":"):
        raise diag(UNKNOWN_NODE, node.path, "type needs a name")
    body = node.items[2] if len(node.items) > 2 else None
    if not isinstance(body, Node):
        raise diag(UNKNOWN_NODE, node.path, f"type '{name}' needs a struct or enum body")
    if body.kind == "struct":
        return StructRef(name)
    if body.kind == "enum":
        return EnumRef(name)
    raise diag(UNKNOWN_NODE, body.path, f"unknown type body '{body.kind}'")


def _read_type(item: Node | Token, types: dict[str, Ty], fallback: str) -> Ty:
    """Read one type item and resolve its names, locating errors at it."""
    path = item.path if isinstance(item, Node) and item.path else fallback
    return resolve_type(parse_type(item, path=path), types, path)


def _read_type_at(
    items: tuple[Node | Token, ...],
    start: int,
    types: dict[str, Ty],
    fallback: str,
) -> tuple[Ty, int]:
    """Read the type at ``items[start]``, returning it and the next index.

    A composite type is written ``:(vec :i64)``, which the lexer splits into a
    lone ``:`` atom and the list, so the marker is consumed here.
    """
    item = items[start]
    if _marker(item):
        if start + 1 >= len(items):
            raise diag(UNKNOWN_TYPE, fallback, "expected a type after ':'")
        item = items[start + 1]
        start += 1
    return _read_type(item, types, fallback), start + 1


def resolve_type(ty: Ty, types: dict[str, Ty], path: str) -> Ty:
    """Resolve every named reference in ``ty`` against ``types``.

    A ``StructRef`` becomes the declared type, which turns it into an
    ``EnumRef`` when the name is an enum. An undeclared name is E0101. The
    lookup is total: an absent name is a diagnostic, never a ``KeyError``.

    Public because the lowerer needs the same answer for a type annotation it
    reads from the tree: the checker proves the annotation resolves, and the
    backend must not guess a second time.
    """
    if isinstance(ty, TypeVar):
        raise diag(
            UNKNOWN_TYPE,
            path,
            f"type variable {ty.name} is not available yet",
        )
    if isinstance(ty, Prim):
        return ty
    if isinstance(ty, (StructRef, EnumRef)):
        declared = types.get(ty.name)
        if declared is None:
            raise diag(UNKNOWN_TYPE, path, f"unknown type '{ty.name}'")
        return declared
    if isinstance(ty, Ptr):
        return Ptr(resolve_type(ty.elem, types, path))
    if isinstance(ty, Own):
        return Own(resolve_type(ty.elem, types, path))
    if isinstance(ty, Opt):
        return Opt(resolve_type(ty.elem, types, path))
    if isinstance(ty, Shared):
        return Shared(resolve_type(ty.elem, types, path))
    if isinstance(ty, Vec):
        return Vec(resolve_type(ty.elem, types, path))
    if isinstance(ty, Res):
        return Res(resolve_type(ty.ok, types, path), resolve_type(ty.err, types, path))
    if isinstance(ty, FnTy):
        params = tuple(resolve_type(param, types, path) for param in ty.params)
        return FnTy(params, resolve_type(ty.ret, types, path))
    # ``ty`` is the array case; spelled last so the branches read in the
    # order the type forms are documented.
    return Arr(resolve_type(ty.elem, types, path), ty.n)


def _param_nodes(item: Node | Token, path: str) -> list[Node]:
    """The parameter forms of a ``fn``, or E0002 when the list is malformed."""
    if not isinstance(item, Node):
        raise diag(UNKNOWN_NODE, path, "fn needs a parameter list")
    params: list[Node] = []
    for entry in item.items:
        if not isinstance(entry, Node):
            raise diag(UNKNOWN_NODE, path, "a parameter must be (name type)")
        params.append(entry)
    return params


def _param_parts(param: Node) -> tuple[str, Node | Token]:
    """A parameter's name and the item that spells its type."""
    items = param.items
    if not items:
        raise diag(UNKNOWN_NODE, param.path, "a parameter must be (name type)")
    name = _name(items[0], param.path, "a parameter")
    if len(items) == 2:
        return name, items[1]
    if len(items) == 3 and _marker(items[1]):
        return name, items[2]
    raise diag(UNKNOWN_NODE, param.path, "a parameter must be (name type)")


def _name(item: Node | Token, path: str, what: str) -> str:
    if isinstance(item, Token) and item.kind == ATOM and not item.value.startswith(":"):
        return item.value
    raise diag(UNKNOWN_NODE, path, f"{what} needs a name")


def _marker(item: Node | Token) -> bool:
    """Whether ``item`` is the ``:`` in front of a composite type."""
    return isinstance(item, Token) and item.kind == ATOM and item.value == ":"


def _is_type_slot(item: Node | Token) -> bool:
    """Whether an item can begin a type in an annotation slot.

    Any atom can: a bare name is a declared type or a type variable. That is
    why a four-item ``let`` always reads its third item as a type.
    """
    if _marker(item):
        return True
    if isinstance(item, Token):
        return item.kind == ATOM
    return item.kind in _TYPE_FORMS


def _clearly_type(item: Node | Token) -> bool:
    """Whether an item can only be a type, never a value.

    A bare atom is left out: it might be a variable, so a three-item ``let``
    treats it as the initialiser. A lone ``:`` marker and a ``:primitive`` have
    no value reading at all. A composite *node* is deliberately not included:
    ``(ptr p)`` is a type when ``p`` names a struct and a value when it names a
    binding, and only the scope could tell them apart. An annotated binding
    whose type is composite therefore carries the marker, ``(let x
    :(own :i64) ...)``, which is the spelling the language prints anyway.
    """
    if _marker(item):
        return True
    if isinstance(item, Token):
        return item.kind == ATOM and item.value.startswith(":")
    return False


def _body_start(node: Node) -> int:
    """The first body statement of a ``fn``, after both signature forms."""
    index = 4 if len(node.items) > 2 and _marker(node.items[2]) else 3
    return index + 1


def _params_item(node: Node) -> Node | Token:
    """The parameter list of a ``fn``, wherever the signature put it."""
    index = _body_start(node) - 1
    if index >= len(node.items):
        raise diag(UNKNOWN_NODE, node.path, "fn needs a parameter list")
    return node.items[index]


def _number(token: Token, expected: Ty | None, where: str) -> Ty:
    """The type of a numeric literal: the expected one when it fits, else a default.

    An integer literal takes the width it is checked against, so the width's
    range is the literal's range: ``(let x :i8 300)`` is E0106, not a silent
    truncation to 44. A literal with no context is ``:i64`` and must fit that.
    """
    text = token.value
    is_float = "." in text or "e" in text or "E" in text
    if is_float:
        if isinstance(expected, Prim) and expected.name in FLOATS:
            return expected
        return Prim("f64")
    if isinstance(expected, Prim) and expected.name in INTEGERS:
        ty = expected
    else:
        ty = Prim("i64")
    low, high = _INT_RANGE[ty.name]
    value = int(text)
    if not low <= value <= high:
        raise diag(LITERAL_RANGE, where, f"literal {text} does not fit {ty}")
    return ty


def _literal(item: Node | Token) -> bool:
    """Whether ``item`` is a number literal, the one value with no width yet."""
    return isinstance(item, Token) and item.kind == NUMBER


def _scalar(ty: Ty) -> bool:
    """Whether ``ty`` is a scalar — the only operand kind ``=``/``!=`` accept."""
    return isinstance(ty, Prim) and ty.name in _SCALARS


def _contains_ret(items: tuple[Node | Token, ...]) -> bool:
    """Whether any statement in ``items`` is or contains a ``ret``."""
    for item in items:
        if isinstance(item, Node):
            for node in item.walk():
                if node.kind == "ret":
                    return True
    return False


def _numeric(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name in NUMBERS


def _integer(ty: Ty) -> bool:
    """Whether ``ty`` is an integer width — what a ``match`` can test."""
    return isinstance(ty, Prim) and ty.name in INTEGERS


def _pointee(ty: Ty) -> Ty | None:
    """The type a pointer points at, or ``None`` when ``ty`` is not a pointer.

    An owning pointer and a raw pointer name the same element: which one you
    hold decides who may free the block, not what a read through it returns.
    """
    if isinstance(ty, (Own, Ptr)):
        return ty.elem
    return None


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _at(item: Node | Token, owner: Node) -> str:
    """The path a diagnostic should name: the item's, else its owner's."""
    return item.path if isinstance(item, Node) and item.path else owner.path


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _module_path(module: Module) -> str:
    for item in module.root.items:
        if isinstance(item, Node) and item.kind == "mod":
            return item.path
    return ""


def _cycle(active: list[str], name: str) -> str:
    return " -> ".join(active[active.index(name) :] + [name])
