"""The type checker: one resolved module in, one checked module out.

Scope
-----
This pass checks what the language has so far: primitives, arithmetic,
comparisons, ``let``/``letmut``/``set``, ``if``, ``while``, ``ret`` and calls.
A form from a later task (``deref``, ``alloc``, ``make``) is not special yet,
so it reaches the call path and is reported as an unknown function: the
vocabulary grows one task at a time, and a name is only a built-in once this
module has a rule for it.

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
Bindings are function-scoped. There is no block construct in the language yet,
so a ``let`` inside an ``if`` or ``while`` body is visible after it, and a
second binding of the same name in one function is a duplicate. Parameters are
immutable; only ``letmut`` allows ``set``.

Literals
--------
A numeric literal has no intrinsic width: it takes the type it is checked
against when that type is a matching primitive (an integer literal against an
integer type, a float literal against a float type) and falls back to ``:i64``
or ``:f64`` otherwise. This is not a conversion: nothing is cast at run time,
and a value that already has a type never changes under ``compatible``.

Tests
-----
Test bodies are checked too, because they are ordinary grammar. ``expect`` and
``expect-leak`` are harness directives, not expressions, so their children are
left alone; a helper ``fn`` inside a test is checked in that test's scope and
is invisible outside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from logos.diag import (
    CIRCULAR,
    DUPLICATE,
    NOT_FOUND,
    NOT_MUTABLE,
    TYPE_MISMATCH,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    UNKNOWN_TYPE,
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
    scope analysis.
    """

    module: Module
    types: dict[str, Ty]
    fn_sigs: dict[str, FnTy]
    expr_types: dict[str, Ty]


def check_module(m: Module) -> CheckedModule:
    """Check ``m`` and return its checked form, or raise ``LogosError``."""
    return _Checker(m).run()


@dataclass(slots=True)
class _Var:
    ty: Ty
    mutable: bool


class _Env:
    """One function's scope: its bindings, visible functions and return type."""

    def __init__(self, fns: dict[str, FnTy], *, ret: Ty, label: str) -> None:
        self.fns = dict(fns)
        self.vars: dict[str, _Var] = {}
        self.ret = ret
        self.label = label

    def define(self, name: str, ty: Ty, *, mutable: bool, path: str) -> None:
        if name in self.vars:
            raise diag(DUPLICATE, path, f"duplicate definition '{name}'")
        self.vars[name] = _Var(ty, mutable)

    def lookup(self, name: str) -> _Var | None:
        return self.vars.get(name)


@dataclass(frozen=True, slots=True)
class _Visible:
    """The names one module can see.

    ``own_*`` are this module's definitions; ``types`` and ``fns`` add the own
    definitions of each direct import, and nothing deeper, because an import
    is not transitive.
    """

    own_types: dict[str, Ty]
    own_fns: dict[str, FnTy]
    types: dict[str, Ty]
    fns: dict[str, FnTy]


#: ``expect`` and ``expect-leak`` are harness directives, not expressions.
_DIRECTIVES = ("expect", "expect-leak")

_ARITH = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide"}
_ORDERED = ("<", "<=", ">", ">=")
_EQUALITY = ("=", "!=")
_TYPE_FORMS = ("arr", "vec", "ptr", "own", "fn", "res", "opt", "shared")
_LET_SHAPE = "takes a name, an optional type and a value"


