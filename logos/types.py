"""Logos types: the vocabulary and the syntax that names it.

A type is a frozen value. Equality is structural for the composites and
nominal for ``StructRef``/``EnumRef``, which compare by name. There is no
subtyping and no conversion: :func:`compatible` answers "is this the same
type", never "can this become that", so a width or a signedness never changes
under it. A ``TypeVar`` is not a wildcard: it compares by name like any other
type, and the checker rejects it outright until substitution exists, because a
wildcard would silently convert a value from one kind to another.

Named types are written bare and primitives are written with a colon, matching
``(res :i64 parse-error)``: the colon marks the closed built-in vocabulary, so
``:point`` is an unknown primitive, not a reference to ``point``. An atom that
is a single capital letter names a type variable.

:func:`parse_type` is pure syntax. It validates primitives, because that list
is closed and needs no module to know, but it leaves a bare name as a
``StructRef`` for the checker to resolve or reject: only the checker knows
which types a module declares. Every diagnostic it raises takes an optional
``path`` so a caller holding a bare ``Token`` (which has no path of its own)
can still locate the error at the form that owns the token.
"""

from __future__ import annotations

from dataclasses import dataclass

from logos.diag import UNKNOWN_TYPE, diag
from logos.lexer import ATOM, NUMBER, Token
from logos.nodes import Node

#: The closed set of primitives, spelled as the language spells them.
PRIMITIVES = (
    "i8",
    "i16",
    "i32",
    "i64",
    "u8",
    "u16",
    "u32",
    "u64",
    "f32",
    "f64",
    "bool",
    "str",
    "void",
)

INTEGERS = PRIMITIVES[:8]
FLOATS = ("f32", "f64")
NUMBERS = INTEGERS + FLOATS

#: The composite type forms, by head name.
FORMS = ("arr", "vec", "ptr", "own", "fn", "res", "opt", "shared")


@dataclass(frozen=True, slots=True)
class Prim:
    """A built-in type such as ``:i64``."""

    name: str

    def __str__(self) -> str:
        return f":{self.name}"


@dataclass(frozen=True, slots=True)
class Ptr:
    """A raw pointer, ``(ptr T)``."""

    elem: "Ty"

    def __str__(self) -> str:
        return f"(ptr {self.elem})"


@dataclass(frozen=True, slots=True)
class Own:
    """An owning pointer, ``(own T)``."""

    elem: "Ty"

    def __str__(self) -> str:
        return f"(own {self.elem})"


@dataclass(frozen=True, slots=True)
class Arr:
    """A fixed array, ``(arr T n)``."""

    elem: "Ty"
    n: int

    def __str__(self) -> str:
        return f"(arr {self.elem} {self.n})"


@dataclass(frozen=True, slots=True)
class Vec:
    """A growable array, ``(vec T)``."""

    elem: "Ty"

    def __str__(self) -> str:
        return f"(vec {self.elem})"


@dataclass(frozen=True, slots=True)
class Res:
    """A result, ``(res T E)``."""

    ok: "Ty"
    err: "Ty"

    def __str__(self) -> str:
        return f"(res {self.ok} {self.err})"


@dataclass(frozen=True, slots=True)
class Opt:
    """An optional value, ``(opt T)``."""

    elem: "Ty"

    def __str__(self) -> str:
        return f"(opt {self.elem})"


@dataclass(frozen=True, slots=True)
class FnTy:
    """A function type, ``(fn (T...) R)``."""

    params: tuple["Ty", ...]
    ret: "Ty"

    def __str__(self) -> str:
        params = " ".join(str(param) for param in self.params)
        return f"(fn ({params}) {self.ret})"


@dataclass(frozen=True, slots=True)
class Shared:
    """Shared mutable state, ``(shared T)``."""

    elem: "Ty"

    def __str__(self) -> str:
        return f"(shared {self.elem})"


@dataclass(frozen=True, slots=True)
class StructRef:
    """A reference to a named struct, written bare: ``point``."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class EnumRef:
    """A reference to a named enum, written bare: ``parse-error``."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class TypeVar:
    """A generic parameter, a single capital letter: ``T``."""

    name: str

    def __str__(self) -> str:
        return self.name


#: Any type value. The alias is recursive, so it lives below the classes.
Ty = Prim | Ptr | Own | Arr | Vec | Res | Opt | FnTy | Shared | StructRef | EnumRef | TypeVar


