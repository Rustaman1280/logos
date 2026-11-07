"""The type checker: one resolved module in, one checked module out.

Scope
-----
This pass checks what the language has so far: primitives, arithmetic,
comparisons, ``and``/``or``/``not``, ``let``/``letmut``/``set``, ``if``,
``while``, ``match``, ``break``/``continue``, ``ret``, calls, struct field
access, fixed arrays, manual memory, strings, growable vectors, results,
optionals, enums, and threads with their synchronization. ``print`` and
``println`` are the built-in functions: each takes one value and writes it,
formatted by the runtime.
value is any scalar with a formatter of its own — ``:i64``, ``:u64``,
``:f64``, ``:bool``, ``:str`` — and nothing is converted to reach one, so a
``:i32`` is a type mismatch and not an implicit widening.

Strings and vectors
-------------------
``:str`` is a fat pointer of bytes: ``(str-len s)`` is its byte count,
``(str-concat a b)`` builds a new one, ``(str-slice s start end)`` is the bytes
of ``[start, end)``, and ``(str-eq a b)`` is byte equality — the same answer
``=`` gives, because ``=`` accepts ``:str`` too. ``(vec T)`` is a growable
array: it starts empty at ``(make (vec T))``, grows with ``(vec-push v x)``,
reads with ``(vec-len v)`` and ``(vec-get v i)``, and releases its buffer with
``(vec-free v)``. The mutating forms — push, set, free — take a name, because
they rewrite the three-word descriptor that lives in its slot, exactly as
``arr-set`` rewrites the array in its own slot: a ``letmut`` binding, or a
parameter, which the callee owns outright. A parameter is a place for these
three forms and not for ``set``, which still needs ``letmut``. An index that
is not an integer is E0400 and a string operand that is not a ``:str`` is
E0401.

A ``(vec T)`` owns the buffer its descriptor names, so a value that contains one
is never copied: binding it to a second name, setting one, passing it as an
argument, or pushing it into another vector moves it, and the source is empty
afterwards (a later read is E0300). A
second transfer of the same value is **E0402** — there is one buffer and no rule
that would let two names share it. For the same reason a vector may not be a
struct field or a fixed-array element yet: releasing it needs a place expression,
and a field is not one, so such a field would leak by construction. Both are
**E0002** where the type is spelled.

Memory and ownership
--------------------
``(alloc T n)`` has type ``(own T)``: a block of n elements the program must
release with ``(free p)``. ``(ptr p)`` gives the same address as a raw
``(ptr T)``, which may be walked with ``(ptr-add p i)`` and read or written
with ``(deref p)``/``(set-deref p v)``; only the owner may free, so a raw
pointer is E0301 there.

An owning value is not copied, it moves: into a binding, into a call's
``(own T)`` parameter, out of a ``ret``, or through ``free``. The binding left
behind holds nothing, and reading it is E0300 — which is what makes a double
free a compile error in the direct case. A move is only legal out of a
``letmut`` or a parameter (E0302): a plain ``let`` promised to hold the same
value for its whole scope, while ``free`` destroys the value rather than
transferring it and so is allowed on any owning binding.

An owner lives only in a direct binding or a function parameter. It may not be
stored in a struct field, a fixed-array element or an enum payload, and an
owner may not hold another (E0002 where the type is spelled): a place has no
way to move it out, so the owner would either be copied or leak. Neither may a
container hold one: a `(vec T)` or `(arr T n)` whose element type holds an
owner at any depth is E0002, `a container of owners is not supported yet`,
where the container is spelled, because an element can be neither read nor
released and the program would leak by construction. For the same
reason no expression may *read* one out of a place — ``(get b p)`` where the
field is an owner, ``(arr-get a i)``, ``(vec-get v i)`` and ``(deref p)`` of an
owning pointer are **E0402**, ``an owner cannot be read out of a place yet``:
a read copies the pointer, which is the second owner the whole rule exists to
prevent.

A type that *contains* an owner by value — ``(opt (own T))``, ``(res (own T)
E)`` — has no copy either, so binding
it, passing it, returning it or storing it in any construction moves it, and
that move needs no ``letmut`` for the same reason a vector's does not: the
binding is emptied, not reused. A ``match`` consumes a scrutinee that owns
something because its arm binds the payload, and a second transfer is E0402.
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
integer, an enum, a result or an optional, and its arms are ``(pattern
body...)``; an integer pattern is a literal or ``_``, and a variant pattern is
the variant's name or ``(variant binding)``. Every arm must have the same type,
and the last item of a body is the arm's value. An integer match needs no
``_`` — its domain is not enumerable — while the other three are closed sets
and must name every variant (E0600). ``break`` and ``continue`` are statements
with no operand and are only legal inside a loop, which this pass tracks as a
depth on the enclosing environment: a helper ``fn`` starts at zero, because it
is not inside the loop that lexically contains it.

Concurrency
-----------
``(spawn f args...)`` starts ``f`` on a thread and returns ``:thread``; ``f``
is a module function returning ``:void`` and its arguments are copied into the
new thread, so a value that owns memory is E0402. A raw pointer or a handle
crosses only under ``(shared ...)``, which is E0700 when it is missing.
``(join t)`` waits and uses the handle up.

``(mutex)`` gives a ``(shared :mutex)``, which ``lock``/``unlock`` take;
``(chan T n)`` gives a ``(shared (chan T))``, which ``send``/``recv`` use;
``(atomic T v)`` gives a ``(shared (atomic T))`` for ``:i64`` or ``:u64``. The
three handles are only spellable behind the shared marker, and the marker wraps
nothing but a pointer or one of them: ``(shared (ptr T))`` is the marked
address a thread may be handed, and ``deref``/``set-deref``/``ptr-add`` accept
it. A channel copies its elements, so a vector or an owner element is refused;
``spawn`` likewise refuses a move-only argument.

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

Generics
--------
A single capital letter in a signature is a type variable, and a definition
whose signature mentions one is generic:
``(fn map :(vec U) ((a :(vec T)) (f :(fn (T) U))) ...)`` is a function of two
type parameters, read off the signature in the order they first appear.
Nothing is checked inside a generic body until a call fixes its variables: a
call unifies each parameter with the argument beside it, and the copy that
results is checked once per concrete instantiation (monomorphisation). The
copy's name spells its type arguments, ``map$i64$i64``, so two instantiations
of one source function never share a symbol.

Instantiations are discovered by a worklist over call sites, not by recursion
through the checker: a call records what it needs and the body is checked after
the enclosing body finishes, so a generic that calls another generic — or
itself — terminates on the memo key ``(name, type arguments)``. 256 distinct
copies is the ceiling (E0500), and a type variable that no argument binds is
E0501: at the call that cannot fix it, or at an annotation that names a
variable the enclosing definition never declared.

Results and optionals
---------------------
``(res T E)`` is a value of two variants, ``(ok v)`` and ``(err e)``, and
``(opt T)`` is ``(some v)`` or ``(none)``. The error slot names a *declared
enum* — ``(type parse-error (enum (bad-digit :u64) (overflow)))`` — and nothing
else, because ``try`` needs one identity to compare; any other error type is
E0101 where the result is spelled. An enum is a closed set of named variants,
each with no payload (``overflow``) or one (``(bad-digit :u64)``); a variant is
built by its bare name, ``(bad-digit 5)``, and matched by name, ``(bad-digit
n)`` or ``overflow``.

``(ok v)`` and ``(err e)`` take the result type the context expects — a
``ret``'s return type or a ``let``'s annotation — because nothing else could
choose the error enum; without one they are E0100. ``(some v)`` infers its
element from ``v`` and ``(none)`` needs the annotation. ``(try e)`` yields the
ok value of a result, and propagates the error by ``ret``ing it from the
enclosing function: the function must return a result whose error type is
exactly ``e``'s, or the ``try`` is E0601. There is no widening and no union.

A result is an obligation: somebody has to look at its error slot. A ``match``
over one must handle ``ok`` and ``err`` (or a ``_``), a match over an enum must
name every variant, and a match over an optional must handle ``some`` and
``none`` — a missing one is E0600. A result in statement position, or a
binding never matched, tried, returned or handed to a callee, is E0602. The
obligation follows the value through a copy, a capture, a ``ret`` and a call
argument; ``set`` gives the binding a new one. Discharging it is not
path-sensitive — handling a result inside one branch of an ``if`` is enough —
because the question this answers is "was it looked at", not "was it looked at
on every path". A result may not be stored in an aggregate — a struct field, an
array or vector element, an enum payload, another result or an optional — until
a place expression can carry the obligation with it: E0002 where the storage is
spelled, and E0602 for a result handed to ``(ok ...)`` or ``(some ...)``, which
would hide it one level down. Vectors that a tagged value would have to copy
are refused (E0402) for the same reason a vector may not be a struct field.

Closures
--------
``(fn :R ((p :T)...) body...)`` is a value of type ``(fn (T) R)``: the same
signature spelling as a definition, without the name. It captures by value
every outer binding its body reads, and the checker records that list so the
lowerer can build the environment. Capturing copies, so an owning value — an
``(own T)`` or anything holding a ``(vec T)`` — is refused: its environment has
no single owner to release it. A captured name is a fresh, immutable binding
inside the closure, which is why ``set`` on one is E0104 and why a change to
the outer binding after the closure was made is invisible to it.

A captured value is copied into the frame that created the closure, not onto
the heap: the language allocates nothing the source did not spell. That frame
is therefore the closure's lifetime, and the checker enforces it where a value
could leave: returning a capturing closure, storing it in a struct field or an
array element, pushing it into a vector, passing it to another function, or
keeping it past the loop iteration that made it is E0502. The function type
records which kind it is — ``(fn (T) R)`` is an ordinary function pointer,
``(fn* (T) R)`` a closure that captured — and the two are not compatible, so a
plain function-typed parameter can never receive one. Binding a capturing
closure to a local name and calling it in the frame that made it is what is
left, and what a capture is for. A closure that captures nothing is a plain
function pointer and may escape freely.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

from logos.diag import (
    CIRCULAR,
    CLOSURE_ESCAPE,
    DUPLICATE,
    FIELD_TYPE_MISMATCH,
    FREE_NON_OWN,
    INDEX_TYPE,
    INSTANTIATION_LIMIT,
    LITERAL_RANGE,
    MISSING_RETURN,
    MOVE_IMMUTABLE,
    NON_EXHAUSTIVE,
    NOT_A_STR,
    NOT_FOUND,
    NOT_MUTABLE,
    SHARED_MARKER,
    TRY_MISMATCH,
    TYPE_MISMATCH,
    UNBOUND_TYPE_VAR,
    UNHANDLED_RESULT,
    UNKNOWN_FIELD,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    UNKNOWN_TYPE,
    USE_AFTER_MOVE,
    VEC_MOVE,
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
    Atomic,
    Chan,
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
    has_typevar,
    holds_owner,
    holds_vector,
    parse_type,
    substitute,
    typevars,
)


@dataclass(slots=True)
class Types:
    """Everything the checker recorded for one body.

    A module's own functions and its tests share one of these; every
    monomorphised copy has its own, because the same node path is a different
    type in each copy — ``(ret x)`` is ``:i64`` in ``id$i64`` and ``:str`` in
    ``id$str``. The four maps are parallel to :attr:`CheckedModule`'s, and the
    lowerer reads whichever one belongs to the body it is lowering.

    ``declared`` holds each ``let``/``letmut``'s binding type, after
    substitution, because a generic body's annotation names a variable the
    lowerer would otherwise have to substitute a second time.
    """

    expr_types: dict[str, Ty] = field(default_factory=dict)
    call_targets: dict[str, str] = field(default_factory=dict)
    closures: dict[str, "Closure"] = field(default_factory=dict)
    declared: dict[str, Ty] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Closure:
    """One ``(fn ...)`` expression: its signature and what it captured.

    ``captures`` holds the outer bindings the body read, by name and with the
    type copied into the environment, in first-use order. The lowerer builds
    the environment from exactly this list, so the closure body and its
    creation cannot disagree about which slots exist. ``names`` is the
    parameter list's names, in order, as the copy's ``define`` line spells them.
    """

    params: tuple[Ty, ...]
    names: tuple[str, ...]
    ret: Ty
    captures: tuple[tuple[str, Ty], ...]


@dataclass(frozen=True, slots=True)
class Instance:
    """One monomorphised copy of a generic function.

    ``name`` is already mangled — ``map$i64$i64`` — and is the source-level
    name of the copy: the lowerer mangles it exactly as it mangles any other
    function, so no user name can collide with a copy of another instantiation.
    ``body`` is the type record for this copy alone.
    """

    name: str
    fn: str
    args: tuple[Ty, ...]
    params: tuple[Ty, ...]
    ret: Ty
    body: Types


#: The ceiling on distinct monomorphised copies in one program. A 257th
#: instantiation is E0500 rather than an ever-growing binary.
MAX_INSTANCES = 256


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

    ``call_targets`` names the monomorphised copy a call site selected, and
    ``closures`` records each ``(fn ...)`` expression's captures; both are keyed
    by node path, like ``expr_types``. ``instances`` holds the copies
    themselves, in the order the checker discovered them, and ``generic`` names
    the source functions that have no body of their own to lower.
    """

    module: Module
    types: dict[str, Ty]
    fn_sigs: dict[str, FnTy]
    expr_types: dict[str, Ty]
    structs: dict[str, tuple[tuple[str, Ty], ...]] = field(default_factory=dict)
    call_targets: dict[str, str] = field(default_factory=dict)
    closures: dict[str, Closure] = field(default_factory=dict)
    declared: dict[str, Ty] = field(default_factory=dict)
    instances: dict[str, Instance] = field(default_factory=dict)
    generic: frozenset[str] = frozenset()
    enums: dict[str, tuple[tuple[str, Ty | None], ...]] = field(default_factory=dict)


def mangle_instance(name: str, args: tuple[Ty, ...], where: str = "") -> str:
    """The name of one copy: ``map$i64$i64``.

    Each type argument is spelled by :func:`mangle_type`, and the pieces are
    joined with ``$`` — a character no primitive or type form produces, so two
    different argument lists can never spell one name. A copy of
    ``(fn id :T ...)`` at ``:i64`` is therefore ``id$i64`` beside the source
    ``id``, and the lowerer's usual ``lg.<module>.`` mangle puts the two apart.
    ``where`` locates E0501 when an argument is still a type variable.
    """
    return name + "".join("$" + mangle_type(arg, where) for arg in args)