class _Checker:
    def __init__(self, module: Module) -> None:
        self.module = module
        self.expr_types: dict[str, Ty] = {}
        self._types: dict[str, Ty] = {}
        self._seen: dict[str, _Visible] = {}
        self._active: list[str] = []

    def run(self) -> CheckedModule:
        visible = self._visible(self.module)
        self._types = visible.types
        for node in self.module.fns.values():
            self._check_fn(node, visible.fns)
        for node in self.module.tests.values():
            self._check_test(node, visible.fns)
        return CheckedModule(
            self.module, dict(visible.types), dict(visible.fns), self.expr_types
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
            for imported in module.imports.values():
                # Only the import's own names cross the boundary: a module
                # never inherits its imports' imports.
                visible = self._visible(imported)
                inherited.update(visible.own_types)
                inherited_fns.update(visible.own_fns)
            own_types = {
                name: _declared(name, node) for name, node in module.types.items()
            }
            types = dict(inherited)
            types.update(own_types)
            own_fns = {
                name: self._signature(node, types)
                for name, node in module.fns.items()
            }
            fns = dict(inherited_fns)
            fns.update(own_fns)
            visible = _Visible(own_types, own_fns, types, fns)
        finally:
            self._active.pop()
        self._seen[module.name] = visible
        return visible

    def _signature(self, node: Node, types: dict[str, Ty]) -> FnTy:
        if node.name is None:
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        if len(node.items) < 3:
            raise diag(
                UNKNOWN_NODE, node.path, "fn needs a return type and a parameter list"
            )
        ret, index = self._type_at(node.items, 2, types, node.path)
        if index >= len(node.items):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a parameter list")
        params: list[Ty] = []
        names: set[str] = set()
        for param in _param_nodes(node.items[index], node.path):
            name, item = _param_parts(param)
            if name in names:
                raise diag(DUPLICATE, param.path, f"duplicate definition '{name}'")
            names.add(name)
            params.append(self._type(item, types, param.path))
        return FnTy(tuple(params), ret)

    def _type(self, item: Node | Token, types: dict[str, Ty], fallback: str) -> Ty:
        path = item.path if isinstance(item, Node) and item.path else fallback
        return _resolve(parse_type(item, path=path), types, path)

    def _type_at(
        self,
        items: tuple[Node | Token, ...],
        start: int,
        types: dict[str, Ty],
        fallback: str,
    ) -> tuple[Ty, int]:
        """Read the type at ``items[start]``, returning it and the next index.

        A composite type is written ``:(vec :i64)``, which the lexer splits
        into a lone ``:`` atom and the list, so the marker is consumed here.
        """
        item = items[start]
        if _marker(item):
            if start + 1 >= len(items):
                raise diag(UNKNOWN_TYPE, fallback, "expected a type after ':'")
            item = items[start + 1]
            start += 1
        return self._type(item, types, fallback), start + 1

    # -- bodies ----------------------------------------------------------

    def _check_fn(self, node: Node, fns: dict[str, FnTy]) -> None:
        name = node.name
        if name is None:
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        sig = fns.get(name)
        if sig is None:
            raise diag(UNKNOWN_NODE, node.path, f"no signature for '{name}'")
        env = _Env(fns, ret=sig.ret, label=f"function '{name}'")
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            env.define(
                param_name, sig.params[index], mutable=False, path=param.path
            )
        self._body(node, _body_start(node), env)

    def _check_test(self, node: Node, fns: dict[str, FnTy]) -> None:
        name = node.name
        if name is None:
            raise diag(UNKNOWN_NODE, node.path, "test needs a name")
        env = _Env(fns, ret=Prim("void"), label=f"test '{name}'")
        for item in node.items[2:]:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, node.path, "a test body must be a form")
            if item.kind in _DIRECTIVES:
                continue
            if item.kind == "fn":
                self._check_helper(item, env)
                continue
            self._expr(item, env)

    def _check_helper(self, node: Node, env: _Env) -> None:
        """A ``fn`` private to one test: same rules, test-local visibility."""
        name = node.name
        if name is None:
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        if name in env.fns:
            raise diag(DUPLICATE, node.path, f"duplicate definition '{name}'")
        sig = self._signature(node, self._types)
        env.fns[name] = sig
        inner = _Env(env.fns, ret=sig.ret, label=f"function '{name}'")
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            inner.define(
                param_name, sig.params[index], mutable=False, path=param.path
            )
        self._body(node, _body_start(node), inner)

    def _body(self, node: Node, start: int, env: _Env) -> None:
        for item in node.items[start:]:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, node.path, "a statement must be a form")
            self._expr(item, env)

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
        if kind in ("let", "letmut"):
            return self._let(node, env, mutable=kind == "letmut")
        if kind == "set":
            return self._set(node, env)
        if kind == "if":
            return self._if(node, env, expected)
        if kind == "while":
            return self._while(node, env)
        if kind == "ret":
            return self._ret(node, env)
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
            return _number(token, expected)
        if token.kind == STRING:
            return Prim("str")
        if token.kind == ATOM:
            if token.value.startswith(":"):
                raise diag(UNKNOWN_NODE, where, f"type name '{token.value}' is not a value")
            var = env.lookup(token.value)
            if var is not None:
                return var.ty
            if token.value in ("true", "false"):
                return Prim("bool")
            raise diag(NOT_FOUND, where, f"unknown name '{token.value}'")
        raise diag(UNKNOWN_NODE, where, "expected a value")

    def _arith(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        args = node.items[1:]
        if len(args) < 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes at least two operands")
        first = self._value(args[0], env, expected, node.path)
        if not _numeric(first):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"'{node.kind}' needs numbers, found {first}",
            )
        for arg in args[1:]:
            ty = self._value(arg, env, first, node.path)
            if compatible(first, ty):
                continue
            verb = _ARITH.get(node.kind)
            if verb is None:
                message = f"cannot take the modulo of {first} by {ty}"
            else:
                message = f"cannot {verb} {ty} to {first}"
            raise diag(TYPE_MISMATCH, _at(arg, node), message)
        return first

    def _compare(self, node: Node, env: _Env) -> Ty:
        args = node.items[1:]
        if len(args) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        left = self._value(args[0], env, None, node.path)
        right = self._value(args[1], env, left, node.path)
        if not compatible(left, right):
            raise diag(
                TYPE_MISMATCH,
                _at(args[1], node),
                f"cannot compare {right} with {left}",
            )
        if node.kind in _ORDERED and not _numeric(left):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"'{node.kind}' needs numbers, found {left}",
            )
        return Prim("bool")

    def _let(self, node: Node, env: _Env, *, mutable: bool) -> Ty:
        items = node.items
        shape = f"{node.kind} {_LET_SHAPE}"
        if len(items) < 3:
            raise diag(WRONG_ARITY, node.path, shape)
        name = _name(items[1], node.path, node.kind)
        if len(items) == 3:
            if _clearly_type(items[2]):
                # The annotation is there, the initialiser is missing.
                raise diag(WRONG_ARITY, node.path, shape)
            ty = self._value(items[2], env, None, node.path)
            env.define(name, ty, mutable=mutable, path=node.path)
            return Prim("void")

        if not _is_type_slot(items[2]):
            raise diag(WRONG_ARITY, node.path, shape)
        declared, index = self._type_at(items, 2, self._types, node.path)
        if len(items) != index + 1:
            raise diag(WRONG_ARITY, node.path, shape)
        value = self._value(items[index], env, declared, node.path)
        if not compatible(declared, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[index], node),
                f"{node.kind} '{name}' has type {declared}, found {value}",
            )
        env.define(name, declared, mutable=mutable, path=node.path)
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
        return Prim("void")

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
        then = self._value(args[1], env, expected, node.path)
        otherwise = self._value(args[2], env, expected, node.path)
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
            raise diag(WRONG_ARITY, node.path, "while takes a condition and a body")
        condition = self._value(args[0], env, Prim("bool"), node.path)
        if not compatible(condition, Prim("bool")):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"condition must be :bool, found {condition}",
            )
        for statement in args[1:]:
            if not isinstance(statement, Node):
                raise diag(UNKNOWN_NODE, node.path, "a statement must be a form")
            self._expr(statement, env)
        return Prim("void")

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
        return Prim("void")

    def _call(self, node: Node, env: _Env) -> Ty:
        name = node.kind
        var = env.lookup(name)
        if var is not None:
            if not isinstance(var.ty, FnTy):
                raise diag(UNKNOWN_FUNCTION, node.path, f"'{name}' is not a function")
            sig = var.ty
        else:
            found = env.fns.get(name)
            if found is None:
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
        return sig.ret