def parse_type(node: "Node | Token", *, path: str = "") -> Ty:
    """Turn the syntax of a type into a ``Ty``.

    ``node`` is a node for a composite type and a token for an atom, so the
    parameter is wider than a ``Node``. ``path`` locates a diagnostic when the
    offending item is a token, which carries no path itself.

    A bare name becomes a ``StructRef``; the checker decides whether the name
    is declared and whether the declaration is an enum. A malformed type form
    or an unknown ``:primitive`` is E0101 here, because neither needs a module.
    """
    where = node.path if isinstance(node, Node) and node.path else path
    if isinstance(node, Token):
        return _atom(node, where)

    kind = node.kind
    if kind == "":
        raise diag(UNKNOWN_TYPE, where, "expected a type, found an empty list")
    if kind == "arr":
        _count(node, 3, where, "arr takes an element type and a length")
        return Arr(parse_type(node.items[1], path=where), _length(node.items[2], where))
    if kind in ("vec", "ptr", "own", "opt", "shared"):
        _count(node, 2, where, f"{kind} takes one element type")
        elem = parse_type(node.items[1], path=where)
        if kind == "vec":
            return Vec(elem)
        if kind == "ptr":
            return Ptr(elem)
        if kind == "own":
            return Own(elem)
        if kind == "opt":
            return Opt(elem)
        return Shared(elem)
    if kind == "res":
        _count(node, 3, where, "res takes an ok type and an error type")
        return Res(
            parse_type(node.items[1], path=where),
            parse_type(node.items[2], path=where),
        )
    if kind == "fn":
        _count(node, 3, where, "fn takes a parameter list and a return type")
        return FnTy(_type_list(node.items[1], where), parse_type(node.items[2], path=where))
    raise diag(UNKNOWN_TYPE, where, f"unknown type form '{kind}'")


def compatible(a: Ty, b: Ty) -> bool:
    """Whether ``a`` and ``b`` are the same type, with no widening.

    A ``TypeVar`` compares by name and nothing else. It is deliberately not a
    wildcard: generics have no substitution yet, so matching a type variable
    against any type would let a value of one kind stand in for another.
    Everything else must match exactly, including integer width, array length
    and the name of a named type.
    """
    if isinstance(a, TypeVar):
        return isinstance(b, TypeVar) and a.name == b.name
    if isinstance(a, Prim):
        return isinstance(b, Prim) and a.name == b.name
    if isinstance(a, StructRef):
        return isinstance(b, StructRef) and a.name == b.name
    if isinstance(a, EnumRef):
        return isinstance(b, EnumRef) and a.name == b.name
    if isinstance(a, Ptr):
        return isinstance(b, Ptr) and compatible(a.elem, b.elem)
    if isinstance(a, Own):
        return isinstance(b, Own) and compatible(a.elem, b.elem)
    if isinstance(a, Opt):
        return isinstance(b, Opt) and compatible(a.elem, b.elem)
    if isinstance(a, Shared):
        return isinstance(b, Shared) and compatible(a.elem, b.elem)
    if isinstance(a, Vec):
        return isinstance(b, Vec) and compatible(a.elem, b.elem)
    if isinstance(a, Arr):
        return isinstance(b, Arr) and a.n == b.n and compatible(a.elem, b.elem)
    if isinstance(a, Res):
        return (
            isinstance(b, Res)
            and compatible(a.ok, b.ok)
            and compatible(a.err, b.err)
        )
    if isinstance(a, FnTy):
        if not isinstance(b, FnTy) or len(a.params) != len(b.params):
            return False
        if not all(compatible(x, y) for x, y in zip(a.params, b.params)):
            return False
        return compatible(a.ret, b.ret)
    return False


def _atom(token: Token, where: str) -> Ty:
    if token.kind != ATOM:
        raise diag(UNKNOWN_TYPE, where, f"expected a type, found {token.kind.lower()}")
    value = token.value
    if value.startswith(":"):
        name = value[1:]
        if name in PRIMITIVES:
            return Prim(name)
        raise diag(UNKNOWN_TYPE, where, f"unknown type '{value}'")
    if len(value) == 1 and "A" <= value <= "Z":
        return TypeVar(value)
    return StructRef(value)


def _count(node: Node, count: int, where: str, message: str) -> None:
    if len(node.items) != count:
        raise diag(UNKNOWN_TYPE, where, message)


def _length(item: "Node | Token", where: str) -> int:
    if isinstance(item, Token) and item.kind == NUMBER and item.value.isdigit():
        return int(item.value)
    raise diag(UNKNOWN_TYPE, where, "an array length must be a non-negative integer")


def _type_list(item: "Node | Token", where: str) -> tuple[Ty, ...]:
    """The type forms beside a function arrow, one per parameter.

    Each entry may be a lone type atom, a composite node, or the ``:`` marker
    the language prints before a composite (``:(vec :i64)``), which the lexer
    splits into its own token. The marker is consumed here so the three
    spellings of one type all land on the same value.
    """
    if not isinstance(item, Node):
        raise diag(UNKNOWN_TYPE, where, "a function type needs a parameter list")
    types: list[Ty] = []
    index = 0
    entries = item.items
    while index < len(entries):
        entry = entries[index]
        if _is_marker(entry):
            if index + 1 >= len(entries):
                raise diag(UNKNOWN_TYPE, where, "expected a type after ':'")
            types.append(parse_type(entries[index + 1], path=where))
            index += 2
        else:
            types.append(parse_type(entry, path=where))
            index += 1
    return tuple(types)


def _is_marker(item: "Node | Token") -> bool:
    """Whether ``item`` is the ``:`` in front of a composite type."""
    return isinstance(item, Token) and item.kind == ATOM and item.value == ":"