def mangle_type(ty: Ty, where: str = "") -> str:
    """One type argument as an unambiguous name fragment.

    Primitives keep their own spelling (``i64``, ``str``); named types are
    prefixed and length-prefixed — ``s3_i64`` for a struct called ``i64``,
    ``s5_point`` for ``point`` — so a struct can never be mistaken for the
    primitive, and two names that escape to the same text (``a-b`` and
    ``a_x2db`` both contain ``a_x2db``) stay distinct because their lengths
    differ. Composites spell their form around their parts.

    A type variable has no concrete spelling: encoding one would give every
    unbound variable the same fragment and silently merge two copies. It is
    E0501 at ``where`` instead.
    """
    if isinstance(ty, Prim):
        return ty.name
    if isinstance(ty, StructRef):
        return "s" + _name_fragment(ty.name)
    if isinstance(ty, EnumRef):
        return "e" + _name_fragment(ty.name)
    if isinstance(ty, Ptr):
        return f"ptr({mangle_type(ty.elem, where)})"
    if isinstance(ty, Own):
        return f"own({mangle_type(ty.elem, where)})"
    if isinstance(ty, Vec):
        return f"vec({mangle_type(ty.elem, where)})"
    if isinstance(ty, Arr):
        return f"arr({mangle_type(ty.elem, where)},{ty.n})"
    if isinstance(ty, Opt):
        return f"opt({mangle_type(ty.elem, where)})"
    if isinstance(ty, Shared):
        return f"shared({mangle_type(ty.elem, where)})"
    if isinstance(ty, Chan):
        return f"chan({mangle_type(ty.elem, where)})"
    if isinstance(ty, Atomic):
        return f"atomic({mangle_type(ty.elem, where)})"
    if isinstance(ty, Res):
        return f"res({mangle_type(ty.ok, where)},{mangle_type(ty.err, where)})"
    if isinstance(ty, FnTy):
        params = ",".join(mangle_type(param, where) for param in ty.params)
        head = "fn*" if ty.captures else "fn"
        return f"{head}({params}){mangle_type(ty.ret, where)}"
    if isinstance(ty, TypeVar):
        raise diag(
            UNBOUND_TYPE_VAR,
            where,
            f"type variable {ty.name} cannot be a type argument",
        )
    raise diag(UNKNOWN_TYPE, where, f"cannot mangle {ty}")


def _name_fragment(name: str) -> str:
    """Spell a named type injectively: its length, then its escaped bytes.

    ``_escape_name`` alone is lossy — ``a-b`` and ``a_x2db`` both become
    ``a_x2db`` — so the character count goes in front. The decimal length and
    the one ``_`` after it are a delimiter no escaped run can be confused with:
    the escape output is either all safe characters (one per character) or
    contains ``_xHH`` escapes, and either way the first ``_`` ends the digits.
    A decoder reads the length there and knows exactly how many characters the
    remainder must account for, which makes the pair injective.
    """
    return f"{len(name)}_{_escape_name(name)}"


