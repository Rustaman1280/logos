"""The type checker: one resolved module in, one checked module out.

Scope
-----
This pass checks what the language has so far: primitives, arithmetic,
comparisons, ``let``/``letmut``/``set``, ``if``, ``while``, ``ret`` and calls.
A form from a later task (``deref``, ``alloc``, ``make``) is not special yet,
so it reaches the call path and is reported as an unknown function: the
vocabulary grows one task at a time, and a name is only a built-in once this
module has a rule for it. ``print`` and ``println`` are the built-ins so far:
each takes one ``:str``, writes it unchanged and returns ``:void``. Formatting a
number as text is a job for the runtime, not for the checker, so an integer
argument is a type mismatch rather than a conversion.

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
or ``:f64`` otherwise. A literal beside a non-literal operand looks ahead to
that operand's type, in arithmetic and in comparisons, so ``(< 1 a)`` with
``a :u8`` checks as ``:u8``. This is not a conversion: nothing is cast at run
time, and a value that already has a type never changes under ``compatible``.

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

from dataclasses import dataclass

from logos.diag import (
    CIRCULAR,
    DUPLICATE,
    MISSING_RETURN,
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
        params.append((name, _read_type(item, types, param.path)))
    return ret, params, index + 1


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


#: ``expect`` and ``expect-leak`` are harness directives, not expressions. The
#: lowerer skips them too, so the list is public rather than copied.
DIRECTIVES = ("expect", "expect-leak")

#: Functions the language provides without a definition. Both write a ``:str``
#: unchanged and return nothing: a value that is not already text needs the
#: runtime's formatter, which arrives with the rest of ``:str`` support. A
#: module-level ``fn`` of one of these names shadows the builtin, because
#: ``env.fns`` is consulted first.
BUILTINS = {
    "print": FnTy((Prim("str"),), Prim("void")),
    "println": FnTy((Prim("str"),), Prim("void")),
}

_ARITH = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide", "%": "modulo"}
_ORDERED = ("<", "<=", ">", ">=")
_EQUALITY = ("=", "!=")
#: The only types ``=`` and ``!=`` accept: scalars, never :void or a function.
_SCALARS = INTEGERS + FLOATS + ("bool", "str")
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
                param_name, sig.params[index], mutable=False, path=param.path
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
                param_name, sig.params[index], mutable=False, path=param.path
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
                raise diag(
                    TYPE_MISMATCH, node.path, f"'{name}' is not a function: found {var.ty}"
                )
            sig = var.ty
        else:
            found = env.fns.get(name)
            if found is None:
                found = BUILTINS.get(name)
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