def _declared(name: str, node: Node) -> Ty:
    """The type a ``(type name ...)`` definition introduces."""
    body = node.items[2] if len(node.items) > 2 else None
    if not isinstance(body, Node):
        raise diag(UNKNOWN_NODE, node.path, f"type '{name}' needs a struct or enum body")
    if body.kind == "struct":
        return StructRef(name)
    if body.kind == "enum":
        return EnumRef(name)
    raise diag(UNKNOWN_NODE, body.path, f"unknown type body '{body.kind}'")


def _resolve(ty: Ty, types: dict[str, Ty], path: str) -> Ty:
    """Resolve every named reference in ``ty`` against ``types``.

    A ``StructRef`` becomes the declared type, which turns it into an
    ``EnumRef`` when the name is an enum. An undeclared name is E0101. The
    lookup is total: an absent name is a diagnostic, never a ``KeyError``.
    """
    if isinstance(ty, (Prim, TypeVar)):
        return ty
    if isinstance(ty, (StructRef, EnumRef)):
        declared = types.get(ty.name)
        if declared is None:
            raise diag(UNKNOWN_TYPE, path, f"unknown type '{ty.name}'")
        return declared
    if isinstance(ty, Ptr):
        return Ptr(_resolve(ty.elem, types, path))
    if isinstance(ty, Own):
        return Own(_resolve(ty.elem, types, path))
    if isinstance(ty, Opt):
        return Opt(_resolve(ty.elem, types, path))
    if isinstance(ty, Shared):
        return Shared(_resolve(ty.elem, types, path))
    if isinstance(ty, Vec):
        return Vec(_resolve(ty.elem, types, path))
    if isinstance(ty, Res):
        return Res(_resolve(ty.ok, types, path), _resolve(ty.err, types, path))
    if isinstance(ty, FnTy):
        params = tuple(_resolve(param, types, path) for param in ty.params)
        return FnTy(params, _resolve(ty.ret, types, path))
    # ``ty`` is the array case; spelled last so the branches read in the
    # order the type forms are documented.
    return Arr(_resolve(ty.elem, types, path), ty.n)


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
    treats it as the initialiser. A colon atom, a marker or a type form has no
    value reading.
    """
    if _marker(item):
        return True
    if isinstance(item, Token):
        return item.kind == ATOM and item.value.startswith(":")
    return item.kind in _TYPE_FORMS


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


def _number(token: Token, expected: Ty | None) -> Ty:
    """The type of a numeric literal: the expected one when it fits, else a default."""
    text = token.value
    is_float = "." in text or "e" in text or "E" in text
    if is_float:
        if isinstance(expected, Prim) and expected.name in FLOATS:
            return expected
        return Prim("f64")
    if isinstance(expected, Prim) and expected.name in INTEGERS:
        return expected
    return Prim("i64")


def _numeric(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name in NUMBERS


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