def _escape_name(name: str) -> str:
    """Spell ``name`` with one ``_xHH`` escape per character outside the grammar."""
    if all(char.isascii() and (char.isalnum() or char == "_") for char in name):
        return name
    return "".join(
        char
        if char.isascii() and (char.isalnum() or char == "_")
        else f"_x{ord(char):02x}"
        for char in name
    )


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
    node: Node, types: dict[str, Ty], bindings: dict[str, Ty] | None = None
) -> tuple[str, Ty | None, Node | Token]:
    """A binding's name, its declared type and the item holding its initialiser.

    ``declared`` is ``None`` for the unannotated spelling. The three spellings
    are ``(let x v)``, ``(let x :i64 v)`` and ``(let x :(vec :i64) v)``; the
    third splits into a marker, a list and the initialiser, which is why the
    initialiser's index is not fixed.

    ``bindings`` is the type variables an annotation may name, already
    substituted: the checker passes the enclosing definition's, so a generic
    body's ``:T`` reads as the concrete type of the copy being checked. The
    lowerer passes the same mapping, or ``None`` when it is reading a signature
    that defines its variables rather than using them.
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
    declared, index = _read_type_at(items, 2, types, node.path, bindings)
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
            # A list pattern is ``(variant name)``. Whether the scrutinee has
            # variants at all is the checker's question, because only it knows
            # the scrutinee's type; the shape read here just has to be a form
            # with a name in front.
            head = pattern.items[0] if pattern.items else None
            if not isinstance(head, Token) or head.kind != ATOM:
                raise diag(
                    UNKNOWN_NODE,
                    pattern.path,
                    "a match pattern is a variant name, (variant name) or '_'",
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
    E0002, and an unresolvable type is E0101, each at the field's own path. A
    field that is or contains a ``(vec T)`` is E0002 at the field: a vector is
    released through its descriptor, and a field is not a place this language
    can name, so such a field could never free its buffer. A field that is or
    contains an ``(own T)`` is E0002 for the same reason: an owner lives only
    in a binding or a parameter, and reading one out of a field would copy the
    pointer rather than move it.

    A type variable has nothing to bind it here — a struct declares no type
    parameters — so one in a field, or in the element type of a fixed array in
    a field, is E0501 at that field rather than a layout failure later.
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
        ty = _read_type(type_item, types, field.path, {})
        if holds_owner(ty):
            # An owner lives in a binding or a parameter, nowhere else. A
            # field is not a place this language can move out of: reading it
            # would copy the pointer, so two owners would name one block and
            # the second free would be a use of freed memory. Refused at the
            # declaration, exactly like a vector field.
            raise diag(
                UNKNOWN_NODE, field.path, "owning fields are not supported yet"
            )
        if holds_vector(ty):
            # A vector's buffer is released through the descriptor, and a
            # field is not a place this language can name: ``vec-free`` takes
            # a binding, so a struct that held a vector could never release
            # it. Refused here, at the field, until places exist.
            raise diag(
                UNKNOWN_NODE, field.path, "vector fields are not supported yet"
            )
        if _stored_result(ty):
            # A result is an obligation a name discharges; a field has no name
            # at the point it is written, so the error slot would be dropped.
            raise diag(
                UNKNOWN_NODE, field.path, "result fields are not supported yet"
            )
        fields.append((name, ty))
    return tuple(fields)


#: The one sentence E0502 carries, wherever a capturing closure would outlive
#: the frame that owns its environment.
_CLOSURE_ESCAPE = "a closure that captures cannot leave its function"

#: The one sentence E0402 carries when a place would hand out a second owner.
#: A place expression does not exist yet, so there is no way to move a value
#: out of one: reading it copies the pointer, and two owners of one block free
#: it twice.
_OWNER_PLACE = "an owner cannot be read out of a place yet"

#: The one sentence E0700 carries. State two threads can both reach is marked
#: ``(shared T)`` where it is declared and where it is handed to a thread, so a
#: reader sees it without tracing the calls.
_SHARED_MARKER = "shared state must be marked '(shared ...)'"

#: Variant names the compiler gives a meaning of its own: ``(ok v)``, ``(err
#: e)``, ``(some v)``, ``(none)`` and ``(try e)`` are forms, and ``print`` and
#: ``println`` are the builtins. An enum variant of one of these names could
#: not be constructed unambiguously, so the declaration is refused instead.
RESERVED_VARIANTS = (
    "ok", "err", "some", "none", "try", "print", "println",
    # The concurrency forms are grammar too: a variant called `spawn` could
    # not be constructed, because `(spawn ...)` is a thread.
    "spawn", "join", "mutex", "lock", "unlock", "chan", "send", "recv",
    "atomic", "atomic-add", "atomic-load", "atomic-store",
)


def enum_shape(node: Node, types: dict[str, Ty]) -> tuple[tuple[str, Ty | None], ...]:
    """The variants of an ``(enum ...)`` body, in declaration order.

    One reader because two would drift: the checker validates every enum with
    it and the lowerer reads the same list to put an index on each variant, so
    the tag a constructor writes and the tag a pattern tests cannot disagree.

    A variant is ``(name)`` or ``(name T)``. A repeated name is E0003, a
    malformed variant is E0002, and an unresolvable type is E0101, each at the
    variant's own path. A ``:void`` payload has no storage (E0100), a payload
    that is or contains a ``(vec T)`` is E0002: a vector is released
    through its descriptor and an enum has no place expression, so such a
    variant could never free its buffer. An owning payload is E0002 too, at
    the type that spelled it: a payload is copied where it is built, so an
    owner in one would be two owners of one block.

    The reserved names are E0003 here: ``ok``, ``err``, ``some``, ``none`` and
    ``try`` build and propagate a result or an optional, and ``print`` and
    ``println`` are the builtins, so a variant of one of those names could not
    be told from the form.
    """
    variants: list[tuple[str, Ty | None]] = []
    names: set[str] = set()
    for variant in node.items[1:]:
        if not isinstance(variant, Node) or not variant.items:
            raise diag(
                UNKNOWN_NODE, node.path, "an enum variant must be (name) or (name type)"
            )
        items = variant.items
        name = _name(items[0], variant.path, "a variant")
        if name in RESERVED_VARIANTS:
            raise diag(
                DUPLICATE, variant.path, f"variant '{name}' is reserved by the compiler"
            )
        if name in names:
            raise diag(DUPLICATE, variant.path, f"duplicate variant '{name}'")
        names.add(name)
        if len(items) == 1:
            variants.append((name, None))
            continue
        if len(items) == 2:
            type_item = items[1]
        elif len(items) == 3 and _marker(items[1]):
            type_item = items[2]
        else:
            raise diag(
                UNKNOWN_NODE,
                variant.path,
                "an enum variant must be (name) or (name type)",
            )
        ty = _read_type(type_item, types, variant.path, {})
        if _is_void(ty):
            raise diag(
                TYPE_MISMATCH, variant.path, f"variant '{name}' cannot hold :void"
            )
        if holds_vector(ty):
            raise diag(
                UNKNOWN_NODE, variant.path, "vector payloads are not supported yet"
            )
        if holds_owner(ty):
            # A payload is a place the language cannot move out of by an
            # expression: a match binds it, but an owner inside would have to
            # be read out of the arm's slot to be released, and that read is
            # E0402. Moving a value through a payload is a later task.
            raise diag(
                UNKNOWN_NODE,
                type_item.path if isinstance(type_item, Node) and type_item.path
                else variant.path,
                "owning enum payloads are not supported yet",
            )
        if _stored_result(ty):
            raise diag(
                UNKNOWN_NODE, variant.path, "result payloads are not supported yet"
            )
        variants.append((name, ty))
    if not variants:
        raise diag(UNKNOWN_NODE, node.path, "an enum needs at least one variant")
    return tuple(variants)


def enum_contains_itself(
    name: str,
    enums: dict[str, tuple[tuple[str, Ty | None], ...]],
    structs: dict[str, tuple[tuple[str, Ty], ...]],
) -> bool:
    """Whether enum ``name`` holds itself by value, directly or indirectly.

    An enum has no finite size if one of its payloads leads back to it, and
    the lowerer measures the payload area by walking exactly that graph. The
    walk here is by value only — a pointer, an owning pointer or a vector
    breaks the chain — and a name already on the path is not entered again,
    so a cycle between two other types terminates. The checker refuses the
    declaration before any layout is computed, instead of letting
    ``enum_words``/``sizeof_of`` recurse until Python's stack runs out.
    """
    seen: set[tuple[str, str]] = set()

    def walk(ty: Ty) -> bool:
        if isinstance(ty, EnumRef):
            if ty.name == name:
                return True
            if ("enum", ty.name) in seen:
                return False
            variants = enums.get(ty.name)
            if not variants:
                return False
            seen.add(("enum", ty.name))
            return any(payload is not None and walk(payload) for _, payload in variants)
        if isinstance(ty, StructRef):
            if ("struct", ty.name) in seen:
                return False
            fields = structs.get(ty.name)
            if not fields:
                return False
            seen.add(("struct", ty.name))
            return any(walk(field) for _, field in fields)
        if isinstance(ty, (Arr, Opt)):
            return walk(ty.elem)
        if isinstance(ty, Res):
            return walk(ty.ok) or walk(ty.err)
        return False

    variants = enums.get(name)
    if not variants:
        return False
    seen.add(("enum", name))
    return any(payload is not None and walk(payload) for _, payload in variants)


def pattern_variant(
    pattern: Node | Token,
    variants: tuple[tuple[str, Ty | None], ...] | list[tuple[str, Ty | None]],
    *,
    where: str = "",
    owner: str = "this match",
) -> tuple[str | None, str | None, Ty | None]:
    """The variant a match pattern names, the name it binds and that type.

    One reader because the checker proves coverage with it and the lowerer
    picks a tag with it: a pattern the checker accepted must mean the same
    variant to the backend, or an exhaustive match would test the wrong arms.

    ``_`` is ``(None, None, None)``. A bare name is a payload-less variant —
    ``overflow``. A list is a variant and the name its payload is bound to —
    ``(bad-digit n)``, or ``(bad-digit _)`` to ignore it. The payload's type is
    the variant's, so an arm binds it at exactly the type the enum declared.

    ``where`` locates a diagnostic about a bare token, which carries no path of
    its own; ``owner`` is how the message names the set, ``enum 'x'`` or ``a
    result``.
    """
    if is_wildcard(pattern):
        return (None, None, None)
    table = {name: payload for name, payload in variants}
    if isinstance(pattern, Token):
        if pattern.kind != ATOM:
            raise diag(
                UNKNOWN_NODE,
                where,
                "a match pattern is a variant name, (variant name) or '_'",
            )
        name = pattern.value
        if name not in table:
            raise diag(TYPE_MISMATCH, where, f"{owner} has no variant '{name}'")
        if table[name] is not None:
            raise diag(
                TYPE_MISMATCH,
                where,
                f"variant '{name}' holds a value: write ({name} name)",
            )
        return (name, None, None)
    items = pattern.items
    head = items[0] if items else None
    if not isinstance(head, Token) or head.kind != ATOM:
        raise diag(
            UNKNOWN_NODE,
            pattern.path or where,
            "a match pattern is a variant name, (variant name) or '_'",
        )
    name = head.value
    at = pattern.path or where
    if name not in table:
        raise diag(TYPE_MISMATCH, at, f"{owner} has no variant '{name}'")
    payload = table[name]
    if payload is None:
        # ``(none)`` is the same pattern as ``none``, so a variant that takes
        # no value may be written the way it is constructed.
        if len(items) != 1:
            raise diag(
                WRONG_ARITY, at, f"variant '{name}' takes no value: write {name}"
            )
        return (name, None, None)
    if len(items) != 2:
        raise diag(
            WRONG_ARITY, at, f"a pattern for variant '{name}' binds exactly one name"
        )
    bound = items[1]
    if not isinstance(bound, Token) or bound.kind != ATOM:
        raise diag(UNKNOWN_NODE, at, "a variant pattern binds one name or '_'")
    if bound.value == "_":
        return (name, None, payload)
    if bound.value.startswith(":"):
        raise diag(UNKNOWN_NODE, at, "a variant pattern binds a name, not a type")
    return (name, bound.value, payload)


@dataclass(slots=True)
class _Var:
    """One binding: its type, and what the checker knows about its value.

    ``moved`` is the ownership state of an owning binding — an ``(own T)`` or a
    value that contains a ``(vec T)``: once its value has been transferred or
    released, the binding holds nothing and any read of it is E0300.
    ``param`` records that the name arrived as a parameter, which
    the callee owns outright — a parameter may be moved on, while a plain
    ``let`` needs ``letmut`` before its value can be transferred (E0302).

    ``decl_loop`` is how many loops enclosed the declaration and
    ``origin_loop`` how many enclosed the value now stored. They differ only
    for a capturing closure: one made inside a loop and stored in a binding
    declared outside it would outlive its iteration, when the environment slot
    is overwritten by the next pass (E0502).

    ``handled`` is the obligation a ``(res T E)`` carries: the value was made,
    so somebody has to look at its error slot. ``match``, ``try``, ``ret`` and
    handing it to a callee all discharge it; letting the block that declared it
    end first is E0602 at ``path``. ``path`` is the binding's own node, so the
    diagnostic points at the declaration and not at the block that ends.
    """

    ty: Ty
    mutable: bool
    moved: bool = False
    param: bool = False
    decl_loop: int = 0
    origin_loop: int = 0
    handled: bool = False
    path: str = ""


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

    ``outer`` is set on a closure's environment. A name the closure's own
    scopes do not hold is looked up there, and finding one *captures* it: the
    closure gets a fresh copy of the binding, and the capture list grows by
    that name. A copy is what makes capture by value visible in the types —
    the closure may read the name for as long as it exists, but nothing it does
    to the copy reaches the binding outside.

    ``tvars`` is the substitution the enclosing definition's type variables
    stand for: empty outside a generic, the concrete arguments inside an
    instantiation. ``structs`` and ``enums`` are the layout namespace, so a
    captured value that holds a ``(vec T)`` or an ``(own T)`` — however deeply
    nested — is recognised as one.
    """

    def __init__(
        self,
        fns: dict[str, FnTy],
        *,
        ret: Ty,
        label: str,
        outer: "_Env | None" = None,
        tvars: dict[str, Ty] | None = None,
        structs: dict[str, tuple[tuple[str, Ty], ...]] | None = None,
        enums: dict[str, tuple[tuple[str, Ty | None], ...]] | None = None,
    ) -> None:
        self.fns = dict(fns)
        self.scopes: list[dict[str, _Var]] = [{}]
        self.ret = ret
        self.label = label
        #: How many loops enclose the form being checked. ``break`` and
        #: ``continue`` are legal only when it is non-zero.
        self.loop = 0
        self.outer = outer
        self.tvars = dict(tvars or {})
        self.structs = structs or {}
        self.enums = enums or {}
        #: The names this closure copied out of ``outer``, in first-use order.
        self.captures: list[tuple[str, Ty]] = []

    @contextmanager
    def block(self) -> Iterator[None]:
        """Check the form inside ``with`` as one nested block.

        Leaving the block is where an unhandled result is caught: a ``(res
        T E)`` declared in it and never matched, tried, returned or passed on
        is E0602 at its declaration. The check runs only on the normal path —
        an exception on the way out is already a diagnostic.
        """
        self.scopes.append({})
        try:
            yield
        except BaseException:
            self.scopes.pop()
            raise
        self._pending(self.scopes.pop())

    def _pending(self, scope: dict[str, "_Var"]) -> None:
        """Report the first result binding in ``scope`` that was never handled."""
        for name, var in scope.items():
            if isinstance(var.ty, Res) and not var.handled:
                raise diag(
                    UNHANDLED_RESULT,
                    var.path,
                    f"the result of '{name}' is never handled: match it or 'try' it",
                )

    def finish(self) -> None:
        """Check the function block's own results, which no ``block`` closes."""
        self._pending(self.scopes[0])

    def define(
        self,
        name: str,
        ty: Ty,
        *,
        mutable: bool,
        path: str,
        param: bool = False,
        handled: bool = False,
    ) -> None:
        block = self.scopes[-1]
        if name in block:
            raise diag(DUPLICATE, path, f"duplicate definition '{name}'")
        block[name] = _Var(
            ty, mutable, param=param, decl_loop=self.loop, handled=handled, path=path
        )

    def find(self, name: str) -> _Var | None:
        """The binding ``name`` holds in this environment, without capturing."""
        for block in reversed(self.scopes):
            var = block.get(name)
            if var is not None:
                return var
        return None

    def lookup(self, name: str, where: str = "") -> _Var | None:
        """The binding ``name`` stands for, capturing it if this is a closure.

        ``where`` locates the diagnostic a refused capture raises. A capture is
        a copy, so an owning value cannot be one: the environment would be a
        second owner of a block nothing could release, and reading the outer
        binding afterwards would name memory two environments own.
        """
        for block in reversed(self.scopes):
            var = block.get(name)
            if var is not None:
                return var
        if self.outer is None:
            return None
        found = self.outer.lookup(name, where)
        if found is None:
            return None
        if found.moved:
            raise diag(USE_AFTER_MOVE, where, f"use after move of '{name}'")
        if (
            isinstance(found.ty, Own)
            or holds_vector(found.ty, self.structs)
            or holds_owner(found.ty, self.structs, self.enums)
        ):
            # A closure environment holds copies. An owning value has no copy,
            # and an environment no statement can name could never release the
            # one it would otherwise share.
            code = VEC_MOVE if holds_vector(found.ty, self.structs) else TYPE_MISMATCH
            raise diag(
                code,
                where,
                f"cannot capture '{name}' of type {found.ty} in a closure: "
                "its environment holds copies",
            )
        copy = _Var(
            found.ty,
            mutable=False,
            param=True,
            origin_loop=found.origin_loop,
            # The obligation of a result moves into the closure with the copy,
            # and the outer binding is discharged by it: the closure's body is
            # then checked for handling what it captured.
            handled=not isinstance(found.ty, Res),
            path=found.path,
        )
        if isinstance(found.ty, Res):
            found.handled = True
        self.scopes[0][name] = copy
        self.captures.append((name, found.ty))
        return copy

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

    ``own_*`` are this module's definitions; ``types``, ``fns``, ``structs`` and
    ``enums`` add the own definitions of each direct import, and nothing deeper,
    because an import is not transitive.
    """

    own_types: dict[str, Ty]
    own_fns: dict[str, FnTy]
    own_structs: dict[str, tuple[tuple[str, Ty], ...]]
    own_enums: dict[str, tuple[tuple[str, Ty | None], ...]]
    types: dict[str, Ty]
    fns: dict[str, FnTy]
    structs: dict[str, tuple[tuple[str, Ty], ...]]
    enums: dict[str, tuple[tuple[str, Ty | None], ...]]


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

#: The forms that build and propagate a result or an optional: ``(ok v)``,
#: ``(err e)``, ``(some v)``, ``(none)`` and ``(try e)``. Like the printers they
#: live outside the function namespace, and a user definition of one of these
#: names shadows the form.
RESULT_FORMS = ("ok", "err", "some", "none", "try")

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

_TYPE_FORMS = ("arr", "vec", "ptr", "own", "fn", "fn*", "res", "opt", "shared",
               "chan", "atomic")
_LET_SHAPE = "takes a name, an optional type and a value"


class _Checker:
    def __init__(self, module: Module) -> None:
        self.module = module
        self.expr_types: dict[str, Ty] = {}
        self.call_targets: dict[str, str] = {}
        self.closures: dict[str, Closure] = {}
        self.declared: dict[str, Ty] = {}
        self._types: dict[str, Ty] = {}
        self._structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
        self._enums: dict[str, tuple[tuple[str, Ty | None], ...]] = {}
        #: Every variant name a visible enum declares, and the enums that
        #: declare it: the constructor namespace, spelled bare.
        self._variants: dict[str, list[tuple[str, Ty | None]]] = {}
        self._seen: dict[str, _Visible] = {}
        self._active: list[str] = []
        #: Which body's record the checker is writing: the module's own, or one
        #: instantiation's. Swapped around each copy's body, because the same
        #: node path is a different type in each.
        self._record = Types(
            self.expr_types, self.call_targets, self.closures, self.declared
        )
        #: The source functions a call may instantiate. Only this module's own
        #: definitions, because a copy's body is emitted from here; a generic
        #: reached through an import has no body this module can lower.
        self._bodies: dict[str, Node] = {}
        #: The copy each (name, type arguments) pair made, in discovery order.
        self._instances: dict[str, Instance] = {}
        #: Copies discovered but not yet checked. A worklist rather than
        #: recursion, so a generic that calls another (or itself) terminates on
        #: memoisation instead of nesting one body check per call site.
        self._pending: list[str] = []
        #: Each generic's type parameters, in signature order.
        self._params: dict[str, tuple[str, ...]] = {}

    def run(self) -> CheckedModule:
        visible = self._visible(self.module)
        self._types = visible.types
        self._structs = visible.structs
        self._enums = visible.enums
        for enum_name, variants in visible.enums.items():
            for variant, payload in variants:
                # A variant name is a constructor: one visible enum declaring
                # it is what `(bad-digit 5)` means. Two is a question only the
                # source can answer, so the ambiguity is reported where the
                # constructor is used.
                self._variants.setdefault(variant, []).append((enum_name, payload))
        self._bodies = {name: node for name, node in self.module.fns.items()}
        for node in self.module.fns.values():
            self._check_fn(node, visible.fns)
        for node in self.module.tests.values():
            self._check_test(node, visible.fns)
        self._drain(visible.fns)
        return CheckedModule(
            self.module,
            dict(visible.types),
            dict(visible.fns),
            self.expr_types,
            dict(visible.structs),
            self.call_targets,
            self.closures,
            self.declared,
            dict(self._instances),
            frozenset(self._params),
            dict(visible.enums),
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
            inherited_enums: dict[str, tuple[tuple[str, Ty | None], ...]] = {}
            for imported in module.imports.values():
                # Only the import's own names cross the boundary: a module
                # never inherits its imports' imports.
                visible = self._visible(imported)
                inherited.update(visible.own_types)
                inherited_fns.update(visible.own_fns)
                inherited_structs.update(visible.own_structs)
                inherited_enums.update(visible.own_enums)
            own_types = {
                name: _declared(name, node) for name, node in module.types.items()
            }
            types = dict(inherited)
            types.update(own_types)
            # Field types resolve against the complete namespace, so a struct
            # may name a type declared after it. The fields themselves are read
            # second, once every name they might mention exists.
            own_structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
            own_enums: dict[str, tuple[tuple[str, Ty | None], ...]] = {}
            for name, node in module.types.items():
                body = node.items[2] if len(node.items) > 2 else None
                if isinstance(body, Node) and body.kind == "struct":
                    own_structs[name] = struct_shape(body, types)
                elif isinstance(body, Node) and body.kind == "enum":
                    own_enums[name] = enum_shape(body, types)
            structs = dict(inherited_structs)
            structs.update(own_structs)
            enums = dict(inherited_enums)
            enums.update(own_enums)
            for name, node in module.types.items():
                if name in own_enums and enum_contains_itself(name, enums, structs):
                    # An enum payload area is sized by measuring its payloads,
                    # so a payload that leads back here would be measured
                    # forever. Refused at the declaration, before any layout.
                    raise diag(
                        UNKNOWN_NODE, node.path, f"enum '{name}' contains itself"
                    )
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
            visible = _Visible(
                own_types, own_fns, own_structs, own_enums, types, fns, structs, enums
            )
        finally:
            self._active.pop()
        self._seen[module.name] = visible
        return visible

    def _signature(self, node: Node, types: dict[str, Ty]) -> FnTy:
        """A ``fn``'s type, and its type parameters when it has any.

        A type variable in a signature is what declares it: there is no
        separate parameter list, so the signature is read with every variable
        accepted, and the ones it mentions — return type first, then the
        parameters left to right — become the copy's type arguments.
        """
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        ret, params, _ = fn_shape(node, types)
        sig = FnTy(tuple(ty for _, ty in params), ret)
        order = typevars(ret)
        for param in sig.params:
            for var in typevars(param):
                if var not in order:
                    order.append(var)
        if order:
            if name == "main":
                # The entry point is called by the system with no arguments, so
                # nothing could ever fix its type variables.
                raise diag(
                    UNKNOWN_TYPE,
                    node.path,
                    "main cannot have type variables: nothing fixes them",
                )
            self._params[name] = tuple(order)
        return sig

    # -- bodies ----------------------------------------------------------

    def _check_fn(self, node: Node, fns: dict[str, FnTy]) -> None:
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        sig = fns.get(name)
        if sig is None:
            raise diag(UNKNOWN_NODE, node.path, f"no signature for '{name}'")
        if name in self._params:
            # A generic body has no single type: it is checked once per copy,
            # after a call has fixed its type arguments (see ``_instantiate``).
            return
        env = _Env(
            fns,
            ret=sig.ret,
            label=f"function '{name}'",
            structs=self._structs,
            enums=self._enums,
        )
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
        env = _Env(
            fns,
            ret=Prim("void"),
            label=f"test '{name}'",
            structs=self._structs,
            enums=self._enums,
        )
        for item in node.items[2:]:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, node.path, "a test body must be a form")
            if item.kind in DIRECTIVES:
                continue
            if item.kind == "fn":
                self._check_helper(item, env)
                continue
            self._statement(item, env)
        env.finish()

    def _check_helper(self, node: Node, env: _Env) -> None:
        """A ``fn`` private to one test: same rules, test-local visibility."""
        name = node.name
        if name is None or name.startswith(":"):
            raise diag(UNKNOWN_NODE, node.path, "fn needs a name")
        if name in env.fns:
            raise diag(DUPLICATE, node.path, f"duplicate definition '{name}'")
        sig = self._signature(node, self._types)
        env.fns[name] = sig
        inner = _Env(
            env.fns,
            ret=sig.ret,
            label=f"function '{name}'",
            tvars=env.tvars,
            structs=self._structs,
            enums=self._enums,
        )
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            inner.define(
                param_name,
                sig.params[index],
                mutable=False,
                path=param.path,
                param=True,
            )
        if name in self._params:
            # A generic helper's body is checked per copy like any other, but a
            # copy of a helper has nowhere to be emitted — helpers are not
            # module functions — so a call to one is refused (E0103) rather
            # than accepted and then refused by the backend.
            return
        self._body(node, _body_start(node), inner)

    def _body(self, node: Node, start: int, env: _Env) -> None:
        statements = node.items[start:]
        self._statements(statements, env, node.path)

    def _statements(
        self,
        statements: tuple[Node | Token, ...] | list[Node | Token],
        env: _Env,
        path: str,
    ) -> None:
        """Check a run of statements, and require a ``ret`` when one is owed.

        Shared by a function body, a test helper and a closure body: all three
        are a block of forms whose last promise is the declared return type.
        The block's own result bindings are discharged here too, because the
        function block is the one scope no ``with env.block()`` ever closes.
        """
        for item in statements:
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, path, "a statement must be a form")
            self._statement(item, env)
        if env.ret != Prim("void") and not _contains_ret(statements):
            raise diag(
                MISSING_RETURN,
                path,
                f"{env.label} must return {env.ret} but has no 'ret'",
            )
        env.finish()

    def _statement(self, node: Node, env: _Env) -> Ty:
        """Check one statement, refusing a result that is thrown away.

        A ``(res T E)`` in statement position — ``(parse x)`` on its own, an
        ``if`` whose arms produce results — is the value the error slot exists
        for, dropped on the floor. E0602 says so here, where the shape is
        visible; a binding is caught when its block ends.
        """
        ty = self._expr(node, env)
        if isinstance(ty, Res):
            raise diag(
                UNHANDLED_RESULT,
                node.path,
                f"this {ty} is never handled: match it or 'try' it",
            )
        return ty

    # -- expressions -----------------------------------------------------

    def _expr(self, node: Node, env: _Env, expected: Ty | None = None) -> Ty:
        ty = self._dispatch(node, env, expected)
        self._record.expr_types[node.path] = ty
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
        if kind == "break" or kind == "continue":
            return self._loop_jump(node, env)
        if kind == "fn":
            return self._closure(node, env)
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
        if kind == "spawn":
            return self._spawn(node, env)
        if kind == "join":
            return self._join(node, env)
        if kind == "mutex":
            return self._mutex(node, env)
        if kind == "lock":
            return self._mutex_op(node, env, "lock")
        if kind == "unlock":
            return self._mutex_op(node, env, "unlock")
        if kind == "chan":
            return self._chan(node, env)
        if kind == "send":
            return self._send(node, env)
        if kind == "recv":
            return self._recv(node, env)
        if kind == "atomic":
            return self._atomic(node, env)
        if kind == "atomic-add":
            return self._atomic_add(node, env)
        if kind == "atomic-load":
            return self._atomic_load(node, env)
        if kind == "atomic-store":
            return self._atomic_store(node, env)
        head = node.items[0] if node.items else None
        if isinstance(head, Token) and head.kind == ATOM:
            return self._call(node, env, expected)
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
            var = env.lookup(token.value, where)
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
            bare = env.fns.get(token.value)
            if bare is not None:
                # A function's name is a value of its own type: passing one
                # needs no closure, and a copy of the environment pointer is
                # what the callee calls through.
                if has_typevar(bare.ret) or any(
                    has_typevar(param) for param in bare.params
                ):
                    raise diag(
                        UNBOUND_TYPE_VAR,
                        where,
                        f"{token.value} is generic: a call fixes its type variables",
                    )
                if token.value not in self._bodies:
                    # The lowerer emits a ``$fnvalue`` wrapper beside the
                    # definition it wraps. An imported (or test-local) name has
                    # no definition in this module, so there is no wrapper to
                    # call and lowering would fail with E0103 after checking.
                    raise diag(
                        UNKNOWN_FUNCTION,
                        where,
                        f"cannot use '{token.value}' as a function value: only "
                        "a function defined in this module can be passed across "
                        "a module boundary",
                    )
                return bare
            variant = self._variant_type(token.value, where)
            if variant is not None:
                # A payload-less variant is a value wherever a name is: `let e
                # overflow` is the same value `(overflow)` builds.
                enum, payload = variant
                if payload is not None:
                    raise diag(
                        TYPE_MISMATCH,
                        where,
                        f"variant '{token.value}' holds a value: "
                        f"write ({token.value} name)",
                    )
                return EnumRef(enum)
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
        name, declared, init = let_shape(node, self._types, env.tvars)
        self._transfer(init, env, node.path)
        ty = self._value(init, env, declared, node.path)
        if declared is not None and not _fits(declared, ty):
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
        if self._move_only(ty):
            # An owning pointer is not copied into a second binding, and
            # neither is a value that contains one or a vector anywhere: the
            # initialiser's value moves here, and the old name is empty.
            self._consume(init, env, node.path)
        bound = declared if declared is not None else ty
        if isinstance(declared, FnTy) and isinstance(ty, FnTy):
            # An annotation spells the signature, never the capture flag:
            # whether the value captures is a property of the value, and the
            # binding keeps it. Dropping it here would let `(ret f)` read as
            # an ordinary function pointer.
            bound = FnTy(declared.params, declared.ret, ty.captures)
        # The binding's type, after substitution: a generic body's annotation
        # names a type variable, and the lowerer must not read the variable.
        self._record.declared[node.path] = bound
        origin = self._origin_loop(init, env, ty)
        env.define(
            name,
            bound,
            mutable=mutable,
            path=node.path,
        )
        env.scopes[-1][name].origin_loop = origin
        if isinstance(bound, Res):
            # The obligation follows the value: a result copied into a second
            # binding is the new binding's to handle, and the source is not
            # reported for it.
            self._handled(init, env)
        return Prim("void")

    def _set(self, node: Node, env: _Env) -> Ty:
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "set takes a name and a value")
        name = _name(items[1], node.path, "set")
        var = env.lookup(name, node.path)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if not var.mutable:
            raise diag(NOT_MUTABLE, node.path, f"cannot set immutable binding '{name}'")
        self._transfer(items[2], env, node.path)
        value = self._value(items[2], env, var.ty, node.path)
        if not _fits(var.ty, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"cannot set '{name}' of type {var.ty} to {value}",
            )
        if isinstance(var.ty, Res) and not var.handled:
            # The old value's error slot is this binding's obligation, and an
            # assignment would overwrite it: the caller must have looked at it
            # before there is a new one to look at.
            raise diag(
                UNHANDLED_RESULT,
                node.path,
                f"cannot overwrite '{name}': its result was never handled",
            )
        if self._move_only(var.ty):
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
        if isinstance(var.ty, FnTy) and isinstance(value, FnTy):
            # The flag follows the value, not the annotation: an assignment
            # of a non-capturing function makes the binding ordinary again.
            var.ty = FnTy(var.ty.params, var.ty.ret, value.captures)
        origin = self._origin_loop(items[2], env, value)
        if _escapes(value) and origin > var.decl_loop:
            # The closure was made inside a loop and stored in a binding
            # declared outside it. Its environment slot is rewritten on the
            # next pass, so the stored value would read another iteration's
            # copies.
            raise diag(CLOSURE_ESCAPE, node.path, _CLOSURE_ESCAPE)
        var.origin_loop = origin
        if isinstance(var.ty, Res):
            # A new result replaces the old one: the obligation is the new
            # value's, so the binding is pending again.
            var.handled = False
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
        if not _fits(then, otherwise):
            raise diag(
                TYPE_MISMATCH,
                _at(args[2], node),
                f"if branches do not match: {then} and {otherwise}",
            )
        then = _merge_functions(then, otherwise)
        if self._move_only(then):
            # R7/R6: the result would be a second descriptor for a buffer — or
            # a second owner of a block — one of the arms still owns, so
            # freeing it would free it twice. Form moves are what would make
            # the arms give the value up; until then the choice is refused.
            raise diag(
                VEC_MOVE,
                node.path,
                "a vector cannot be selected by if or match"
                if holds_vector(then, self._structs)
                else "an owning value cannot be selected by if or match",
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
                        self._statement(statement, env)
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
        """``match``: an integer, an enum, a result or an optional.

        An integer's arms are literals because no finite list of them covers
        the domain; a fall-through is the runtime's R0003. An enum, a result
        and an optional are *closed* — their variants are declared — so the
        same fall-through is statically impossible and a missing arm is E0600.
        A ``_`` arm covers whatever is left, which is why a match on one of
        them may be written either way.

        A result is a two-variant set — ``(ok v)`` and ``(err e)`` — and an
        optional is ``(some v)`` and ``(none)``: the same reader handles all
        three, so a match cannot be exhaustive for one and not the other.
        """
        scrutinee, arms = match_shape(node)
        ty = self._value(scrutinee, env, None, node.path)
        if isinstance(ty, Prim) and ty.name in INTEGERS:
            return self._integer_match(node, env, expected, ty, arms)
        if self._move_only(ty):
            # An arm binds its payload by copy, so a scrutinee that owns
            # something gives it up: exactly one arm runs and the binding it
            # makes holds the only owner afterwards.
            self._consume(scrutinee, env, node.path)
        variants = self._variants_of(ty, node)
        # Whichever family this is, every arm's pattern is read once here and
        # the plan is reused: the tag the arm tests and the name it binds come
        # from the same reading.
        plans: list[tuple[str | None, tuple[str, Ty, str] | None]] = []
        seen: set[str] = set()
        for pattern, _ in arms:
            name, binding, payload = pattern_variant(
                pattern, variants, where=node.path, owner=_owner(ty)
            )
            if name is not None:
                if name in seen:
                    raise diag(
                        UNKNOWN_NODE,
                        _at(pattern, node),
                        f"an arm for '{name}' can never match",
                    )
                seen.add(name)
            plans.append(
                (
                    name,
                    (binding, payload, _at(pattern, node)) if binding else None,
                )
            )
        if not any(is_wildcard(pattern) for pattern, _ in arms):
            missing = [name for name, _ in variants if name not in seen]
            if missing:
                raise diag(
                    NON_EXHAUSTIVE,
                    node.path,
                    f"this match on {ty} does not handle "
                    + ", ".join(f"'{name}'" for name in missing),
                )
        if isinstance(ty, Res):
            # A matched result has been looked at; the obligation is gone even
            # if only one arm could run in practice.
            self._handled(scrutinee, env)
        branches: list[Callable[[], Ty]] = []
        for (_, body), plan in zip(arms, plans):
            branches.append(
                lambda body=body, plan=plan: self._arm(
                    body, env, expected, node, plan[1]
                )
            )
        return self._arm_types(env, branches, node)

    def _integer_match(
        self,
        node: Node,
        env: _Env,
        expected: Ty | None,
        ty: Ty,
        arms: list[tuple[Node | Token, tuple[Node | Token, ...]]],
    ) -> Ty:
        """A match over integers: literals, ``_``, and the R0003 fall-through."""
        branches: list[Callable[[], Ty]] = []
        for pattern, body in arms:
            self._pattern(pattern, ty, node)
            branches.append(
                lambda body=body: self._arm(body, env, expected, node, None)
            )
        return self._arm_types(env, branches, node)

    def _arm_types(
        self, env: _Env, branches: list[Callable[[], Ty]], node: Node
    ) -> Ty:
        """Check a match's arms as one statement, and give the match their type.

        Every arm is checked from the pre-match state and the arms' moves are
        unioned at the join: exactly one arm runs, but which one is not known
        here, so a value freed in any arm is gone afterwards.
        """
        result: Ty | None = None
        # ``arms`` is non-empty: ``match_shape`` refuses a match without one.
        for arm in self._branches(env, branches):
            if result is None:
                result = arm
            elif not _fits(result, arm):
                raise diag(
                    TYPE_MISMATCH,
                    node.path,
                    f"match arms do not agree: {result} and {arm}",
                )
            else:
                result = _merge_functions(result, arm)
        if result is not None and self._move_only(result):
            # R7/R6, the same rule as ``if``: an arm still owns the buffer or
            # the block the result names, so the result would be a second
            # owner.
            raise diag(
                VEC_MOVE,
                node.path,
                "a vector cannot be selected by if or match"
                if holds_vector(result, self._structs)
                else "an owning value cannot be selected by if or match",
            )
        return result  # type: ignore[return-value]

    def _variants_of(
        self, ty: Ty, node: Node
    ) -> tuple[tuple[str, Ty | None], ...]:
        """The closed set of variants a scrutinee of ``ty`` can be."""
        if isinstance(ty, EnumRef):
            variants = self._enums.get(ty.name)
            if variants is None:
                raise diag(UNKNOWN_TYPE, node.path, f"unknown type '{ty.name}'")
            return variants
        if isinstance(ty, Res):
            return (("ok", ty.ok), ("err", ty.err))
        if isinstance(ty, Opt):
            return (("some", ty.elem), ("none", None))
        raise diag(
            TYPE_MISMATCH,
            node.path,
            f"'match' needs an integer, an enum, a result or an optional, found {ty}",
        )

    def _pattern(self, pattern: Node | Token, ty: Ty, owner: Node) -> None:
        """One arm's pattern: an integer literal that fits ``ty``, or ``_``."""
        where = _at(pattern, owner)
        if not isinstance(pattern, Token):
            raise diag(
                UNKNOWN_NODE,
                where,
                f"'{ty}' has no variants: a match pattern is an integer literal or '_'",
            )
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
        binding: tuple[str, Ty, str] | None = None,
    ) -> Ty:
        """The type of one arm: its last item, after the statements before it.

        The arm is one block: a ``let`` in it is visible to the items after it
        and to nothing outside the arm, and so is a payload a variant pattern
        bound. It is a fresh, immutable binding at exactly the variant's type,
        which is what makes ``(bad-digit n)`` read ``n`` as the ``:u64`` the
        enum declared.
        """
        with env.block():
            if binding is not None:
                name, ty, path = binding
                env.define(name, ty, mutable=False, path=path)
            for statement in body[:-1]:
                if not isinstance(statement, Node):
                    raise diag(UNKNOWN_NODE, owner.path, "a statement must be a form")
                self._statement(statement, env)
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
        if _escapes(value):
            # The environment lives in this frame. Returning the closure hands
            # a caller a pointer to a slot that dies when this frame does.
            raise diag(CLOSURE_ESCAPE, _at(args[0], node), _CLOSURE_ESCAPE)
        if not compatible(env.ret, value):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"{env.label} returns {env.ret}, found {value}",
            )
        if self._move_only(env.ret):
            # Returning an owning value hands it to the caller.
            self._consume(args[0], env, node.path)
        if isinstance(env.ret, Res):
            # The value leaves the function: its error slot is the caller's to
            # handle, which is exactly what the annotated return type says.
            self._handled(args[0], env)
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
        ty, index = _read_type_at(items, 1, self._types, node.path, env.tvars)
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
                if _escapes(got):
                    # A field outlives the expression, so a capturing closure
                    # stored here would be called after its frame is gone.
                    raise diag(CLOSURE_ESCAPE, _at(item, node), _CLOSURE_ESCAPE)
                if not compatible(fty, got):
                    raise diag(
                        FIELD_TYPE_MISMATCH,
                        _at(item, node),
                        f"field '{field}' of '{ty.name}' expects {fty}, found {got}",
                    )
                if self._move_only(fty):
                    self._consume(item, env, node.path)
            return ty
        if isinstance(ty, Arr):
            if holds_owner(ty.elem, self._structs, self._enums):
                # An owner lives in a binding or a parameter, not in an
                # element: ``(arr-get a i)`` cannot move one out, so an array
                # of owners would leak by construction. Refused where it is
                # built, at the type that spelled it.
                raise diag(
                    UNKNOWN_NODE,
                    _at(items[1], node),
                    "owning array elements are not supported yet",
                )
            if len(values) != ty.n:
                raise diag(
                    WRONG_ARITY,
                    node.path,
                    f"array '{ty}' takes {_count(ty.n, 'element')}, "
                    f"got {len(values)}",
                )
            for item in values:
                got = self._value(item, env, ty.elem, node.path)
                if _escapes(got):
                    # An array element is storage the frame keeps: the value
                    # would be callable after the closure's frame is gone.
                    raise diag(CLOSURE_ESCAPE, _at(item, node), _CLOSURE_ESCAPE)
                if not compatible(ty.elem, got):
                    raise diag(
                        TYPE_MISMATCH,
                        _at(item, node),
                        f"cannot store {got} in {ty}",
                    )
                if self._move_only(ty.elem):
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
                if holds_owner(fty, self._structs, self._enums):
                    # An owning field is E0002 at its declaration, so this is
                    # the invariant stated at the read site: a place read has
                    # no way to move an owner out, only to copy the pointer.
                    raise diag(VEC_MOVE, node.path, _OWNER_PLACE)
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
        var = env.lookup(name, node.path)
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
            if _escapes(got):
                raise diag(
                    CLOSURE_ESCAPE, _at(items[3], node), _CLOSURE_ESCAPE
                )
            if not compatible(fty, got):
                raise diag(
                    FIELD_TYPE_MISMATCH,
                    _at(items[3], node),
                    f"field '{field}' of '{var.ty.name}' expects {fty}, found {got}",
                )
            if self._move_only(fty):
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
        if holds_owner(ty.elem, self._structs, self._enums):
            # An element of a fixed array is a place: the read would copy the
            # owning pointer out of it, and both copies would free the block.
            raise diag(VEC_MOVE, node.path, _OWNER_PLACE)
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
        var = env.lookup(name, node.path)
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
        if _escapes(got):
            raise diag(CLOSURE_ESCAPE, _at(items[3], node), _CLOSURE_ESCAPE)
        if not compatible(var.ty.elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(items[3], node),
                f"cannot store {got} in {var.ty}",
            )
        if self._move_only(var.ty.elem):
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
        """The mutable vector place a mutating vector form writes through.

        A push may reallocate, so it changes the descriptor; a set writes
        through the buffer but the descriptor stays; a free zeroes it. Either
        way the target is a binding or a parameter rather than a temporary —
        a temporary has no storage the new descriptor could be written back to
        — and it needs ``letmut`` unless it is a parameter, which the callee
        owns outright: the caller gave up the descriptor at the call, so the
        callee is the one that has to be able to release the buffer.

        ``set`` is deliberately not in that list (it refuses parameters with
        E0104): a parameter's assignment promises something the caller never
        sees, and a vector hand-off is a move, not a shared place.
        """
        items = node.items
        if len(items) != arity:
            raise diag(WRONG_ARITY, node.path, shape)
        if not isinstance(items[1], Token) or items[1].kind != ATOM:
            raise diag(WRONG_ARITY, node.path, shape)
        name = _name(items[1], node.path, node.kind)
        var = env.lookup(name, node.path)
        if var is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{name}'")
        if var.moved:
            # The value was moved or released, so the descriptor holds
            # nothing: writing through this name would touch a buffer another
            # binding owns now.
            raise diag(USE_AFTER_MOVE, node.path, f"use after move of '{name}'")
        if not var.mutable and not var.param:
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
            shape="vec-push takes a vector name and a value",
            verb="push to",
        )
        got = self._value(node.items[2], env, ty.elem, node.path)
        if _escapes(got):
            # The buffer outlives the frame that holds the environment, so a
            # capturing closure pushed here would be called through a dead
            # pointer after its creator returns.
            raise diag(CLOSURE_ESCAPE, _at(node.items[2], node), _CLOSURE_ESCAPE)
        if not compatible(ty.elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(node.items[2], node),
                f"cannot push {got} into {ty}",
            )
        if self._move_only(ty.elem):
            # The element is a place this language cannot name afterwards —
            # ``vec-free`` takes a binding or a parameter, not an element — so
            # the value moved into the buffer can never be released, but it
            # must still be moved: a second descriptor for one buffer would
            # make the source a second owner.
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
        if holds_owner(ty.elem, self._structs, self._enums):
            # A vector element is a place: releasing the owner needs the
            # element value, and the read would copy it rather than move it.
            raise diag(VEC_MOVE, node.path, _OWNER_PLACE)
        return ty.elem

    def _vec_set(self, node: Node, env: _Env) -> Ty:
        """``(vec-set v i x)``: replace one element in place."""
        var, ty = self._vec_target(
            node,
            env,
            arity=4,
            shape="vec-set takes a vector name, an index and a value",
            verb="set",
        )
        self._index(node.items[2], env, node, "vector index")
        got = self._value(node.items[3], env, ty.elem, node.path)
        if _escapes(got):
            raise diag(CLOSURE_ESCAPE, _at(node.items[3], node), _CLOSURE_ESCAPE)
        if not compatible(ty.elem, got):
            raise diag(
                TYPE_MISMATCH, _at(node.items[3], node), f"cannot store {got} in {ty}"
            )
        if self._move_only(ty.elem):
            # Same move as ``vec-push``: storing a descriptor in an element
            # would otherwise leave the source naming the same buffer.
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
            shape="vec-free takes a vector name",
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
        elem, _ = _read_type_at(items, 1, self._types, node.path, env.tvars)
        if holds_owner(elem, self._structs, self._enums):
            # ``(own X)`` is refused when X holds an owner, so an allocation
            # whose element does would make a binding of a type the language
            # cannot spell — an outer owner holding the inner one reachable
            # only through a read that is itself refused. The container types
            # are allowed to spell an owner as an element (``(opt (own T))``),
            # which is why the check has to be here as well as in
            # ``resolve_type``: ``(alloc (opt (own T)) n)`` is the spelling
            # that reaches this method with an owner-holding element.
            raise diag(
                UNKNOWN_NODE,
                node.path,
                "allocation of owners is not supported yet",
            )
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
        if holds_owner(elem, self._structs, self._enums):
            # ``(deref p)`` is a place. When the pointee is itself an owner —
            # ``(ptr (own T))`` names one, while ``(alloc (own T) n)`` is
            # refused — the read would hand out a second owner of the inner
            # block.
            raise diag(VEC_MOVE, node.path, _OWNER_PLACE)
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
        if holds_owner(elem, self._structs, self._enums):
            # Writing an owner behind a pointer stores it inside another owner
            # (``(ptr (own T))`` names such a pointee; ``(alloc (own T) n)`` is
            # refused), where no place expression can move it back out.
            raise diag(
                UNKNOWN_NODE,
                node.path,
                "an owner cannot be stored inside an owner yet",
            )
        got = self._value(items[2], env, elem, node.path)
        if _escapes(got):
            # The pointee outlives this frame; a capturing closure written
            # through a pointer is the clearest way to call it after return.
            raise diag(CLOSURE_ESCAPE, _at(items[2], node), _CLOSURE_ESCAPE)
        if _stored_result(got):
            # A pointee is storage with no name of its own: the error slot
            # would be dropped, exactly as in a field or an element.
            raise diag(
                UNHANDLED_RESULT,
                _at(items[2], node),
                "this result cannot be stored through a pointer: handle it first",
            )
        if not compatible(elem, got):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"cannot store {got} through {ty}",
            )
        if self._move_only(elem):
            # A stored descriptor must be the only one: the memory behind a
            # raw pointer is not a place a ``vec-free`` can name either, so
            # the value is moved, not copied.
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

    # -- concurrency ------------------------------------------------------

    def _spawn(self, node: Node, env: _Env) -> Ty:
        """``(spawn f args...)``: start ``f`` on a new thread, return its handle.

        The target is a module function, named rather than a value: the
        lowering emits a thunk per call site, and a thunk can only be built for
        a definition whose symbol and parameter list this module knows. Its
        arguments are *copied* into a record the thread owns, so a type that
        owns memory cannot cross — there would be two owners and one block. A
        pointer or a synchronization handle crosses only when it is marked
        ``(shared ...)``: that marker is the whole point of the form, because
        two threads reaching one object is exactly what a reader must be able
        to see.
        """
        items = node.items
        if len(items) < 2:
            raise diag(
                WRONG_ARITY, node.path, "spawn takes a function and its arguments"
            )
        target = items[1]
        if not (isinstance(target, Token) and target.kind == ATOM):
            raise diag(
                UNKNOWN_FUNCTION,
                _at(target, node),
                "spawn needs the name of a module function",
            )
        name = target.value
        sig = env.fns.get(name)
        if sig is None:
            raise diag(
                UNKNOWN_FUNCTION, _at(target, node), f"unknown function '{name}'"
            )
        if name == "main":
            raise diag(
                UNKNOWN_FUNCTION,
                _at(target, node),
                "main is the entry point and cannot be started as a thread",
            )
        if name not in self._bodies:
            # Two different reasons share this rule. A test-local helper is
            # checked but not lowered: its symbol does not exist, so a thunk
            # calling it could not link. An imported function is a module
            # function, but in another module — this module lowers only its own
            # definitions, so that symbol is not in this object file. Both are
            # E0103, like a function value; the message says which.
            if any(name in module.fns for module in self.module.imports.values()):
                raise diag(
                    UNKNOWN_FUNCTION,
                    _at(target, node),
                    f"cannot start '{name}' as a thread: imported functions "
                    "are not lowered yet",
                )
            raise diag(
                UNKNOWN_FUNCTION,
                _at(target, node),
                f"cannot start '{name}' as a thread: '{name}' is a test-local "
                "helper with no lowered definition",
            )
        if name in self._params:
            raise diag(
                UNBOUND_TYPE_VAR,
                _at(target, node),
                f"{name} is generic: a thread needs a signature with no type variables",
            )
        if not _is_void(sig.ret):
            raise diag(
                TYPE_MISMATCH,
                _at(target, node),
                f"a thread's function must return :void, found {sig.ret}",
            )
        args = items[2:]
        if len(args) != len(sig.params):
            raise diag(
                WRONG_ARITY,
                node.path,
                f"'{name}' takes {_count(len(sig.params), 'argument')}, "
                f"got {len(args)}",
            )
        for index, (arg, param) in enumerate(zip(args, sig.params), start=1):
            ty = self._value(arg, env, param, node.path)
            if _escapes(ty):
                raise diag(CLOSURE_ESCAPE, _at(arg, node), _CLOSURE_ESCAPE)
            if self._move_only(ty):
                raise diag(
                    VEC_MOVE,
                    _at(arg, node),
                    "a thread argument is copied into the new thread; "
                    "a value that owns memory cannot be copied across",
                )
            if self._unmarked_share(ty):
                raise diag(SHARED_MARKER, _at(arg, node), _SHARED_MARKER)
            if not compatible(param, ty):
                raise diag(
                    TYPE_MISMATCH,
                    _at(arg, node),
                    f"argument {index} of '{name}' expects {param}, found {ty}",
                )
            if isinstance(param, Res):
                # The thread's body is checked for the obligation, exactly as
                # a callee's is; the caller's copy is discharged by the handoff.
                self._handled(arg, env)
        return Prim("thread")

    def _unmarked_share(self, ty: Ty, seen: frozenset[str] = frozenset()) -> bool:
        """Whether ``ty`` is, or holds at any depth, shared state with no marker.

        A thread argument is *copied* into the new thread, and a struct or an
        array copy carries every field with it: a raw address in a field
        crosses the boundary exactly as a bare ``(ptr T)`` argument does, while
        the argument's spelling — ``holder`` — says nothing about it. So the
        check descends through aggregates, the same way the move check does,
        and the ``(shared ...)`` marker is a stop: below it the state is
        already accounted for, and ``(shared (ptr T))`` in a field is legal.
        A bare mutex, channel or atomic cannot appear in a field (their own
        declaration rules refuse it), but they are named here so one reachable
        by any future route is refused too.
        """
        if isinstance(ty, Shared):
            return False
        if isinstance(ty, (Ptr, Chan, Atomic)):
            return True
        if isinstance(ty, Prim):
            return ty.name == "mutex"
        if isinstance(ty, (Arr, Vec, Opt)):
            return self._unmarked_share(ty.elem, seen)
        if isinstance(ty, Res):
            return self._unmarked_share(ty.ok, seen) or self._unmarked_share(ty.err, seen)
        if isinstance(ty, StructRef):
            if ty.name in seen:
                return False
            inner = seen | {ty.name}
            return any(
                self._unmarked_share(field, inner)
                for _name, field in self._structs.get(ty.name, ())
            )
        if isinstance(ty, EnumRef):
            if ty.name in seen:
                return False
            inner = seen | {ty.name}
            return any(
                payload is not None and self._unmarked_share(payload, inner)
                for _variant, payload in self._enums.get(ty.name, ())
            )
        return False

    def _join(self, node: Node, env: _Env) -> Ty:
        """``(join t)``: wait for a thread to finish; the handle is used up."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "join takes one thread")
        ty = self._value(items[1], env, None, node.path)
        if not (isinstance(ty, Prim) and ty.name == "thread"):
            raise diag(
                TYPE_MISMATCH,
                _at(items[1], node),
                f"join needs a :thread, found {ty}",
            )
        return Prim("void")

    def _mutex(self, node: Node, env: _Env) -> Ty:
        """``(mutex)``: a new lock, already marked shared."""
        if len(node.items) != 1:
            raise diag(WRONG_ARITY, node.path, "mutex takes no arguments")
        return Shared(Prim("mutex"))

    def _mutex_op(self, node: Node, env: _Env, kind: str) -> Ty:
        """``(lock m)``/``(unlock m)``: the mutex-specific pair of checks."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, f"{kind} takes one mutex")
        ty = self._value(items[1], env, None, node.path)
        if isinstance(ty, Shared) and isinstance(ty.elem, Prim) and ty.elem.name == "mutex":
            return Prim("void")
        if _shared_state(ty):
            raise diag(SHARED_MARKER, _at(items[1], node), _SHARED_MARKER)
        raise diag(
            TYPE_MISMATCH,
            _at(items[1], node),
            f"{kind} needs a (shared :mutex), found {ty}",
        )

    def _chan(self, node: Node, env: _Env) -> Ty:
        """``(chan T n)``: a channel of T with room for n elements."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "chan takes an element type and a capacity"
            )
        elem, _ = _read_type_at(items, 1, self._types, node.path, env.tvars)
        # The same element rules `resolve_type` applies to a spelled
        # `(chan T)`: a channel copies its elements, so a value that owns
        # memory cannot be one, and a result's obligation would be dropped in
        # the ring.
        _channel_element(elem, node.path)
        capacity = self._value(items[2], env, None, node.path)
        if not _integer(capacity):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"channel capacity must be an integer, found {capacity}",
            )
        return Shared(Chan(elem))

    def _chan_operand(self, item: Node | Token, env: _Env, node: Node) -> Chan:
        ty = self._value(item, env, None, node.path)
        if isinstance(ty, Shared) and isinstance(ty.elem, Chan):
            return ty.elem
        if isinstance(ty, Chan):
            raise diag(SHARED_MARKER, _at(item, node), _SHARED_MARKER)
        raise diag(
            TYPE_MISMATCH, _at(item, node), f"expected a channel, found {ty}"
        )

    def _send(self, node: Node, env: _Env) -> Ty:
        """``(send c v)``: copy v into c, blocking while c is full."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "send takes a channel and a value")
        chan = self._chan_operand(items[1], env, node)
        ty = self._value(items[2], env, chan.elem, node.path)
        if _escapes(ty):
            # The ring outlives this frame; a capturing closure's environment
            # is a slot in it. Same rule as any other place a value can be
            # kept (E0502).
            raise diag(CLOSURE_ESCAPE, _at(items[2], node), _CLOSURE_ESCAPE)
        if self._move_only(ty):
            raise diag(
                VEC_MOVE,
                _at(items[2], node),
                "a channel copies its element; a value that owns memory "
                "cannot be sent",
            )
        if not compatible(chan.elem, ty):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"this channel carries {chan.elem}, found {ty}",
            )
        return Prim("void")

    def _recv(self, node: Node, env: _Env) -> Ty:
        """``(recv c)``: the next element, blocking while c is empty."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "recv takes one channel")
        return self._chan_operand(items[1], env, node).elem

    def _atomic(self, node: Node, env: _Env) -> Ty:
        """``(atomic T v)``: a new atomic cell holding v."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic takes a type and an initial value"
            )
        elem, next_index = _read_type_at(items, 1, self._types, node.path, env.tvars)
        if not (isinstance(elem, Prim) and elem.name in ("i64", "u64")):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"an atomic needs :i64 or :u64, found {elem}",
            )
        value = self._value(items[next_index], env, elem, node.path)
        if not compatible(elem, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[next_index], node),
                f"this atomic holds {elem}, found {value}",
            )
        return Shared(Atomic(elem))

    def _atomic_operand(self, item: Node | Token, env: _Env, node: Node) -> Atomic:
        ty = self._value(item, env, None, node.path)
        if isinstance(ty, Shared) and isinstance(ty.elem, Atomic):
            return ty.elem
        if isinstance(ty, Atomic):
            raise diag(SHARED_MARKER, _at(item, node), _SHARED_MARKER)
        raise diag(
            TYPE_MISMATCH, _at(item, node), f"expected an atomic, found {ty}"
        )

    def _atomic_add(self, node: Node, env: _Env) -> Ty:
        """``(atomic-add a n)``: add n, returning the new value."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic-add takes an atomic and a value"
            )
        atomic = self._atomic_operand(items[1], env, node)
        value = self._value(items[2], env, atomic.elem, node.path)
        if not compatible(atomic.elem, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"this atomic holds {atomic.elem}, found {value}",
            )
        return atomic.elem

    def _atomic_load(self, node: Node, env: _Env) -> Ty:
        """``(atomic-load a)``: the current value."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "atomic-load takes one atomic")
        return self._atomic_operand(items[1], env, node).elem

    def _atomic_store(self, node: Node, env: _Env) -> Ty:
        """``(atomic-store a v)``: replace the value."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic-store takes an atomic and a value"
            )
        atomic = self._atomic_operand(items[1], env, node)
        value = self._value(items[2], env, atomic.elem, node.path)
        if not compatible(atomic.elem, value):
            raise diag(
                TYPE_MISMATCH,
                _at(items[2], node),
                f"this atomic holds {atomic.elem}, found {value}",
            )
        return Prim("void")

    def _origin_loop(self, item: Node | Token, env: _Env, ty: Ty) -> int:
        """How many loops enclosed the value ``item`` produced.

        Only a capturing closure cares: its environment slot is one per
        expression per frame, so a value born inside a loop cannot be kept in a
        binding declared outside it. A name stands for the value already in the
        binding, and a branch yields whichever arm runs, so the deepest arm is
        the conservative answer.
        """
        if not _escapes(ty):
            return env.loop
        if isinstance(item, Token) and item.kind == ATOM:
            found = env.lookup(item.value, "")
            if found is not None:
                return found.origin_loop
            return env.loop
        if isinstance(item, Node):
            if item.kind == "if" and len(item.items) == 4:
                return max(
                    self._origin_loop(item.items[2], env, ty),
                    self._origin_loop(item.items[3], env, ty),
                )
            if item.kind == "match":
                arms = [
                    arm.items[-1]
                    for arm in item.items[2:]
                    if isinstance(arm, Node) and arm.items
                ]
                if arms:
                    return max(self._origin_loop(arm, env, ty) for arm in arms)
        return env.loop

    def _move_only(self, ty: Ty) -> bool:
        """Whether a value of ``ty`` has no copy at all.

        An ``(own T)`` is the owning pointer itself; anything that contains one
        or a ``(vec T)`` by value — an optional, a result, an array, a struct
        field, an enum payload, however deeply nested — is the same: copying it
        would give two names one block. A ``(ptr T)`` is an address and copies,
        and so does a closure type: neither owns anything by value.
        """
        if isinstance(ty, Own):
            return True
        return holds_vector(ty, self._structs) or holds_owner(
            ty, self._structs, self._enums
        )

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

        A value that contains a ``(vec T)`` or an ``(own T)`` is different:
        copying it would give two bindings one buffer or one block, and this
        language has no rule for that, so a second name for such a value is
        always a move. It is therefore allowed out of a plain ``let`` as well —
        the alternative is not a stable binding but a second owner — and the
        source is empty afterwards, which is what makes the ``let``'s promise
        true rather than broken.
        """
        if not (isinstance(item, Token) and item.kind == ATOM):
            return
        var = env.lookup(item.value, where)
        if var is None or var.moved:
            return
        if not self._move_only(var.ty):
            return
        if not isinstance(var.ty, Own):
            # Structural ownership travels with the value, so the move needs no
            # ``letmut``: the binding is not being reused, it is being emptied.
            var.moved = True
            return
        if not release and not var.mutable and not var.param:
            raise diag(
                MOVE_IMMUTABLE,
                where,
                f"cannot move out of immutable binding '{item.value}'",
            )
        var.moved = True

    def _transfer(self, item: Node | Token, env: _Env, where: str) -> None:
        """Refuse a second transfer of a value that has no copy.

        Binding or passing such a value moves it, so the source is empty
        afterwards. Reaching it again means the program asked for a second
        value where there is one buffer or one block, and the answer is neither
        a copy (two owners of one allocation) nor a use-after-move (the value
        is still here, it just left): it is E0402, which names the reason. A
        plain read of the empty binding is still E0300.
        """
        if not (isinstance(item, Token) and item.kind == ATOM):
            return
        var = env.lookup(item.value, where)
        if var is None or not var.moved:
            return
        if holds_vector(var.ty, self._structs):
            raise diag(
                VEC_MOVE,
                where,
                f"a value containing a vector is not copyable; '{item.value}' moved",
            )
        if holds_owner(var.ty, self._structs, self._enums):
            raise diag(
                VEC_MOVE,
                where,
                f"a value containing an owner is not copyable; '{item.value}' moved",
            )

    # -- results and optionals --------------------------------------------

    def _result_form(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        """``(ok v)``, ``(err e)``, ``(some v)``, ``(none)`` and ``(try e)``.

        One entry point because the five are one family: each builds or
        consumes a tagged value whose tag the compiler owns. ``ok`` and ``err``
        need the result type the context expects — there is no widening and no
        inference to invent an error enum — while ``some`` infers an element
        for itself and ``none`` cannot.
        """
        kind = node.kind
        if kind == "try":
            return self._try(node, env)
        if kind == "none":
            return self._none(node, env, expected)
        if kind == "some":
            return self._some(node, env, expected)
        return self._ok_or_err(node, env, expected)

    def _ok_or_err(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        """``(ok v)`` or ``(err e)`` at the result type the context declares."""
        args = node.items[1:]
        if len(args) != 1:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes one value")
        if not isinstance(expected, Res):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"cannot infer the result type of '{node.kind}': "
                "annotate the binding or the return type",
            )
        value = self._value(
            args[0], env, expected.ok if node.kind == "ok" else expected.err, node.path
        )
        wanted = expected.ok if node.kind == "ok" else expected.err
        if not _fits(wanted, value):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"'{node.kind}' expects {wanted}, found {value}",
            )
        self._refuse_carried(value, args[0], node, "a result")
        # A result is built by copying its slot; when the slot owns something,
        # the value moves instead and the source is empty.
        self._consume(args[0], env, node.path)
        return expected

    def _some(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        """``(some v)``: an optional, at the annotation's element or v's own type."""
        args = node.items[1:]
        if len(args) != 1:
            raise diag(WRONG_ARITY, node.path, "'some' takes one value")
        elem = expected.elem if isinstance(expected, Opt) else None
        value = self._value(args[0], env, elem, node.path)
        if elem is not None and not _fits(elem, value):
            raise diag(
                TYPE_MISMATCH,
                _at(args[0], node),
                f"'some' expects {elem}, found {value}",
            )
        self._refuse_carried(value, args[0], node, "an optional")
        # An optional holds its element by value, so an owning element moves
        # into it rather than being copied.
        self._consume(args[0], env, node.path)
        return Opt(elem if elem is not None else value)

    def _none(self, node: Node, env: _Env, expected: Ty | None) -> Ty:
        """``(none)``: the empty optional. Its element is the annotation's."""
        if len(node.items) != 1:
            raise diag(WRONG_ARITY, node.path, "'none' takes no values")
        if not isinstance(expected, Opt):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                "cannot infer the element type of 'none': annotate the binding "
                "or the return type",
            )
        return expected

    def _try(self, node: Node, env: _Env) -> Ty:
        """``(try e)``: the ok value, or an early ``(err e)`` for the caller.

        Propagation is exact: the enclosing function must return a result whose
        error type is *the same* enum. There is no widening and no union, so a
        different error type is E0601 and the source maps it in a ``match``.
        """
        args = node.items[1:]
        if len(args) != 1:
            raise diag(WRONG_ARITY, node.path, "'try' takes one result")
        inner = self._value(args[0], env, None, node.path)
        if not isinstance(inner, Res):
            raise diag(
                TRY_MISMATCH, node.path, f"'try' needs a result, found {inner}"
            )
        if not isinstance(env.ret, Res):
            raise diag(
                TRY_MISMATCH,
                node.path,
                f"{env.label} returns {env.ret}, so it cannot propagate {inner}",
            )
        if not compatible(env.ret.err, inner.err):
            raise diag(
                TRY_MISMATCH,
                node.path,
                f"cannot 'try' a result whose error is {inner.err}: "
                f"{env.label} returns {env.ret}",
            )
        # The error is the caller's to handle now; the ok value is this
        # expression's.
        self._handled(args[0], env)
        if self._move_only(inner.ok):
            # ``try`` hands the ok payload out by value. When it owns
            # something the result gives it up, exactly as a construction
            # does, so the source cannot be read again.
            self._consume(args[0], env, node.path)
        return inner.ok

    def _handled(self, item: Node | Token, env: _Env) -> None:
        """Discharge a result binding's obligation (see :meth:`_Env.finish`)."""
        if not (isinstance(item, Token) and item.kind == ATOM):
            return
        var = env.find(item.value)
        if var is not None and isinstance(var.ty, Res):
            var.handled = True

    def _refuse_carried(self, ty: Ty, item: Node | Token, node: Node, what: str) -> None:
        """Refuse a closure, vector or result carried inside a tagged value.

        A tagged value is copied where it is built, and neither a capturing
        closure's environment nor a vector's descriptor has a copy: the enum's
        payload or the result's slot would point at a stack slot or a buffer
        that dies with the construction, and nothing could release the second
        owner. A result inside one would be an obligation with no name to
        discharge it, which is E0602 — the value its error slot exists for,
        hidden one level down. All three are refused at the construction, where
        the source still names the inner value — the same check a struct field
        already makes.
        """
        if _escapes(ty):
            # The environment is a slot in this frame; a tagged value is
            # storage that outlives the expression that built it, so a
            # capturing closure in it would be called after this frame is gone.
            raise diag(CLOSURE_ESCAPE, _at(item, node), _CLOSURE_ESCAPE)
        if holds_vector(ty, self._structs):
            raise diag(
                VEC_MOVE,
                _at(item, node),
                f"a vector cannot be carried in {what} yet",
            )
        if _stored_result(ty):
            raise diag(
                UNHANDLED_RESULT,
                _at(item, node),
                f"this result cannot be carried in {what}: handle it first",
            )

    def _variant_type(self, name: str, where: str) -> tuple[str, Ty | None] | None:
        """The enum and payload a variant name declares, or ``None``.

        Shared by the two spellings of a constructor: the form ``(bad 5)`` and
        the bare atom ``overflow``. Two visible enums declaring one name is
        ambiguous — the constructor would have to guess which type to build —
        so it is E0005 at the use.
        """
        found = self._variants.get(name)
        if not found:
            return None
        if len(found) > 1:
            enums = ", ".join(sorted(enum for enum, _ in found))
            raise diag(
                NOT_FOUND,
                where,
                f"variant '{name}' is declared by more than one enum ({enums})",
            )
        return found[0]

    def _construct(self, node: Node, env: _Env) -> Ty | None:
        """``(variant v)`` or ``variant``: a value of the enum that declares it.

        The variant namespace is the module's visible enums, like a C enum's
        enumerators. A payload-less variant is written bare, ``overflow``; a
        variant that carries a value is written as a form, ``(bad 5)``.
        """
        name = node.kind
        found = self._variant_type(name, node.path)
        if found is None:
            return None
        enum, payload = found
        args = node.items[1:]
        if payload is None:
            if args:
                raise diag(
                    WRONG_ARITY, node.path, f"variant '{name}' takes no value"
                )
        else:
            if len(args) != 1:
                raise diag(
                    WRONG_ARITY, node.path, f"variant '{name}' takes one value"
                )
            value = self._value(args[0], env, payload, node.path)
            if not _fits(payload, value):
                raise diag(
                    TYPE_MISMATCH,
                    _at(args[0], node),
                    f"variant '{name}' holds {payload}, found {value}",
                )
            self._refuse_carried(value, args[0], node, f"enum '{enum}'")
            # The payload area holds the value by copy, so anything owning
            # moves into the variant and the source is empty afterwards.
            self._consume(args[0], env, node.path)
        return EnumRef(enum)

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

    def _call(self, node: Node, env: _Env, expected: Ty | None = None) -> Ty:
        """A call: through a binding's ``(fn ...)`` type, or by name.

        A name that a binding shadows is that binding — a function value called
        indirectly. A name in the function namespace is a definition; when its
        signature mentions a type variable the call is an instantiation, and
        when it does not it is the plain direct call it always was.

        A name that is neither is one of the compiler's own forms — the
        printers, ``ok``/``err``/``some``/``none``/``try`` — or an enum
        variant, which is a constructor. A user definition of one of those
        names shadows the form, exactly as ``print`` already does.
        """
        name = node.kind
        var = env.lookup(name, node.path)
        if var is not None:
            if not isinstance(var.ty, FnTy):
                raise diag(
                    TYPE_MISMATCH, node.path, f"'{name}' is not a function: found {var.ty}"
                )
            return self._arguments(node, env, name, var.ty)
        found = env.fns.get(name)
        if found is None:
            if name in PRINTERS:
                return self._printer(node, env)
            if name in RESULT_FORMS:
                return self._result_form(node, env, expected)
            constructed = self._construct(node, env)
            if constructed is not None:
                return constructed
            raise diag(UNKNOWN_FUNCTION, node.path, f"unknown function '{name}'")
        if name in self._params:
            return self._generic_call(node, env, name, found)
        return self._arguments(node, env, name, found)

    def _arguments(self, node: Node, env: _Env, name: str, sig: FnTy) -> Ty:
        """Check one call's arguments against ``sig`` and return its type."""
        args = node.items[1:]
        if len(args) != len(sig.params):
            raise diag(
                WRONG_ARITY,
                node.path,
                f"function '{name}' takes {_count(len(sig.params), 'argument')}, "
                f"got {len(args)}",
            )
        for index, (arg, param) in enumerate(zip(args, sig.params), start=1):
            if holds_vector(param, self._structs):
                # R6: the callee owns and may free what it receives, so the
                # argument must be a whole binding the caller can give up. A
                # descriptor copied out of an element, out of a call result or
                # out of a branch would leave the caller's container pointing
                # at memory the callee released.
                if not (isinstance(arg, Token) and arg.kind == ATOM):
                    raise diag(
                        VEC_MOVE,
                        _at(arg, node),
                        "a vector argument must be a name",
                    )
                # A vector argument is a transfer, not a copy: the same
                # argument twice is E0402 at the second one.
                self._transfer(arg, env, node.path)
            elif self._move_only(param):
                # An owner travels the same way: a second transfer of the same
                # binding is E0402 rather than a read of an empty slot.
                self._transfer(arg, env, node.path)
            ty = self._value(arg, env, param, node.path)
            if _escapes(ty):
                # R12: a capturing closure's environment is a slot in this
                # frame. A callee may keep what it is handed — in a field, a
                # vector, or by returning it — and nothing in the type of a
                # parameter can say it will not, so the value never crosses a
                # call boundary at all. (A plain `(fn ...)` parameter is also
                # incompatible with it, but E0502 says why.)
                raise diag(CLOSURE_ESCAPE, _at(arg, node), _CLOSURE_ESCAPE)
            if not compatible(param, ty):
                raise diag(
                    TYPE_MISMATCH,
                    _at(arg, node),
                    f"argument {index} of '{name}' expects {param}, found {ty}",
                )
            if self._move_only(param):
                # The callee owns what it is handed: the argument moves into
                # the parameter and the caller's binding is empty afterwards.
                self._consume(arg, env, node.path)
            if isinstance(param, Res):
                # The callee's parameter carries the obligation now, and its
                # own body is checked for it.
                self._handled(arg, env)
        return sig.ret

    # -- generics ---------------------------------------------------------

    def _generic_call(
        self, node: Node, env: _Env, name: str, sig: FnTy
    ) -> Ty:
        """One call to a generic: unify the arguments, then name the copy.

        Unification is what "resolved per call site" means: each parameter is
        matched against the argument beside it, binding every type variable it
        meets. A parameter that mentions no variable is checked exactly as a
        direct call's is, so the error a non-generic call would give is the
        error this one gives.
        """
        args = node.items[1:]
        if len(args) != len(sig.params):
            raise diag(
                WRONG_ARITY,
                node.path,
                f"function '{name}' takes {_count(len(sig.params), 'argument')}, "
                f"got {len(args)}",
            )
        bindings: dict[str, Ty] = {}
        for index, (arg, param) in enumerate(zip(args, sig.params), start=1):
            if holds_vector(param, self._structs):
                # The parameter is ``(vec T)`` or a vector-holding type however
                # T ends up bound, so the transfer rules apply before the call
                # is instantiated.
                if not (isinstance(arg, Token) and arg.kind == ATOM):
                    raise diag(
                        VEC_MOVE, _at(arg, node), "a vector argument must be a name"
                    )
                self._transfer(arg, env, node.path)
            elif self._move_only(param):
                # An owning argument is a transfer too, whether the type is
                # written out or fixed by unification.
                self._transfer(arg, env, node.path)
            hint = None if has_typevar(param) else param
            ty = self._value(arg, env, hint, node.path)
            if _escapes(ty):
                # R12, as in `_arguments`: no call may receive a capturing
                # closure, and that includes one whose type argument would
                # have been inferred from it.
                raise diag(CLOSURE_ESCAPE, _at(arg, node), _CLOSURE_ESCAPE)
            trial = dict(bindings)
            if _is_void(ty) or not _unify(param, ty, trial):
                raise diag(
                    TYPE_MISMATCH,
                    _at(arg, node),
                    f"argument {index} of '{name}' expects "
                    f"{substitute(param, bindings)}, found {ty}",
                )
            bindings = trial
            concrete = substitute(param, bindings)
            if holds_vector(concrete, self._structs):
                # R8: the parameter was a bare type variable, so the vector
                # rules could not be applied before unification. Apply them
                # now, exactly as a non-generic call would: only a whole
                # binding can be handed over, and the transfer empties it so a
                # second use is E0300 rather than a double free.
                if not (isinstance(arg, Token) and arg.kind == ATOM):
                    raise diag(
                        VEC_MOVE, _at(arg, node), "a vector argument must be a name"
                    )
                self._transfer(arg, env, node.path)
                self._consume(arg, env, node.path)
            elif self._move_only(concrete):
                # R6: a bare type variable fixed by this argument to a type
                # that owns something moves here for the same reason.
                self._transfer(arg, env, node.path)
                self._consume(arg, env, node.path)
            if isinstance(concrete, Res):
                self._handled(arg, env)
        target = self._instantiate(node, name, bindings)
        self._record.call_targets[node.path] = target.name
        return target.ret

    def _instantiate(
        self, node: Node, name: str, bindings: dict[str, Ty]
    ) -> Instance:
        """The copy of ``name`` for ``bindings``, checking it the first time.

        The memo key is the mangled name, so a second call with the same type
        arguments reuses the copy; a new one is recorded and queued, and the
        worklist checks its body after the enclosing body is done. The ceiling
        is checked here, where a new copy is about to be made: 256 copies exist
        before the 257th is asked for, and that one is E0500 rather than a
        program whose binary grows with every call site.
        """
        order = self._params[name]
        missing = [var for var in order if var not in bindings]
        if missing:
            # Nothing in the arguments fixed this variable, so there is no one
            # type the copy could be made at.
            raise diag(
                UNBOUND_TYPE_VAR,
                node.path,
                f"type variable {missing[0]} of '{name}' is not bound by this call",
            )
        args = tuple(bindings[var] for var in order)
        target = mangle_instance(name, args, node.path)
        found = self._instances.get(target)
        if found is not None:
            return found
        if target in self._visible(self.module).fns:
            # Every definition the module can see is emitted (or imported)
            # under ``lg.<module>.<name>``; a source name that already spells
            # this copy's name would make two definitions of one symbol. The
            # generic source names are not the only names that matter: a
            # user-written ``id$i64`` collides exactly as hard as a second
            # copy would.
            raise diag(
                UNKNOWN_FUNCTION,
                node.path,
                f"cannot name a copy of '{name}': '{target}' is already defined",
            )
        body = self._bodies.get(name)
        if body is None:
            raise diag(
                UNKNOWN_FUNCTION,
                node.path,
                f"cannot instantiate '{name}': only module functions are "
                f"monomorphised, not test helpers or imports",
            )
        if len(self._instances) >= MAX_INSTANCES:
            raise diag(
                INSTANTIATION_LIMIT,
                node.path,
                f"more than {MAX_INSTANCES} instantiations of generic functions",
            )
        subst = {var: bindings[var] for var in order}
        sig = self._fn_sig(name)
        instance = Instance(
            target,
            name,
            args,
            tuple(substitute(param, subst) for param in sig.params),
            substitute(sig.ret, subst),
            Types(),
        )
        self._instances[target] = instance
        self._pending.append(target)
        return instance

    def _fn_sig(self, name: str) -> FnTy:
        """A generic definition's signature, with its type variables in place."""
        return self._visible(self.module).fns[name]

    def _drain(self, fns: dict[str, FnTy]) -> None:
        """Check every queued copy, oldest first, until the queue is empty.

        A copy's body can name further copies, which append themselves to the
        queue; draining it in creation order is what makes a generic that calls
        another generic — or itself — terminate, and makes the order of the
        emitted copies a function of the source alone.
        """
        while self._pending:
            target = self._pending.pop(0)
            self._check_instance(self._instances[target], fns)

    def _check_instance(self, instance: Instance, fns: dict[str, FnTy]) -> None:
        """Check one copy's body under its own substitution and type record."""
        node = self._bodies[instance.fn]
        _, _, start = fn_shape(node, self._types)
        subst = dict(zip(self._params[instance.fn], instance.args))
        env = _Env(
            fns,
            ret=instance.ret,
            label=f"function '{instance.name}'",
            tvars=subst,
            structs=self._structs,
            enums=self._enums,
        )
        for index, param in enumerate(_param_nodes(_params_item(node), node.path)):
            param_name, _ = _param_parts(param)
            env.define(
                param_name,
                instance.params[index],
                mutable=False,
                path=param.path,
                param=True,
            )
        saved = self._record
        self._record = instance.body
        try:
            self._body(node, start, env)
        finally:
            self._record = saved

    # -- closures ---------------------------------------------------------

    def _closure(self, node: Node, env: _Env) -> Ty:
        """``(fn :R ((p :T)...) body...)``: a value of ``(fn (T) R)``.

        The body is a function of its own: parameters, a return type and its
        own loop depth, checked in a fresh scope whose outer environment is the
        one the expression appeared in. A name the body reads from there is
        captured on first use — the lookup copies it — so the capture list the
        lowerer builds the environment from is exactly the set of names the
        body could not resolve for itself.
        """
        ret, params, start = closure_shape(node, self._types, env.tvars)
        inner = _Env(
            env.fns,
            ret=ret,
            label="closure",
            outer=env,
            tvars=env.tvars,
            structs=self._structs,
            enums=self._enums,
        )
        for (param_name, param_ty), param_node in zip(
            params, _param_nodes(_closure_params(node), node.path)
        ):
            inner.define(
                param_name,
                param_ty,
                mutable=False,
                path=param_node.path,
                param=True,
            )
        self._statements(node.items[start:], inner, node.path)
        self._record.closures[node.path] = Closure(
            tuple(ty for _, ty in params),
            tuple(name for name, _ in params),
            ret,
            tuple(inner.captures),
        )
        return FnTy(tuple(ty for _, ty in params), ret, bool(inner.captures))


def closure_shape(
    node: Node, types: dict[str, Ty], bindings: dict[str, Ty] | None = None
) -> tuple[Ty, list[tuple[str, Ty]], int]:
    """A ``(fn ...)`` expression's return type, parameters and body start.

    The spelling is a definition's without the name: ``(fn :i64 ((a :i64))
    (ret a))``. ``bindings`` is the enclosing definition's substitution, so a
    closure inside a generic body may name its type variables.
    """
    if len(node.items) < 3:
        raise diag(
            UNKNOWN_NODE,
            node.path,
            "a closure needs a return type and a parameter list",
        )
    ret, index = _read_type_at(node.items, 1, types, node.path, bindings)
    if index >= len(node.items):
        raise diag(UNKNOWN_NODE, node.path, "a closure needs a parameter list")
    params: list[tuple[str, Ty]] = []
    names: set[str] = set()
    for param in _param_nodes(node.items[index], node.path):
        name, item = _param_parts(param)
        if name in names:
            raise diag(DUPLICATE, param.path, f"duplicate definition '{name}'")
        names.add(name)
        ty = _read_type(item, types, param.path, bindings)
        if compatible(ty, Prim("void")):
            raise diag(
                TYPE_MISMATCH, param.path, f"parameter '{name}' cannot be :void"
            )
        params.append((name, ty))
    return ret, params, index + 1


def _closure_params(node: Node) -> Node | Token:
    """The parameter list of a ``(fn ...)`` expression, wherever it sits."""
    index = 3 if len(node.items) > 1 and _marker(node.items[1]) else 2
    if index >= len(node.items):
        raise diag(UNKNOWN_NODE, node.path, "a closure needs a parameter list")
    return node.items[index]


def _unify(param: Ty, actual: Ty, bindings: dict[str, Ty]) -> bool:
    """Bind every type variable in ``param`` so that it matches ``actual``.

    The result is what makes a call site's types concrete: matching ``(vec T)``
    against ``(vec :i64)`` binds ``T`` to ``:i64``, and a later match of the
    same variable must agree with the first. A variable never binds another
    variable — that would only postpone the choice — and an already-bound one
    compares with equality, so ``(fn same :T ((a :T) (b :T)))`` accepts two
    ``:i64`` and refuses an ``:i64`` beside a ``:str``. ``bindings`` is updated
    on success only; the caller passes a copy so a mismatched argument leaves
    no half-bound state behind.
    """
    if isinstance(param, TypeVar):
        bound = bindings.get(param.name)
        if bound is None:
            if isinstance(actual, TypeVar):
                return False
            bindings[param.name] = actual
            return True
        return compatible(bound, actual)
    if isinstance(param, (Ptr, Own, Opt, Shared, Vec)):
        return isinstance(actual, type(param)) and _unify(
            param.elem, actual.elem, bindings
        )
    if isinstance(param, Arr):
        return (
            isinstance(actual, Arr)
            and param.n == actual.n
            and _unify(param.elem, actual.elem, bindings)
        )
    if isinstance(param, Res):
        return (
            isinstance(actual, Res)
            and _unify(param.ok, actual.ok, bindings)
            and _unify(param.err, actual.err, bindings)
        )
    if isinstance(param, FnTy):
        if (
            not isinstance(actual, FnTy)
            or param.captures != actual.captures
            or len(param.params) != len(actual.params)
        ):
            return False
        if not all(
            _unify(one, other, bindings)
            for one, other in zip(param.params, actual.params)
        ):
            return False
        return _unify(param.ret, actual.ret, bindings)
    return compatible(param, actual)


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


def _read_type(
    item: Node | Token,
    types: dict[str, Ty],
    fallback: str,
    bindings: dict[str, Ty] | None = None,
) -> Ty:
    """Read one type item and resolve its names, locating errors at it."""
    path = item.path if isinstance(item, Node) and item.path else fallback
    return resolve_type(parse_type(item, path=path), types, path, bindings)


def _read_type_at(
    items: tuple[Node | Token, ...],
    start: int,
    types: dict[str, Ty],
    fallback: str,
    bindings: dict[str, Ty] | None = None,
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
    return _read_type(item, types, fallback, bindings), start + 1


def resolve_type(
    ty: Ty,
    types: dict[str, Ty],
    path: str,
    bindings: dict[str, Ty] | None = None,
    *,
    _shared: bool = False,
) -> Ty:
    """Resolve every named reference in ``ty`` against ``types``.

    A ``StructRef`` becomes the declared type, which turns it into an
    ``EnumRef`` when the name is an enum. An undeclared name is E0101. The
    lookup is total: an absent name is a diagnostic, never a ``KeyError``.

    ``bindings`` decides what a type variable means. ``None`` is a signature
    reading the variables it *declares*, so each one stays as it is; a mapping
    is a body inside a definition, where a variable is replaced by the concrete
    type it stands for and one the mapping does not hold is E0501 — an
    annotation naming a variable nothing bound.

    Public because the lowerer needs the same answer for a type annotation it
    reads from the tree: the checker proves the annotation resolves, and the
    backend must not guess a second time.
    """
    if isinstance(ty, TypeVar):
        if bindings is None:
            return ty
        bound = bindings.get(ty.name)
        if bound is None:
            raise diag(
                UNBOUND_TYPE_VAR,
                path,
                f"type variable {ty.name} is not bound here",
            )
        return bound
    if isinstance(ty, Prim):
        if ty.name == "mutex" and not _shared:
            # A mutex is shared mutable state: two threads lock it, and the
            # type has to say so at every declaration that names it. Only the
            # ``(shared ...)`` wrapper, which passes ``_shared`` down one
            # level, may spell it.
            raise diag(SHARED_MARKER, path, _SHARED_MARKER)
        return ty
    if isinstance(ty, (StructRef, EnumRef)):
        declared = types.get(ty.name)
        if declared is None:
            raise diag(UNKNOWN_TYPE, path, f"unknown type '{ty.name}'")
        return declared
    if isinstance(ty, Ptr):
        return Ptr(resolve_type(ty.elem, types, path, bindings))
    if isinstance(ty, Own):
        elem = resolve_type(ty.elem, types, path, bindings)
        if holds_owner(elem):
            # An owner holds a block, not another owner: the inner pointer
            # would only be reachable through a deref, and that read is E0402.
            # ``alloc`` refuses an element that holds an owner for the same
            # reason, so no owning pointer to one can be made; the read site
            # states the rule as well (a ``(ptr (own T))`` is spellable).
            raise diag(
                UNKNOWN_NODE,
                path,
                "an owner cannot hold another owner yet",
            )
        return Own(elem)
    if isinstance(ty, Opt):
        elem = resolve_type(ty.elem, types, path, bindings)
        if holds_vector(elem):
            # Same reason a vector may not be a struct field: a tagged value is
            # copied where it is built, and a vector's descriptor has no copy.
            raise diag(
                UNKNOWN_NODE, path, "a vector cannot be inside an optional yet"
            )
        if _stored_result(elem):
            # An optional is storage, and a result's obligation is discharged
            # through a name; nesting one here would drop it.
            raise diag(
                UNKNOWN_NODE, path, "a result cannot be inside an optional yet"
            )
        return Opt(elem)
    if isinstance(ty, Shared):
        elem = resolve_type(ty.elem, types, path, bindings, _shared=True)
        # Shared state is something a thread can reach and change: a pointer
        # into the heap, or one of the three synchronization handles. A
        # ``(shared :i64)`` would name a copy, which no thread could change for
        # anybody else, so it is refused rather than left as a marker with no
        # meaning.
        if not _shareable(elem):
            raise diag(
                UNKNOWN_NODE,
                path,
                "only a pointer, a mutex, a channel or an atomic can be shared",
            )
        return Shared(elem)
    if isinstance(ty, (Chan, Atomic)):
        if not _shared:
            raise diag(SHARED_MARKER, path, _SHARED_MARKER)
        elem = resolve_type(ty.elem, types, path, bindings)
        if isinstance(ty, Chan):
            return Chan(_channel_element(elem, path))
        if not (isinstance(elem, Prim) and elem.name in ("i64", "u64")):
            raise diag(
                TYPE_MISMATCH,
                path,
                f"an atomic needs :i64 or :u64, found {elem}",
            )
        return Atomic(elem)
    if isinstance(ty, Vec):
        elem = resolve_type(ty.elem, types, path, bindings)
        if holds_owner(elem):
            # A container is not a place this language can name: an element
            # can be neither read (E0402) nor released where it lies, so a
            # vector of owners leaks by construction and no program can be
            # written with one. Refused where the container is spelled, for
            # the same reason a vector field is.
            raise diag(
                UNKNOWN_NODE,
                path,
                "a container of owners is not supported yet",
            )
        if _stored_result(elem):
            raise diag(
                UNKNOWN_NODE, path, "a result cannot be a vector element yet"
            )
        return Vec(elem)
    if isinstance(ty, Res):
        ok = resolve_type(ty.ok, types, path, bindings)
        err = resolve_type(ty.err, types, path, bindings)
        if not isinstance(err, EnumRef):
            # The error slot is a declared enum and nothing else: `try` needs
            # one identity to compare, and a primitive or a struct has none.
            # E0100, not E0101: the type is known, it is just the wrong kind.
            raise diag(
                TYPE_MISMATCH,
                path,
                f"the error type of a result must be a declared enum, found {err}",
            )
        if holds_vector(ok):
            raise diag(UNKNOWN_NODE, path, "a vector cannot be inside a result yet")
        if _stored_result(ok):
            raise diag(
                UNKNOWN_NODE, path, "a result cannot be inside a result yet"
            )
        return Res(ok, err)
    if isinstance(ty, FnTy):
        params = tuple(
            resolve_type(param, types, path, bindings) for param in ty.params
        )
        return FnTy(
            params,
            resolve_type(ty.ret, types, path, bindings),
            ty.captures,
        )
    # ``ty`` is the array case; spelled last so the branches read in the
    # order the type forms are documented.
    elem = resolve_type(ty.elem, types, path, bindings)
    if holds_owner(elem):
        # Also the reachable spelling of the same program: an `(arr (own T)
        # n)` parameter could name the type, and nothing could release an
        # element of it. Refused where the array is spelled, like a vector.
        raise diag(
            UNKNOWN_NODE,
            path,
            "a container of owners is not supported yet",
        )
    if holds_vector(elem):
        # Same reason a vector field is refused: an element of a fixed array
        # is not a place this language can name, so a vector stored there
        # could never release its buffer. Refused where the array is spelled.
        raise diag(UNKNOWN_NODE, path, "vector array elements are not supported yet")
    if _stored_result(elem):
        raise diag(UNKNOWN_NODE, path, "a result cannot be an array element yet")
    return Arr(elem, ty.n)


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


def _contains_ret(items: tuple[Node | Token, ...] | list[Node | Token]) -> bool:
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
    A ``(shared (ptr T))`` is the same address under the marker a thread
    boundary requires, so a read through it is a pointee read too.
    """
    if isinstance(ty, Shared):
        ty = ty.elem
    if isinstance(ty, (Own, Ptr)):
        return ty.elem
    return None


def _shareable(ty: Ty) -> bool:
    """Whether ``(shared ty)`` names state two threads can reach and change.

    A pointer is an address into memory somebody else owns; a mutex, a channel
    and an atomic are runtime handles whose whole purpose is to be touched from
    more than one thread. Anything else — a scalar, a vector descriptor, an
    owner — is a copy when it crosses a thread boundary, so marking it shared
    would promise a sharing that does not happen.
    """
    if isinstance(ty, Ptr):
        return True
    if isinstance(ty, (Chan, Atomic)):
        return True
    return isinstance(ty, Prim) and ty.name == "mutex"


def _shared_state(ty: Ty) -> bool:
    """Whether ``ty`` is a handle that only the ``(shared ...)`` marker allows."""
    if isinstance(ty, (Chan, Atomic)):
        return True
    return isinstance(ty, Prim) and ty.name == "mutex"


def _channel_element(elem: Ty, path: str) -> Ty:
    """Refuse a channel element the ring cannot copy, at ``path``.

    A channel stores copies of its elements and copies them again on ``recv``,
    so the element must have a copy: not a vector or an owner (two owners of one
    block), not a result (an obligation with no name to discharge it), not
    ``:void`` (no value exists), and not a capturing closure (its environment
    belongs to the frame that made it).
    """
    if _is_void(elem):
        raise diag(TYPE_MISMATCH, path, "a channel cannot carry :void")
    if holds_owner(elem):
        raise diag(
            UNKNOWN_NODE, path, "a channel cannot carry a value that owns memory"
        )
    if holds_vector(elem):
        raise diag(
            UNKNOWN_NODE,
            path,
            "a channel cannot carry a vector: its buffer has no copy",
        )
    if _stored_result(elem):
        raise diag(UNKNOWN_NODE, path, "a channel cannot carry a result")
    if _escapes(elem):
        raise diag(CLOSURE_ESCAPE, path, _CLOSURE_ESCAPE)
    return elem


def _stored_result(ty: Ty) -> bool:
    """Whether ``ty`` *is* a result, or an array or optional of one.

    A result is an obligation, and the checker discharges it through the names
    it can see: a binding, a parameter, a ``ret``, an argument, an arm's value.
    A slot in an aggregate has no name, so a result stored there would be
    dropped without ever being matched — refused where the storage is spelled,
    until places can carry an obligation.
    """
    if isinstance(ty, Res):
        return True
    if isinstance(ty, (Arr, Vec, Opt)):
        return _stored_result(ty.elem)
    return False


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _escapes(ty: Ty) -> bool:
    """Whether ``ty`` is a closure that captured its way into this frame."""
    return isinstance(ty, FnTy) and ty.captures


def _fits(expected: Ty, actual: Ty) -> bool:
    """``compatible`` where ``expected`` is an annotation or a sibling arm.

    An annotation spells a function's signature, not whether the value
    captures: ``(fn (:i64) :i64)`` names both a plain function pointer and a
    capturing closure. The flag follows the value instead — a binding records
    it, and every place the value could leave is E0502 — so ignoring it here
    keeps ``(let f :(fn (:i64) :i64) (fn ...))`` and an ``if`` with one
    capturing arm meaning what they always meant. A call argument is *not*
    compared this way: there the two spellings are different types (E0502).
    """
    if isinstance(expected, FnTy) and isinstance(actual, FnTy):
        return compatible(
            FnTy(expected.params, expected.ret),
            FnTy(actual.params, actual.ret),
        )
    if isinstance(expected, Shared) and not isinstance(actual, Shared):
        # A ``(shared (ptr T))`` annotation is how a bare address becomes the
        # marked value a thread boundary requires. The representation is the
        # same word, so the wrap is free and one-way: a marked value never
        # silently becomes an unmarked one.
        return _shareable(expected.elem) and compatible(expected.elem, actual)
    return compatible(expected, actual)


def _merge_functions(a: Ty, b: Ty) -> Ty:
    """One type for two branches, keeping a capture flag either one carries.

    The two types are already known compatible. The flag is not part of type
    identity, so it has to be merged by hand: if either branch produced a
    capturing closure, the form as a whole may produce one and the value is
    confined no matter which branch runs.
    """
    if isinstance(a, FnTy) and isinstance(b, FnTy) and (a.captures or b.captures):
        return FnTy(a.params, a.ret, True)
    return a


def _at(item: Node | Token, owner: Node) -> str:
    """The path a diagnostic should name: the item's, else its owner's."""
    return item.path if isinstance(item, Node) and item.path else owner.path


def _owner(ty: Ty) -> str:
    """How a message names the set of variants a value of ``ty`` can be."""
    if isinstance(ty, EnumRef):
        return f"enum '{ty.name}'"
    if isinstance(ty, Res):
        return "a result"
    if isinstance(ty, Opt):
        return "an optional"
    return str(ty)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _module_path(module: Module) -> str:
    for item in module.root.items:
        if isinstance(item, Node) and item.kind == "mod":
            return item.path
    return ""


def _cycle(active: list[str], name: str) -> str:
    return " -> ".join(active[active.index(name) :] + [name])
