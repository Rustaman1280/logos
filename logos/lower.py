"""Lower a checked module to the IR.

Scope
-----
This pass lowers what the check accepts: integer literals, ``let``/``letmut``
and ``set``, ``ret``, calls, ``if``, ``while``, ``match``, ``break``/
``continue``, comparisons, ``and``/``or``/``not``, ``print``/``println`` over
every printable scalar, checked integer division, structs (``make``, ``get``,
``set-field``), fixed arrays (``make``, ``arr-get``, ``arr-set``), strings
(``str-len``, ``str-concat``, ``str-eq``, ``str-slice``), vectors
(``make``, ``vec-push``, ``vec-len``, ``vec-get``, ``vec-set``, ``vec-free``)
and the tagged values of results and optionals (``ok``, ``err``, ``some``,
``none``, ``try``), and threads with their synchronization (``spawn``,
``join``, ``mutex``, ``lock``, ``unlock``, ``chan``, ``send``, ``recv``,
``atomic``, ``atomic-add``, ``atomic-load``, ``atomic-store``). A construct the
checker accepts but this pass does not lower yet is reported as a diagnostic at
its own path — never a ``KeyError`` or a ``ValueError``, and never silently
miscompiled.

Results, optionals and enums
----------------------------
A result is ``{ i1, T, E }`` and an optional is ``{ i1, T }``: the tag first,
then one slot per variant. ``(ok v)`` writes the tag and the ok slot,
``(err e)`` the tag and the error slot, the unused slot staying ``undef``;
``(some v)`` and ``(none)`` are the same shape with one slot. A ``match`` over
one extracts the tag, tests it with a single ``icmp``, and reads the payload
with ``extractvalue`` — no memory round trip, because both payloads are fields.

``(try e)`` is the one form that returns: it tests the tag, and on the error
path builds an ``(err e)`` of the *enclosing function's* return type and
executes ``ret`` from the new block. The checker has already proved the two
error types are the same enum, so the payload carries across unchanged. The ok
path extracts the payload and continues in the block the expression was
written in, which is what lets ``(try ...)`` sit inside a longer expression.

An enum is ``{ i32, [K x i64] }`` — a tag and a payload area wide enough for
its widest variant, so every variant is one LLVM type and a value passes
between functions without a new type per variant. ``K`` is registered by the
enclosing declaration, like a struct's field list, and ``sizeof_of`` uses the
same rule. A variant with a payload is built in a stack slot: the tag stored at
field 0, the payload at the first byte of the area through a bitcast pointer,
then the slot loaded as a value. A variant without one is a single
``insertvalue`` of its tag. Binding a payload in an arm reverses that: the
value is stored, the area re-read through a pointer of the variant's own type
and the result bound to a fresh slot. The tag is what makes the different
interpretations safe — only the arm whose test passed reads the area.

Structs and arrays
------------------
A struct is an LLVM identified type whose fields are printed in declaration
order, ``%struct.point = type { i64, i64 }``, which is C's layout: LLVM pads
and aligns an identified struct exactly as C does for the same field list. A
``make`` builds the aggregate as a value with one ``insertvalue`` per field;
``get`` reads one field with ``extractvalue``, so a struct can be nested,
passed, returned and stored without ever taking its address. ``set-field`` and
``arr-set`` mutate a binding in place: they take the binding's slot and store
through a ``getelementptr``. A fixed array is ``[n x T]``; ``arr-get`` spills a
non-binding operand into a slot (LLVM cannot index a value with a run-time
index), bounds-checks the index with one unsigned ``icmp`` and panics
``R0003`` before the ``gep`` and ``load``.

Only structs a function names are emitted, in dependency-first order: one it
materialises, one named in a signature (even only behind a pointer), in a
field, or in an array element. A declaration nothing uses is not emitted, so
it cannot fail a build over a field type this backend cannot spell. A struct
that points at itself is finite and gets one definition; the collection walk
stops at a pointer back to the struct being collected.

Values and slots
----------------
Every binding lives in a named stack slot, and every read of it is an explicit
``load`` into a fresh temporary. That is deliberately unoptimised: clang's
``-O2`` promotes the slots away, and until it does the IR shows one line per
source-level operation. A parameter is stored into its slot on entry, so a
parameter and a ``let`` are read the same way.

Every slot is allocated in the entry block, however deep in the source the form
that introduces it sits. ``alloca`` executes where it appears, so a binding
inside a loop body would otherwise grow the frame on every iteration and a long
loop would run out of stack; allocating once, in the block that runs once, is
what makes a ``let`` inside a loop free.

Binding visibility is block-scoped, matching the checker: the slot exists from
the entry block, but the name is known only inside the block that declared it
(an ``if`` branch, a ``while`` body, a ``match`` arm). Each block saves the
binding table on entry and restores it on exit, so a branch's ``let`` cannot
retarget a later read of the same outer name.

A declared type with no LLVM layout — ``(res T E)``, ``(opt T)`` and the rest
— is E0002 at the node that materialises it, never a ``ValueError`` from the
printer: the type vocabulary is larger than this backend, and the refusal must
carry a path. The walk descends through arrays, vectors *and* pointers, so a
struct behind a ``(ptr T)`` cannot hide a field this backend cannot spell. A
struct, an array or a vector is lowerable when every type inside it is; an enum
is not, until results arrive.

Control flow
------------
``if`` and ``match`` are expressions, so each writes its branch's value into a
result slot and the join block loads it. A slot and a load, rather than a
``phi``, because a branch may end in a terminator (``ret``, ``break``) and a
``phi`` would then name an edge that does not exist; ``-O2`` turns the
slot back into a ``phi``.

``while`` is head/body/exit: the body branches back to the head, and the loop
stack gives ``break`` the exit label and ``continue`` the head label of the
innermost enclosing loop. A condition that is the literal ``true`` branches
straight to the body, with no head-to-exit edge: an exit block that no
``break`` can reach is closed with ``unreachable``, so a function whose
constant-true loop always returns is not reported as missing a ``ret``.
``match`` tests its arms' literals in source order and falls through to a panic
when no arm matches; ``and``/``or`` short-circuit, so the right operand of
``(and false (println "x"))`` never runs.

Concurrency
-----------
A thread, a mutex, a channel and an atomic are one opaque ``i8*`` at the C
boundary; generated code never looks inside one. ``spawn`` builds one heap
record holding the target's arguments (``rt_alloc`` under the spawn's own node
path, so an unjoined thread's handle is reportable at the form that started
it), emits a thunk ``i8* (i8*)`` per call site that unpacks the record and
calls the target, and hands both to ``rt_thread_new``. The runtime owns the
record and releases it when the target returns; ``join`` waits and releases the
handle. ``deref``, ``set-deref`` and ``ptr-add`` through a ``(shared (ptr T))``
are the same operations as through a ``(ptr T)``: the marker is a promise about
who else reaches the memory, not a different representation.

``send`` spills its value into a stack slot and passes the slot's address;
``recv`` gives the runtime a slot and loads what it filled. Both are one
runtime call, and both may block, which is why they are statements in practice
but remain expressions of type ``:void`` / ``T`` respectively. Every runtime
call that can fail carries the node path as a C literal, so R0005 names the
form that asked.

Names
-----
A binding is ``%v.<n>`` and its slot ``%v.<n>.addr``, numbered in declaration
order within the function; a temporary is ``%t<n>``. Source names never reach
the IR, because a Logos atom is not necessarily an LLVM identifier (``?``,
``/`` and non-ASCII are all legal in one), and a diagnostic that blamed the
``.ll`` for invalid text would be lying about where the bug is. Node paths do
not depend on binding names, so nothing else loses information.

A program function is emitted as ``internal @lg.<module>.<name>``; only
``main`` keeps its source name and external linkage. Without the mangle a Logos
``(fn write ...)`` would define ``@write`` and interpose on libc, so a program
calling ``println`` would write nothing.

Division
--------
``/`` and ``%`` are checked, never a bare ``sdiv``/``srem``. Before the
operation the divisor is compared with zero and a zero divisor calls
``rt_panic(path, "R0001", "division by zero")``: LLVM treats a division by zero
as undefined behaviour, so an unchecked one either traps at ``-O0`` or folds
the whole expression to poison at ``-O2``. For signed widths the one overflowing
pair, ``INT_MIN / -1``, takes a branch of its own and yields the wrapped result
without dividing. Unsigned widths use ``udiv``/``urem``.

Strings
-------
A string literal becomes one private global holding exactly its bytes, shared
by every use of the same bytes, and the value of type ``:str`` is the aggregate
``{ i8*, i64 }`` the spec fixes: a pointer to those bytes and their byte count.
A literal builds it with two ``insertvalue`` steps from ``undef``; a ``:str``
parameter, return or binding carries the whole aggregate, so one string can be
passed through any number of functions without the pointer and the length
coming apart. ``print``/``println`` hand the two fields to ``rt_print_str``,
never a ``printf`` format: the length is the value's own, not a scan for a
terminator that was never written. A panic argument is the one exception: it is
a NUL-terminated C string, because ``rt_panic`` reads it with ``strlen``.

``str-len`` reads the length field and costs nothing else. ``str-slice`` builds
a view — the same buffer one ``gep`` along, with a shorter length — and copies
no bytes; it checks both bounds against the length and against each other with
three unsigned compares, then panics ``R0003``. ``str-concat`` is one call to
``rt_str_concat``, which allocates the result for the process's life: a
``:str`` owns nothing, so there is no one to hand the buffer back. ``str-eq``
and ``=``/``!=`` on ``:str`` are the same lowering: compare the lengths, and
only when they agree compare the bytes with ``memcmp``. ``!=`` inverts that
answer. ``read-all`` is one call to ``rt_read_all``, which reads standard input
to end of file into a buffer of the same kind: process-lifetime, untracked,
and released by nobody because no binding owns it.

Vectors
-------
A ``(vec T)`` is three words, ``{ T*, i64, i64 }``: the buffer, its length in
elements and its capacity. ``(make (vec T))`` builds the empty descriptor —
null buffer, two zero counts — and every read is an ``extractvalue`` of the
descriptor the binding holds.

``vec-push`` may have to grow, so it is the one form here with a control-flow
cost: it tests ``len == cap``, and on the full path allocates a new buffer with
``rt_alloc``, copies the live elements with ``memcpy``, releases the old buffer
with ``rt_free``, and joins with a ``phi`` carrying the new buffer and capacity.
Capacity goes to twice itself plus four, so the first push allocates four
elements and the copies a program pays for are logarithmic. Allocate and copy
rather than ``realloc`` because every block's record belongs to the leak
tracker: a buffer handed to ``realloc`` would leave the tracker describing an
address it no longer controls, and a freed buffer would look live. Growth also
checks that the new capacity is larger than the old one, so a wrapped value can
never pass as a bigger buffer.

The mutating forms — ``vec-push``, ``vec-set``, ``vec-free`` — name a place
whose slot holds the descriptor: a ``letmut`` binding or a parameter, both of
which have an alloca this function can write back into. ``vec-get`` and
``vec-len`` read any vector value, so a vector may be passed to a function and
read there, and a parameter is a place for the three mutating forms because
the callee owns what the caller moved into it — it is the one that has to be
able to release the buffer. ``vec-free`` releases the buffer and stores a
zeroed descriptor, which makes a second free release NULL and a later length
read zero.

Memory
------
``(alloc T n)`` becomes ``rt_alloc(bytes, align, path)`` — the byte count is
``n`` times the element's layout, the alignment is the element's own, and the
path is a compiler-generated C string the leak report prints at exit. The
result crosses the C boundary once, as ``i8*``, and is ``bitcast`` to ``T*``;
``(own T)`` and ``(ptr T)`` are both ``T*`` in the IR, because the difference
between them is what the checker knows, not what is stored.

``(deref p)`` is a typed ``load`` and ``(set-deref p v)`` a typed ``store``;
``(ptr-add p n)`` is a ``gep`` whose stride is the pointee's size. ``(ptr p)``
emits nothing at all: it is the same machine value under a type that cannot
free it. A move emits nothing either — the pointer is copied where the checker
allowed it and the old binding is never read again — so ownership costs no
instructions and the release is exactly the ``free`` the source spelled.

A debug build (a test build, by default) calls ``rt_leak_report`` on every path
out of ``main``, after the exit status is computed; a release build does not.
A debug build also defines ``LOGOS_DEBUG`` for the C runtime, which quarantines
every released block instead of returning it to malloc: a freed address then
cannot be recycled, so the double-free check in ``rt_free`` stays exact. The
runtime, not the compiler, decides what a live block is, which is why a double
free is caught there: a copy of a pointer the checker could not see — through
an aggregate, or from C — still reaches ``rt_free`` twice.

Generics
--------
A generic function has no body of its own here: the checker's copies do. Each
``Instance`` is lowered as its own function at ``lg.<module>.<name>$<type
arguments>`` — ``lg.m.map$i64$i64`` — with the copy's concrete parameters and
return type, and its own type record, so one source path can mean ``:i64`` in
one copy and ``:str`` in the next. A call site's copy is the one the checker
recorded for that path; anything else would be a second, silent instantiation
the checker never verified. A generic that no call reaches is not emitted at
all: there is no type to emit it at.

Closures
--------
``(fn :R ((p :T)...) body...)`` becomes a function of its own plus a value of
two words, ``{ i8*, i8* }``: the environment and the code. The code's address
is taken with ``faddr`` and the environment's with ``allocarec``/``geprec`` —
one stack slot holding a copy of each captured binding, written where the
expression appears and read through the pointer inside the closure. Stack, not
heap, because the language allocates nothing the source did not spell, and a
copy at creation is what "capture by value" means: a later ``set`` of the outer
binding reaches the binding, not the copy.

The environment therefore lives exactly as long as the frame that created it;
nothing here frees it, and nothing extends it. The checker refuses every form
that would let a capturing closure outlive its frame (E0502): returning it,
storing it in a field, an array or a vector, passing it to another function,
or keeping it past the loop iteration that made it. The one copy per
expression per frame is why the iteration rule exists, and the backend can
rely on the checker having applied it: no value it lowers reads a slot after
the frame that owns it. A capturing value has the type ``(fn* (T) R)``; a
``(fn (T) R)`` is a plain pointer with a null environment.

Calling one is indirect: the value is loaded, its two words are extracted, the
code pointer is ``bitcast`` to its real type — ``R (i8*, T...)``, the
environment first — and the call passes the environment ahead of the declared
arguments. A named function used as a value is the same pair with a null
environment, so both cross the boundary the same way.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import struct
from typing import Iterator

from logos import llvm
from logos.check import (
    BUILTINS,
    DIRECTIVES,
    RESULT_FORMS,
    CheckedModule,
    Closure,
    Instance,
    PRINTABLE,
    Types,
    fn_param_nodes,
    fn_shape,
    is_wildcard,
    let_shape,
    match_shape,
    pattern_variant,
)
from logos.diag import (
    ARRAY_BOUNDS,
    DIVISION_BY_ZERO,
    INDEX_TYPE,
    LITERAL_RANGE,
    MATCH_NO_ARM,
    MISSING_RETURN,
    NOT_A_STR,
    NOT_FOUND,
    TRY_MISMATCH,
    TYPE_MISMATCH,
    UNKNOWN_FIELD,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    UNKNOWN_TYPE,
    WRONG_ARITY,
    diag,
)
from logos.ir import (
    TRIPLE,
    IrBlock,
    IrDecl,
    IrEnum,
    IrFn,
    IrGlobal,
    IrModule,
    IrStruct,
)
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.types import (
    INTEGERS,
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
    Vec,
    compatible,
)

#: Logos arithmetic to LLVM opcode. ``/`` and ``%`` are not here: they are
#: checked before they are emitted, and their opcode depends on signedness.
_ARITH = {"+": "add", "-": "sub", "*": "mul"}

#: The float spellings of the same four operations. IEEE arithmetic has no
#: wrapping and no trap, so every one of them is a single instruction: ``+``,
#: ``-`` and ``*`` map straight across, and ``/`` is a bare ``fdiv`` — dividing
#: by zero yields ``inf``/``nan`` under IEEE 754, which is a value, not a panic.
#: ``%`` is not here at all: the checker refuses it on a float, because there is
#: no one answer to what it should mean (fmod or IEEE remainder).
_FLOAT_ARITH = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv"}

#: Logos comparison to ``fcmp`` predicate. The ordered predicates are false
#: when either operand is NaN, which is the IEEE 754 meaning of ``<`` and ``<=``;
#: ``!=`` is ``une``, the exact negation of ``=``, so a NaN is unequal to
#: everything including itself. Nothing here is fast-math: no flag is added.
_FLOAT_COMPARE = {
    "=": "oeq",
    "!=": "une",
    "<": "olt",
    "<=": "ole",
    ">": "ogt",
    ">=": "oge",
}

#: Logos comparison to ``icmp`` predicate. The ordered ones are the signed
#: spellings; an unsigned operand turns ``slt`` into ``ult`` and so on, because
#: the width, not the operator, decides what the bits mean.
_COMPARE = {"=": "eq", "!=": "ne", "<": "slt", "<=": "sle", ">": "sgt", ">=": "sge"}

#: The two operations that can trap.
_DIVIDE = ("/", "%")

#: Short-circuiting boolean forms. ``not`` is not here: it always evaluates
#: its one operand and is a single ``icmp``.
_LOGICAL = ("and", "or")

#: Forms that are statements: a value position for one of these is a mistake,
#: because it produces nothing (or, for ``while``, no result a caller can use).
_STATEMENTS = (
    "let",
    "letmut",
    "set",
    "ret",
    "while",
    "break",
    "continue",
    "set-field",
    "arr-set",
    "vec-push",
    "vec-set",
    "vec-free",
    "free",
    "set-deref",
)

#: The one aggregate this backend builds by hand: the ``:str`` the spec fixes.
_STR = Prim("str")
_PTR_U8 = Ptr(Prim("u8"))

#: ``INT_MIN`` for every signed width, the dividend of the one overflowing
#: division. ``INT_MIN / -1`` wraps to ``INT_MIN`` on x86-64; the hardware would
#: fault, so the lowerer never lets that pair reach an ``idiv``.
_INT_MIN = {
    "i8": -(2**7),
    "i16": -(2**15),
    "i32": -(2**31),
    "i64": -(2**63),
}

#: ``rt_panic``'s C signature: ``_Noreturn void (const char*, const char*, const char*)``.
#: All three arguments are NUL-terminated C strings the compiler emits itself.
_RT_PANIC = IrDecl(
    "rt_panic",
    (Ptr(Prim("u8")), Ptr(Prim("u8")), Ptr(Prim("u8"))),
    Prim("void"),
)

#: ``rt_alloc``'s C signature: ``void *(size_t, size_t, const char*)``. The size
#: is in bytes and the third argument is the node path of the ``alloc``, a
#: NUL-terminated literal the leak report prints back at exit.
_RT_ALLOC = IrDecl(
    "rt_alloc", (Prim("i64"), Prim("i64"), Ptr(Prim("u8"))), Ptr(Prim("u8"))
)

#: ``void rt_free(void *)`` — the block goes back to the allocator.
_RT_FREE = IrDecl("rt_free", (Ptr(Prim("u8")),), Prim("void"))

#: ``void rt_leak_report(void)`` — one line per live block, at the end of main.
_RT_LEAK_REPORT = IrDecl("rt_leak_report", (), Prim("void"))

#: ``i8*`` is the C boundary's ``void *``; a Logos pointer is ``T*`` and crosses
#: it with one ``bitcast``.
_VOID_PTR = Ptr(Prim("u8"))

#: ``main`` returns a C exit status, whatever the Logos program says.
_ENTRY_RET = Prim("i32")

#: The printers. Each takes the value in its own native type — an ``i8*`` plus
#: a length, an ``i64``, an ``unsigned long long`` (``i64`` in the IR, which has
#: no signedness), a ``double`` — and writes formatted bytes to stdout.
_RT_PRINT_STR = IrDecl("rt_print_str", (_VOID_PTR, Prim("i64")), Prim("void"))
_RT_PRINT_I64 = IrDecl("rt_print_i64", (Prim("i64"),), Prim("void"))
_RT_PRINT_U64 = IrDecl("rt_print_u64", (Prim("u64"),), Prim("void"))
_RT_PRINT_F64 = IrDecl("rt_print_f64", (Prim("f64"),), Prim("void"))
_RT_PRINTLN = IrDecl("rt_println", (), Prim("void"))

#: ``unsigned char *rt_str_concat(a, an, b, bn, size_t *out_len)``. The length
#: comes back through a pointer rather than in a returned struct: a three-word
#: return would be lowered to an ``sret`` argument by the ABI, and a signature
#: of scalars and pointers crosses it the same way in both languages.
_RT_STR_CONCAT = IrDecl(
    "rt_str_concat",
    (_VOID_PTR, Prim("i64"), _VOID_PTR, Prim("i64"), Ptr(Prim("i64"))),
    _VOID_PTR,
)

#: ``unsigned char *rt_read_all(size_t *out_len, const char *path)``. The whole
#: of standard input, in a buffer the runtime owns for the process's life — the
#: same ownership ``rt_str_concat`` gives its result, for the same reason: a
#: ``:str`` owns nothing, so a buffer it could not release would either leak or
#: be blamed on a program that cannot free it. The length comes back through a
#: slot, and ``path`` names the ``read-all`` node for a failed read (R0003).
_RT_READ_ALL = IrDecl("rt_read_all", (Ptr(Prim("i64")), Ptr(Prim("u8"))), _VOID_PTR)

#: ``int memcmp(const void *a, const void *b, size_t n)``: the byte comparison
#: inside ``str-eq``. Declared with its real signature so the call is not a
#: lie about the C ABI; the result is the sign of the difference, and only its
#: equality with zero is read.
_MEMCMP = IrDecl("memcmp", (_VOID_PTR, _VOID_PTR, Prim("i64")), Prim("i32"))

#: ``void *memcpy(void *dst, const void *src, size_t n)``: what a vector's
#: growth uses to carry the old elements into the new buffer.
_MEMCPY = IrDecl("memcpy", (_VOID_PTR, _VOID_PTR, Prim("i64")), _VOID_PTR)

#: The concurrency runtime. Every handle crosses as ``i8*``; a ``path`` is a
#: compiler-generated NUL-terminated literal for a failure that can name a
#: node. ``rt_thread_new`` takes the path (the argument record is allocated
#: with it, so an unjoined thread's record is named at the spawn), the thunk's
#: address and the argument record.
_RT_THREAD_NEW = IrDecl(
    "rt_thread_new", (_VOID_PTR, _VOID_PTR, _VOID_PTR), _VOID_PTR
)
_RT_JOIN = IrDecl("rt_join", (_VOID_PTR, _VOID_PTR), Prim("void"))
_RT_MUTEX_NEW = IrDecl("rt_mutex_new", (_VOID_PTR,), _VOID_PTR)
_RT_MUTEX_LOCK = IrDecl("rt_mutex_lock", (_VOID_PTR, _VOID_PTR), Prim("void"))
_RT_MUTEX_UNLOCK = IrDecl("rt_mutex_unlock", (_VOID_PTR, _VOID_PTR), Prim("void"))
_RT_CHAN_NEW = IrDecl(
    "rt_chan_new", (Prim("i64"), Prim("i64"), _VOID_PTR), _VOID_PTR
)
_RT_CHAN_SEND = IrDecl(
    "rt_chan_send", (_VOID_PTR, _VOID_PTR, _VOID_PTR), Prim("void")
)
_RT_CHAN_RECV = IrDecl(
    "rt_chan_recv", (_VOID_PTR, _VOID_PTR, _VOID_PTR), Prim("void")
)
#: An atomic is one 64-bit word. ``:u64`` crosses as the same ``i64``: LLVM
#: has no signedness, and the checker has already refused every other width.
_RT_ATOMIC_NEW = IrDecl("rt_atomic_new", (Prim("i64"),), _VOID_PTR)
_RT_ATOMIC_ADD = IrDecl(
    "rt_atomic_add", (_VOID_PTR, Prim("i64")), Prim("i64")
)
_RT_ATOMIC_LOAD = IrDecl("rt_atomic_load", (_VOID_PTR,), Prim("i64"))
_RT_ATOMIC_STORE = IrDecl(
    "rt_atomic_store", (_VOID_PTR, Prim("i64")), Prim("void")
)


def lowered_type(
    ty: Ty,
    path: str,
    structs: dict[str, tuple[tuple[str, Ty], ...]] | None = None,
    enums: dict[str, tuple[tuple[str, Ty | None], ...]] | None = None,
) -> Ty:
    """Return ``ty`` if the printer has a layout for it, else raise E0002.

    The checker's type vocabulary is larger than this backend's: a parameter,
    binding or result may name ``(vec :i64)``, ``(own T)`` or ``(shared T)``
    and still type-check. Materialising one would reach
    :func:`logos.llvm.type_text` and escape as a bare ``ValueError`` with no
    node path, so every place that materialises a type passes through here and
    the refusal is a diagnostic at the node that named it.

    A struct, an enum, a result, an optional or an array is lowerable only when
    every type inside it is, so ``structs`` and ``enums`` supply the
    declarations and the walk descends. A struct that contains itself by value
    — directly, or through an enum payload — is refused too, because no finite
    LLVM type spells an infinite one.
    """
    bad, cycle = _unlowered(ty, structs or {}, enums or {}, ())
    if cycle is not None:
        raise diag(UNKNOWN_NODE, path, f"struct '{cycle}' contains itself by value")
    if bad is not None:
        raise diag(UNKNOWN_NODE, path, f"{bad} is not lowered yet")
    return ty


def _unlowered(
    ty: Ty,
    structs: dict[str, tuple[tuple[str, Ty], ...]],
    enums: dict[str, tuple[tuple[str, Ty | None], ...]],
    stack: tuple[str, ...],
) -> tuple[Ty | None, str | None]:
    """The first type in ``ty`` with no layout, and any value cycle found.

    A pointer is followed rather than accepted as spellable on its own:
    ``(ptr bad)`` prints as ``%struct.bad*`` even when ``bad`` has no
    definition, so a struct whose field is a ``(vec :i64)`` would reach the
    printer and escape as a bare ``ValueError`` with no node path. Descending
    turns that into E0002 at the node that named the pointer.

    A cycle reached *through* a pointer is finite — ``(struct (head (ptr
    node)))`` is one word — so it is not reported; the same cycle by value is.
    A vector is a pointer and two counts, so a cycle reached through one is
    finite too: ``(vec node)`` inside ``node`` is not an infinite type. (A
    vector cannot be a field at all — the checker refuses that before this
    runs — so the case is here to keep the message true, not to allow it.)
    """
    if isinstance(ty, (Ptr, Own)):
        bad, _ = _unlowered(ty.elem, structs, enums, stack)
        return bad, None
    if isinstance(ty, Vec):
        # A vector's element has to have a layout: ``(vec :void)`` has no
        # storage for an element, and ``(vec (shared :i64))`` would print a
        # type the backend cannot spell. The stack still grows through the
        # element so the walk terminates, but a cycle it finds is not reported:
        # it is one pointer deep, not a type that contains itself by value.
        if _is_void(ty.elem):
            return ty.elem, None
        bad, _ = _unlowered(ty.elem, structs, enums, stack)
        return bad, None
    if isinstance(ty, Shared):
        # A marker, not a layout: the value is the one it wraps.
        return _unlowered(ty.elem, structs, enums, stack)
    if isinstance(ty, (Chan, Atomic)):
        # An opaque handle is one word; the element type is never materialised
        # in this program's storage, but it still has to be spellable so the
        # operations that carry it can be typed.
        if _is_void(ty.elem):
            return ty.elem, None
        bad, _ = _unlowered(ty.elem, structs, enums, stack)
        return bad, None
    if isinstance(ty, Opt):
        # ``{ i1, T }``: the tag needs no room, but T is stored beside it and
        # has to have a layout, and ``(opt :void)`` has nothing to store.
        if _is_void(ty.elem):
            return ty.elem, None
        return _unlowered(ty.elem, structs, enums, stack)
    if isinstance(ty, Res):
        # ``{ i1, T, E }``: both slots are storage, whatever variant the value
        # actually is, so both are walked.
        for part in (ty.ok, ty.err):
            if _is_void(part):
                return part, None
            bad, cycle = _unlowered(part, structs, enums, stack)
            if bad is not None or cycle is not None:
                return bad, cycle
        return None, None
    if isinstance(ty, EnumRef):
        if ty.name in stack:
            return None, ty.name
        variants = enums.get(ty.name)
        if variants is None:
            return ty, None
        for _, payload in variants:
            if payload is None:
                continue
            if _is_void(payload):
                return payload, None
            bad, cycle = _unlowered(payload, structs, enums, stack + (ty.name,))
            if bad is not None or cycle is not None:
                return bad, cycle
        return None, None
    if isinstance(ty, Arr):
        if _is_void(ty.elem):
            return ty.elem, None
        return _unlowered(ty.elem, structs, enums, stack)
    if isinstance(ty, FnTy):
        # A function value is two opaque words, but the type it is called
        # through is spelled at the call: every parameter and the result must
        # have a layout, or the indirect call would print a type clang refuses.
        for param in ty.params:
            if _is_void(param):
                # A parameter is a storage location, and no ``void`` value
                # exists to put in it.
                return param, None
            bad, cycle = _unlowered(param, structs, enums, stack)
            if bad is not None or cycle is not None:
                return bad, cycle
        return _unlowered(ty.ret, structs, enums, stack)
    try:
        llvm.type_text(ty)
    except ValueError:
        return ty, None
    if isinstance(ty, StructRef):
        if ty.name in stack:
            return None, ty.name
        fields = structs.get(ty.name)
        if fields is None:
            return ty, None
        for _, field in fields:
            if _is_void(field):
                # A field is a storage location, and no ``void`` value exists
                # to put in it.
                return field, None
            bad, cycle = _unlowered(field, structs, enums, stack + (ty.name,))
            if bad is not None or cycle is not None:
                return bad, cycle
    return None, None


def lower_module(cm: CheckedModule, *, debug: bool = False) -> IrModule:
    """Lower every function of ``cm``; a module with a ``main`` is a program.

    Tests are compiled out: they are grammar nodes for the harness, and a
    build must not carry their bodies into the binary. ``debug`` adds the leak
    report at the exit of ``main``; a release build runs without the tracker's
    output, because a program that leaks is a program the author has decided
    to run as it is.

    A generic definition has no body here — only the copies the checker made
    for its call sites do, and they are lowered after every ordinary function.
    """
    lowering = _Lower(cm, debug=debug)
    for name, node in cm.module.fns.items():
        if name in cm.generic:
            continue
        lowering.fn(name, node)
    for instance in cm.instances.values():
        lowering.instance(instance)
    return lowering.close()


def lower_test(cm: CheckedModule, test_name: str, *, debug: bool = True) -> IrModule:
    """Lower one ``(test ...)`` body into ``main``.

    The body's harness directives are skipped, because they are instructions to
    the runner and not expressions. Everything else is ordinary grammar.

    The module's own functions are lowered too: a test body may call one, and a
    call with no definition is a diagnostic rather than a symbol the linker
    would fail to find. A module-level ``main`` is skipped, because the test
    body is the entry point and two definitions of ``@main`` cannot coexist.

    A test build is a debug build: its ``main`` reports every live block at
    exit, which is what lets a ``(leaks 0)`` expectation be checked.
    """
    node = cm.module.tests.get(test_name)
    if node is None:
        raise diag(
            NOT_FOUND,
            "",
            f"no test named '{test_name}' in module '{cm.module.name}'",
        )
    lowering = _Lower(cm, debug=debug)
    lowering.symbols.pop("main", None)
    for name, fn_node in cm.module.fns.items():
        if name == "main" or name in cm.generic:
            continue
        lowering.fn(name, fn_node)
    for instance in cm.instances.values():
        lowering.instance(instance)
    fn = IrFn("main", [], _ENTRY_RET, [IrBlock("entry")], symbol="main")
    builder = _Fn(lowering, fn, path=node.path, ret=Prim("void"), is_main=True)
    builder.body(
        [
            item
            for item in node.items[2:]
            if not (isinstance(item, Node) and item.kind in DIRECTIVES)
        ]
    )
    builder.finish()
    lowering.ir.functions.append(fn)
    return lowering.close()


class _Lower:
    """One module's shared state: its globals, its declarations, its IR."""

    def __init__(self, cm: CheckedModule, *, debug: bool = False) -> None:
        # The layout registry answers by name alone, so it describes exactly
        # one module at a time: drop whatever an earlier module left there
        # before this one registers its structs.
        llvm.reset_structs()
        self.cm = cm
        self.debug = debug
        self.ir = IrModule(TRIPLE, [], [])
        self.strings: dict[bytes, str] = {}
        self.declares: list[IrDecl] = []
        #: Structs a function materialises, in dependency-first order. A
        #: declaration nothing uses is not emitted: its layout may name a type
        #: this backend cannot spell, and a module should not fail to build
        #: over a struct it never touched.
        self.structs: dict[str, tuple[tuple[str, Ty], ...]] = {}
        #: Every enum a function materialises, by name, with its payload width
        #: in eight-byte words. Like ``structs`` it is dependency-first and only
        #: holds what was used: a declaration nothing touches is not emitted.
        self.enum_widths: dict[str, int] = {}
        #: The type record shared by everything that is not a copy: the
        #: module's own functions and its tests.
        self.body = Types(
            cm.expr_types,
            cm.call_targets,
            cm.closures,
            cm.declared,
            cm.literal_types,
        )
        #: Every source function this module can call, mapped to its symbol.
        #: Built before any body is lowered, so source order is irrelevant and
        #: a call to a name that is not here is E0103 in the lowerer. A generic
        #: definition is left out: it has no body of its own, and calling it
        #: goes through the copy the checker recorded for the call site.
        self.symbols: dict[str, str] = {
            name: self.symbol(name)
            for name in cm.module.fns
            if name not in cm.generic
        }
        #: The ``$lambda$<n>`` names closures have been given so far.
        self.lambdas = 0
        self.lambda_names: set[str] = set()
        #: The wrapper each named function used as a value got, by source name.
        self.wrappers: dict[str, str] = {}

    def symbol(self, name: str) -> str:
        """The linker-visible symbol for a program function.

        ``main`` keeps its name and its external linkage: it is the C entry
        point. Everything else is internal and mangled, so no source name can
        interpose on libc or on the runtime.
        """
        if name == "main":
            return "main"
        return f"lg.{self.cm.module.name}.{name}"

    def intern(self, data: bytes) -> str:
        """The global holding ``data``, created on first use."""
        name = self.strings.get(data)
        if name is None:
            name = f".str{len(self.strings)}"
            self.strings[data] = name
            self.ir.globals.append(IrGlobal(name, data))
        return name

    def c_string(self, text: str) -> str:
        """The global holding ``text`` as a NUL-terminated C string."""
        return self.intern(text.encode("utf-8") + b"\0")

    def variant_tag(
        self, name: str, where: str
    ) -> tuple[EnumRef, int, Ty | None] | None:
        """The enum, tag index and payload of a variant name, or ``None``.

        The checker has already resolved a constructor's name and refused an
        ambiguous one; this answers for the backend from the declarations it
        lowered, and reports the same ambiguity rather than picking an enum,
        because a pick would build a value of a type the checker never saw.
        """
        found: list[tuple[str, int, Ty | None]] = []
        for enum, variants in self.cm.enums.items():
            for index, (variant, payload) in enumerate(variants):
                if variant == name:
                    found.append((enum, index, payload))
        if not found:
            return None
        if len(found) > 1:
            raise diag(
                NOT_FOUND,
                where,
                f"variant '{name}' is declared by more than one enum ("
                + ", ".join(sorted(enum for enum, _, _ in found))
                + ")",
            )
        enum, index, payload = found[0]
        return (EnumRef(enum), index, payload)

    def declare(self, decl: IrDecl) -> None:
        if decl not in self.declares:
            self.declares.append(decl)

    def use_type(self, ty: Ty, path: str) -> None:
        """Refuse a type with no layout, and record the types it needs.

        Validation is the public :func:`lowered_type`, which descends into
        struct fields, enum payloads and array elements. Registration is
        separate and post-order, so ``%struct.line`` is printed after
        ``%struct.pair``, the type it contains: a definition may then be read
        top to bottom. An enum is registered before validation because its
        layout is ``{ i32, [K x i64] }`` and ``K`` is a fact about the
        declaration the type spelling cannot read for itself.
        """
        self._register_enums(ty)
        lowered_type(ty, path, self.cm.structs, self.cm.enums)
        self._collect(ty)

    def _register_enums(self, ty: Ty, stack: tuple[str, ...] = ()) -> None:
        """Record the payload width of every enum ``ty`` names, innermost first.

        ``K`` is the largest payload of the enum, in eight-byte words, so a
        payload that is itself an enum has to be measured first. ``stack``
        holds the type names being walked, which is what stops a struct that
        points at itself from walking forever.
        """
        if isinstance(ty, EnumRef):
            if ty.name in stack:
                return
            finished = self.enum_widths.get(ty.name)
            if finished is not None:
                # Measured already: re-walking the payloads would be the same
                # answer, and a payload graph that reached back here would
                # never finish (the checker refuses such an enum, but the
                # lowerer must not depend on that to terminate).
                llvm.register_enum(ty.name, finished)
                return
            variants = self.cm.enums.get(ty.name)
            if variants is None:
                return
            for _, payload in variants:
                if payload is not None:
                    self._register_enums(payload, stack + (ty.name,))
            words = enum_words(
                ty.name, self.cm.enums, self.cm.structs, self.enum_widths
            )
            llvm.register_enum(ty.name, words)
            self.enum_widths[ty.name] = words
        elif isinstance(ty, (Arr, Ptr, Own, Vec, Opt, Shared, Chan, Atomic)):
            self._register_enums(ty.elem, stack)
        elif isinstance(ty, Res):
            self._register_enums(ty.ok, stack)
            self._register_enums(ty.err, stack)
        elif isinstance(ty, FnTy):
            for param in ty.params:
                self._register_enums(param, stack)
            self._register_enums(ty.ret, stack)
        elif isinstance(ty, StructRef):
            if ty.name in stack:
                return
            for _, field in self.cm.structs.get(ty.name, ()):
                self._register_enums(field, stack + (ty.name,))

    def _collect(self, ty: Ty, stack: tuple[str, ...] = ()) -> None:
        """Record every struct and enum ``ty`` names, dependencies first.

        A struct reached only through a pointer is collected too: a signature,
        a field or an array element names it as ``%struct.point*``, and clang
        refuses a use of an identified type that is never defined. ``stack``
        holds the types currently being collected, so a pointer back to one of
        them stops there instead of walking forever: a struct that contains a
        pointer to itself is finite (``%struct.node = type { %struct.node* }``),
        unlike a struct that contains itself by value.

        An enum is collected through its payloads as well: an enum whose
        payload names a struct spells that struct's identified type inside the
        value, so the definition has to be emitted too. It has no definition
        line of its own — ``{ i32, [K x i64] }`` is a literal.
        """
        if isinstance(ty, StructRef):
            if ty.name in self.structs or ty.name in stack:
                return
            fields = self.cm.structs.get(ty.name)
            if fields is None:
                return
            for _, field in fields:
                self._collect(field, stack + (ty.name,))
            self.structs[ty.name] = fields
            llvm.register_struct(ty.name, tuple(fty for _, fty in fields))
        elif isinstance(ty, EnumRef):
            if ty.name in stack:
                return
            for _, payload in self.cm.enums.get(ty.name, ()):
                if payload is not None:
                    self._collect(payload, stack + (ty.name,))
        elif isinstance(ty, (Arr, Ptr, Own, Vec, Opt, Shared, Chan, Atomic)):
            self._collect(ty.elem, stack)
        elif isinstance(ty, Res):
            self._collect(ty.ok, stack)
            self._collect(ty.err, stack)
        elif isinstance(ty, FnTy):
            for param in ty.params:
                self._collect(param, stack)
            self._collect(ty.ret, stack)

    def next_name(self, prefix: str) -> str:
        """A fresh source-level name for a generated function: ``$lambda$1``.

        ``$`` and the counter keep it apart from any ordinary definition, and
        the loop skips a source name that happens to spell the same thing.
        """
        while True:
            self.lambdas += 1
            name = f"{prefix}${self.lambdas}"
            if name not in self.symbols and name not in self.lambda_names:
                self.lambda_names.add(name)
                return name

    def next_lambda(self) -> str:
        """A fresh name for one closure's function."""
        return self.next_name("$lambda")

    def fn(self, name: str, node: Node) -> None:
        ret, params, start = fn_shape(node, self.cm.types)
        # The signature is the first place a declared type is materialised:
        # refusing it here keeps the function's ``define`` line from reaching
        # the printer with a type it cannot spell.
        self.use_type(ret, node.path)
        param_paths = [param.path for param in fn_param_nodes(node)]
        is_main = name == "main"
        if is_main:
            if params:
                raise diag(TYPE_MISMATCH, node.path, "'main' takes no parameters")
            if not (_is_void(ret) or _is_i64(ret)):
                raise diag(
                    TYPE_MISMATCH,
                    node.path,
                    f"'main' must return :void or :i64, found {ret}",
                )
            fn = IrFn("main", [], _ENTRY_RET, [IrBlock("entry")], symbol="main")
        else:
            fn = IrFn(
                name,
                [],
                ret,
                [IrBlock("entry")],
                symbol=self.symbol(name),
                internal=True,
            )
        builder = _Fn(self, fn, path=node.path, ret=ret, is_main=is_main)
        builder.parameters(params, param_paths)
        builder.body(list(node.items[start:]))
        builder.finish()
        self.ir.functions.append(fn)

    def instance(self, instance: Instance) -> None:
        """Lower one monomorphised copy of a generic function.

        The copy's parameters and return type are the checker's, not the
        signature's: the signature still names type variables, and the copy has
        none. Its body is lowered from the same source node as the original,
        but its type record is the copy's, so every path resolves to the types
        this instantiation fixed.
        """
        node = self.cm.module.fns[instance.fn]
        _, params, start = fn_shape(node, self.cm.types)
        self.use_type(instance.ret, node.path)
        for param_ty in instance.params:
            self.use_type(param_ty, node.path)
        fn = IrFn(
            instance.name,
            [],
            instance.ret,
            [IrBlock("entry")],
            symbol=self.symbol(instance.name),
            internal=True,
        )
        builder = _Fn(
            self,
            fn,
            path=node.path,
            ret=instance.ret,
            is_main=False,
            body=instance.body,
        )
        builder.parameters(
            [(name, ty) for (name, _), ty in zip(params, instance.params)],
            [param.path for param in fn_param_nodes(node)],
        )
        builder.body(list(node.items[start:]))
        builder.finish()
        self.ir.functions.append(fn)

    def close(self) -> IrModule:
        """Finish the module: its struct and enum definitions, then its declares."""
        self.ir.structs = [
            IrStruct(name, tuple(fty for _, fty in fields))
            for name, fields in self.structs.items()
        ]
        self.ir.enums = [
            IrEnum(name, words) for name, words in self.enum_widths.items()
        ]
        self.ir.declares = list(self.declares)
        return self.ir


class _Fn:
    """One function's lowering: its blocks, its slots, its temporaries.

    ``ret`` is the type the Logos signature declares, which for ``main`` is
    ``:void`` or ``:i64`` even though the IR entry point returns ``i32``.
    """

    def __init__(
        self,
        lowering: _Lower,
        fn: IrFn,
        *,
        path: str,
        ret: Ty,
        is_main: bool,
        body: Types | None = None,
    ) -> None:
        self.lowering = lowering
        self.cm = lowering.cm
        self.fn = fn
        self.path = path
        self.ret = ret
        self.is_main = is_main
        self.record = body if body is not None else lowering.body
        self.block = fn.blocks[0]
        self.count = 0
        self.bindings = 0
        self.labels = 0
        self.vars: dict[str, tuple[Ty, str]] = {}
        #: Slots waiting for their ``alloca``, which always lands in ``entry``.
        self.allocas: list[tuple] = []
        #: The enclosing loops, innermost last: ``(exit label, head label)``.
        self.loops: list[tuple[str, str]] = []
        #: Exit labels some ``break`` targets, so a constant-true loop whose
        #: exit is never broken out of can close that block as unreachable.
        self.escapes: set[str] = set()

    # -- names and emission ---------------------------------------------

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Lower the form inside ``with`` as one nested binding scope.

        Every slot is still allocated in ``entry``; only the name table is
        saved and restored. A binding declared in an ``if`` branch therefore
        cannot leave the branch's slot attached to an outer name, which is
        what would have made a later ``set`` store into the wrong slot.
        """
        saved = dict(self.vars)
        try:
            yield
        finally:
            self.vars = saved

    def temp(self) -> str:
        """A fresh temporary. ``%t<n>`` cannot collide with ``%v.<n>``."""
        self.count += 1
        return f"%t{self.count}"

    def binding(self) -> str:
        """A fresh, IR-safe local for one source binding: ``%v.<n>``."""
        name = f"%v.{self.bindings}"
        self.bindings += 1
        return name

    def slot(self, ty: Ty, owner: str | None = None, *, path: str) -> str:
        """One stack slot of ``ty``, allocated in the entry block.

        ``owner`` is the binding name to derive the slot from, when there is
        one; a slot for a result the source never named gets a fresh number.
        The ``alloca`` itself is held back and lands in ``entry`` (see
        :meth:`finish`), so a form inside a loop allocates once. ``path`` is
        the node that named the type, so a type with no layout is E0002 there.
        """
        # Record the struct definitions the slot's type needs, and refuse a
        # field type with no layout, at the node that named the slot.
        self.lowering.use_type(ty, path)
        name = owner if owner is not None else self.binding()
        address = f"{name}.addr"
        self.allocas.append(("alloca", address, ty))
        return address

    def new_block(self, label: str) -> None:
        """Start a new block in this function and make it current."""
        self.block = IrBlock(label)
        self.fn.blocks.append(self.block)

    def emit(self, instr: tuple) -> None:
        self.block.instrs.append(instr)

    def parameters(self, params: list[tuple[str, Ty]], paths: list[str]) -> None:
        for (source, ty), path in zip(params, paths):
            safe = self.binding()
            slot = self.slot(ty, safe, path=path)
            self.emit(("store", ty, safe, slot))
            self.vars[source] = (ty, slot)
            self.fn.params.append((safe.lstrip("%"), ty))

    def finish(self) -> None:
        """Give the body its terminator, unless it ended in one already."""
        # Every slot exists before the first instruction runs, whatever block
        # its source form sits in: an ``alloca`` in a loop body would execute
        # once per iteration and grow the frame until the stack gave out.
        self.fn.blocks[0].instrs[0:0] = self.allocas
        if self.block.term is not None:
            return
        if self.is_main:
            self.leak_report()
            self.block.term = ("ret", _ENTRY_RET, "0")
        elif _is_void(self.ret):
            self.block.term = ("ret", Prim("void"), None)
        else:
            raise diag(
                MISSING_RETURN,
                self.path,
                f"function '{self.fn.name}' ends without a 'ret'",
            )

    # -- statements ------------------------------------------------------

    def body(self, statements: list[Node | Token]) -> None:
        for item in statements:
            if self.block.term is not None:
                # Anything after a terminator is unreachable, and LLVM has no
                # place to put it: the block is closed.
                return
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, self.path, "a statement must be a form")
            self.statement(item)

    def statement(self, node: Node) -> None:
        kind = node.kind
        if kind in ("let", "letmut"):
            self.bind(node)
        elif kind == "set":
            self.assign(node)
        elif kind == "set-field":
            self.set_field(node)
        elif kind == "arr-set":
            self.arr_set(node)
        elif kind == "vec-push":
            self.vec_push(node)
        elif kind == "vec-set":
            self.vec_set(node)
        elif kind == "vec-free":
            self.vec_free(node)
        elif kind == "set-deref":
            self.set_deref(node)
        elif kind == "free":
            self.free_stmt(node)
        elif kind == "ret":
            self.ret_stmt(node)
        elif kind == "while":
            self.while_stmt(node)
        elif kind in ("break", "continue"):
            self.jump(node)
        else:
            self.value(node, node.path)

    def bind(self, node: Node) -> None:
        name, declared, init = let_shape(node, self.cm.types, None)
        # The binding's type is the checker's: inside a copy, an annotation
        # ``:T`` is already the concrete type this instantiation fixed, so the
        # lowerer never substitutes a second time.
        ty = self.record.declared.get(node.path)
        if ty is None:
            ty = declared if declared is not None else self.value_type(init, node.path)
        slot = self.slot(ty, path=node.path)
        self.emit(("store", ty, self.value(init, node.path), slot))
        self.vars[name] = (ty, slot)

    def assign(self, node: Node) -> None:
        """``(set name value)``: store into the binding's own slot.

        The checker has proved the name exists and is mutable; this pass still
        looks it up rather than trusting that, so a mismatch is a diagnostic at
        the ``set`` and not a ``KeyError``.
        """
        items = node.items
        if len(items) != 3 or not isinstance(items[1], Token):
            raise diag(WRONG_ARITY, node.path, "set takes a name and a value")
        binding = self.vars.get(items[1].value)
        if binding is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{items[1].value}'")
        ty, slot = binding
        self.emit(("store", ty, self.value(items[2], node.path), slot))

    def while_stmt(self, node: Node) -> None:
        """``head`` tests, ``body`` repeats, ``exit`` continues after the loop.

        A condition that is the literal ``true`` gets no head-to-exit edge: the
        head branches straight to the body. The exit block then has no
        predecessor unless a ``break`` names it, so it is closed with
        ``unreachable`` — which is what lets ``(while true (ret 1))`` be a
        function that cannot fall off its end.
        """
        items = node.items
        if len(items) < 2:
            raise diag(
                WRONG_ARITY, node.path, "while takes a condition and an optional body"
            )
        always = _is_true(items[1])
        self.labels += 1
        tag = f"while{self.labels}"
        head, body, exit_label = f"{tag}.head", f"{tag}.body", f"{tag}.exit"
        self.block.term = ("br", head)
        self.new_block(head)
        condition = self.value(items[1], node.path)
        if always:
            self.block.term = ("br", body)
        else:
            self.block.term = ("cbr", condition, body, exit_label)
        self.new_block(body)
        self.loops.append((exit_label, head))
        try:
            with self.scope():
                self.body(list(items[2:]))
        finally:
            self.loops.pop()
        if self.block.term is None:
            self.block.term = ("br", head)
        self.new_block(exit_label)
        if always and exit_label not in self.escapes:
            self.block.term = ("unreachable",)

    def jump(self, node: Node) -> None:
        """``break`` leaves the innermost loop; ``continue`` re-tests it."""
        if not self.loops:
            raise diag(UNKNOWN_NODE, node.path, f"'{node.kind}' outside a loop")
        exit_label, head = self.loops[-1]
        if node.kind == "break":
            # The exit block is reachable, so a constant-true loop must not
            # close it as unreachable.
            self.escapes.add(exit_label)
        self.block.term = ("br", exit_label if node.kind == "break" else head)

    def ret_stmt(self, node: Node) -> None:
        args = node.items[1:]
        if args:
            value = self.value(args[0], node.path)
        else:
            value = None
        if self.is_main:
            # The tracker reports before the process leaves ``main``, on every
            # path out of it: a test's ``(leaks 0)`` is about the whole run.
            status = self.exit_status(value)
            self.leak_report()
            self.block.term = ("ret", _ENTRY_RET, status)
        elif value is None:
            self.block.term = ("ret", Prim("void"), None)
        else:
            self.block.term = ("ret", self.ret, value)

    def leak_report(self) -> None:
        """Ask the runtime to print every block still live, in a debug build."""
        if not self.lowering.debug:
            return
        self.lowering.declare(_RT_LEAK_REPORT)
        self.emit(("call", "", "rt_leak_report", ()))

    def exit_status(self, value: str | None) -> str:
        """The value ``main`` returns as a process status, truncated to ``i32``.

        A ``:void`` main has no value and exits 0. An ``:i64`` main returns its
        value; ``trunc`` keeps the low 32 bits, which is what the process
        status is.
        """
        if value is None:
            return "0"
        dest = self.temp()
        self.emit(("trunc", dest, Prim("i64"), _ENTRY_RET, value))
        return dest

    # -- results, optionals and enums ------------------------------------

    def result_value(self, node: Node) -> str:
        """``(ok v)`` or ``(err e)``: the tag, then one of the two slots.

        The unused slot stays ``undef``: a result is ``{ i1, T, E }`` and only
        one of the two payloads exists. Nothing reads the other, because every
        read of a payload is guarded by the tag the match tested.
        """
        ty = self.type_of(node)
        if not isinstance(ty, Res):
            raise diag(UNKNOWN_NODE, node.path, f"'{node.kind}' is not a result here")
        if len(node.items) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes one value")
        self.lowering.use_type(ty, node.path)
        if node.kind == "ok":
            part, index, tag = ty.ok, 1, "true"
        else:
            part, index, tag = ty.err, 2, "false"
        value = self.value(node.items[1], node.path)
        partial = self.temp()
        self.emit(("insertvalue", partial, ty, "undef", Prim("bool"), tag, 0))
        whole = self.temp()
        self.emit(("insertvalue", whole, ty, partial, part, value, index))
        return whole

    def optional_value(self, node: Node) -> str:
        """``(some v)`` or ``(none)``: ``{ i1, T }`` with the tag set."""
        ty = self.type_of(node)
        if not isinstance(ty, Opt):
            raise diag(
                UNKNOWN_NODE, node.path, f"'{node.kind}' is not an optional here"
            )
        self.lowering.use_type(ty, node.path)
        if node.kind == "none":
            if len(node.items) != 1:
                raise diag(WRONG_ARITY, node.path, "'none' takes no values")
            whole = self.temp()
            self.emit(("insertvalue", whole, ty, "undef", Prim("bool"), "false", 0))
            return whole
        if len(node.items) != 2:
            raise diag(WRONG_ARITY, node.path, "'some' takes one value")
        value = self.value(node.items[1], node.path)
        partial = self.temp()
        self.emit(("insertvalue", partial, ty, "undef", Prim("bool"), "true", 0))
        whole = self.temp()
        self.emit(("insertvalue", whole, ty, partial, ty.elem, value, 1))
        return whole

    def enum_value(self, node: Node, tag: tuple[EnumRef, int, Ty | None]) -> str:
        """``(variant v)``: a tag and the payload the variant carries.

        A payload is written into the enum's payload area through a typed
        pointer: the aggregate is built in a stack slot, the tag stored first,
        then the payload at the area's first byte. Loading the slot yields the
        one value the whole enum has, whatever variant it is.
        """
        enum, index, payload = tag
        ty = self.type_of(node)
        if not isinstance(ty, EnumRef):
            raise diag(UNKNOWN_NODE, node.path, f"'{node.kind}' is not a variant here")
        self.lowering.use_type(ty, node.path)
        if payload is None:
            if len(node.items) != 1:
                raise diag(
                    WRONG_ARITY, node.path, f"variant '{node.kind}' takes no value"
                )
        elif len(node.items) != 2:
            raise diag(WRONG_ARITY, node.path, f"variant '{node.kind}' takes one value")
        if payload is None:
            whole = self.temp()
            self.emit(("insertvalue", whole, ty, "undef", Prim("i32"), str(index), 0))
            return whole
        value = self.value(node.items[1], node.path)
        slot = self.slot(ty, path=node.path)
        tag_address = self.temp()
        self.emit(
            ("gep", tag_address, ty, slot, (("i32", "0"), ("i32", "0")))
        )
        self.emit(("store", Prim("i32"), str(index), tag_address))
        area = self.temp()
        self.emit(("gep", area, ty, slot, (("i32", "0"), ("i32", "1"))))
        words = enum_words(ty.name, self.cm.enums, self.cm.structs)
        address = self.temp()
        pointer = Ptr(Arr(Prim("i64"), words))
        self.emit(("bitcast", address, pointer, Ptr(payload), area))
        self.emit(("store", payload, value, address))
        whole = self.temp()
        self.emit(("load", whole, ty, slot))
        return whole

    def try_expr(self, node: Node) -> str:
        """``(try e)``: test the tag, return the error or yield the ok value.

        On the error path the enclosing function returns at once with an
        ``(err e)`` of its own error type — the checker has already proved the
        two are the same enum, so the payload carries across unchanged. On the
        ok path the payload is extracted and execution continues in the block
        the expression was written in.
        """
        ty = self.type_of(node)
        if len(node.items) != 2:
            raise diag(WRONG_ARITY, node.path, "'try' takes one result")
        arg = node.items[1]
        inner = self.type_of_item(arg, node.path)
        if (
            not isinstance(inner, Res)
            or not isinstance(self.ret, Res)
            or not compatible(inner.err, self.ret.err)
        ):
            # The checker refuses this before lowering; the message keeps a
            # hand-built tree from reaching the printer as a ValueError.
            raise diag(
                TRY_MISMATCH,
                node.path,
                f"cannot 'try' {inner} in a function returning {self.ret}",
            )
        value = self.value(arg, node.path)
        self.labels += 1
        label = f"try{self.labels}"
        ok_block, err_block = f"{label}.ok", f"{label}.err"
        failed = self.temp()
        self.emit(("extractvalue", failed, inner, Prim("bool"), value, 0))
        self.block.term = ("cbr", failed, ok_block, err_block)
        self.new_block(err_block)
        error = self.temp()
        self.emit(("extractvalue", error, inner, inner.err, value, 2))
        partial = self.temp()
        self.emit(("insertvalue", partial, self.ret, "undef", Prim("bool"), "false", 0))
        whole = self.temp()
        self.emit(
            ("insertvalue", whole, self.ret, partial, self.ret.err, error, 2)
        )
        self.block.term = ("ret", self.ret, whole)
        self.new_block(ok_block)
        payload = self.temp()
        self.emit(("extractvalue", payload, inner, ty, value, 1))
        return payload

    def bind_payload(
        self,
        variant: str,
        name: str,
        payload: Ty,
        tested: Ty,
        value: str,
        memory: str | None,
        path: str,
    ) -> None:
        """Bind one arm's payload name to the value the tag says is there.

        A result and an optional carry their payload in a field, so one
        ``extractvalue`` reads it. An enum has a uniform payload area, so the
        value is stored and the area re-read through a pointer of the variant's
        own type — the same bytes ``enum_value`` wrote.
        """
        if isinstance(tested, Res):
            index = 1 if variant == "ok" else 2
            extracted = self.temp()
            self.emit(("extractvalue", extracted, tested, payload, value, index))
        elif isinstance(tested, Opt):
            extracted = self.temp()
            self.emit(("extractvalue", extracted, tested, payload, value, 1))
        else:
            if memory is None or not isinstance(tested, EnumRef):
                raise diag(
                    UNKNOWN_NODE, path, "a variant binding needs the enum in memory"
                )
            words = enum_words(tested.name, self.cm.enums, self.cm.structs)
            area = self.temp()
            self.emit(
                ("gep", area, tested, memory, (("i32", "0"), ("i32", "1")))
            )
            address = self.temp()
            pointer = Ptr(Arr(Prim("i64"), words))
            self.emit(("bitcast", address, pointer, Ptr(payload), area))
            extracted = self.temp()
            self.emit(("load", extracted, payload, address))
        slot = self.slot(payload, path=path)
        self.emit(("store", payload, extracted, slot))
        self.vars[name] = (payload, slot)

    # -- structs and fixed arrays ----------------------------------------

    def make(self, node: Node) -> str:
        """``(make T v...)``: one ``insertvalue`` per field or element.

        The aggregate is built as a value, not written into storage: a struct
        returned by a function, passed as an argument or nested inside another
        ``make`` is the same chain. Fields are inserted at their declaration
        index, which is what keeps the value's layout the C layout.
        """
        ty = self.type_of(node)
        self.lowering.use_type(ty, node.path)
        items = node.items
        if len(items) < 2:
            raise diag(WRONG_ARITY, node.path, "make takes a type and its fields")
        # A composite type may be written behind the ``:`` marker,
        # ``(make :(arr :i64 3) 1 2 3)``, which occupies its own item.
        start = 3 if _is_marker(items[1]) else 2
        if isinstance(ty, Vec):
            if len(items) > start:
                raise diag(
                    WRONG_ARITY,
                    node.path,
                    "a vector starts empty: make takes no elements",
                )
            return self.empty_vector(ty, node.path)
        values = [self.value(item, node.path) for item in items[start:]]
        if isinstance(ty, StructRef):
            fields = self.cm.structs.get(ty.name, ())
            parts = [(value, fty) for value, (_, fty) in zip(values, fields)]
        elif isinstance(ty, Arr):
            parts = [(value, ty.elem) for value in values]
        else:
            raise diag(
                UNKNOWN_NODE, node.path, f"'make' of {ty} is not lowered yet"
            )
        aggregate = "undef"
        for index, (value, field_ty) in enumerate(parts):
            dest = self.temp()
            self.emit(("insertvalue", dest, ty, aggregate, field_ty, value, index))
            aggregate = dest
        return aggregate

    def get_field(self, node: Node) -> str:
        """``(get e field)``: ``extractvalue`` at the field's declaration index."""
        items = node.items
        if len(items) != 3 or not isinstance(items[2], Token):
            raise diag(WRONG_ARITY, node.path, "'get' takes a struct and a field")
        ty = self.type_of_item(items[1], node.path)
        if not isinstance(ty, StructRef):
            raise diag(
                TYPE_MISMATCH, node.path, f"'get' needs a struct, found {ty}"
            )
        index, field_ty = self.field(ty, items[2].value, node.path)
        self.lowering.use_type(field_ty, node.path)
        whole = self.value(items[1], node.path)
        dest = self.temp()
        self.emit(("extractvalue", dest, ty, field_ty, whole, index))
        return dest

    def set_field(self, node: Node) -> None:
        """``(set-field name field value)``: store through a field pointer."""
        items = node.items
        if (
            len(items) != 4
            or not isinstance(items[1], Token)
            or not isinstance(items[2], Token)
        ):
            raise diag(
                WRONG_ARITY,
                node.path,
                "set-field takes a mutable name, a field and a value",
            )
        binding = self.vars.get(items[1].value)
        if binding is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{items[1].value}'")
        ty, slot = binding
        if not isinstance(ty, StructRef):
            raise diag(
                TYPE_MISMATCH, node.path, f"'set-field' needs a struct, found {ty}"
            )
        index, field_ty = self.field(ty, items[2].value, node.path)
        value = self.value(items[3], node.path)
        address = self.temp()
        self.emit(("gep", address, ty, slot, (("i32", "0"), ("i32", str(index)))))
        self.emit(("store", field_ty, value, address))

    def arr_get(self, node: Node) -> str:
        """``(arr-get a i)``: bounds check, ``gep``, ``load``."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "'arr-get' takes an array and an index"
            )
        ty = self.type_of_item(items[1], node.path)
        if not isinstance(ty, Arr):
            raise diag(
                TYPE_MISMATCH, node.path, f"'arr-get' needs a fixed array, found {ty}"
            )
        self.lowering.use_type(ty, node.path)
        place = self.place(items[1], ty, node.path)
        index = self.index64(items[2], node.path)
        checked = self.bounds(str(ty.n), index, node.path)
        address = self.temp()
        self.emit(("gep", address, ty, place, (("i64", "0"), ("i64", checked))))
        dest = self.temp()
        self.emit(("load", dest, ty.elem, address))
        return dest

    def arr_set(self, node: Node) -> None:
        """``(arr-set name i v)``: bounds check, ``gep``, ``store``."""
        items = node.items
        if len(items) != 4 or not isinstance(items[1], Token):
            raise diag(
                WRONG_ARITY,
                node.path,
                "arr-set takes a mutable name, an index and a value",
            )
        binding = self.vars.get(items[1].value)
        if binding is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{items[1].value}'")
        ty, slot = binding
        if not isinstance(ty, Arr):
            raise diag(
                TYPE_MISMATCH, node.path, f"'arr-set' needs a fixed array, found {ty}"
            )
        self.lowering.use_type(ty, node.path)
        index = self.index64(items[2], node.path)
        checked = self.bounds(str(ty.n), index, node.path)
        value = self.value(items[3], node.path)
        address = self.temp()
        self.emit(("gep", address, ty, slot, (("i64", "0"), ("i64", checked))))
        self.emit(("store", ty.elem, value, address))

    # -- strings ----------------------------------------------------------

    def str_parts(self, item: Node | Token, where: str) -> tuple[str, str]:
        """The data pointer and the byte count of one ``:str`` value."""
        ty = self.type_of_item(item, where)
        if not (isinstance(ty, Prim) and ty.name == "str"):
            raise diag(NOT_A_STR, _path_of(item, where), f"expected a :str, found {ty}")
        whole = self.value(item, where)
        pointer = self.temp()
        self.emit(("extractvalue", pointer, _STR, _PTR_U8, whole, 0))
        length = self.temp()
        self.emit(("extractvalue", length, _STR, Prim("i64"), whole, 1))
        return pointer, length

    def str_value(self, pointer: str, length: str) -> str:
        """One ``:str`` value from a data pointer and a byte count."""
        partial = self.temp()
        self.emit(("insertvalue", partial, _STR, "undef", _PTR_U8, pointer, 0))
        whole = self.temp()
        self.emit(("insertvalue", whole, _STR, partial, Prim("i64"), length, 1))
        return whole

    def str_eq_form(self, node: Node) -> str:
        """``(str-eq a b)``: the same bytes comparison ``=`` lowers to."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'str-eq' takes two strings")
        return self.str_compare(items[1], items[2], node.path)

    def str_len(self, node: Node) -> str:
        """``(str-len s)``: the byte count the value already carries."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "'str-len' takes a :str")
        _, length = self.str_parts(items[1], node.path)
        return length

    def str_compare(
        self, left_item: Node | Token, right_item: Node | Token, path: str
    ) -> str:
        """Byte equality: the lengths, then ``memcmp`` of the bytes.

        The length test comes first and the byte comparison only runs when the
        two agree, so ``memcmp`` is never asked to read past the end of the
        shorter string. Equal lengths and no difference is the definition of a
        matching string; a difference in length is answered without touching
        either buffer.
        """
        left_pointer, left_length = self.str_parts(left_item, path)
        right_pointer, right_length = self.str_parts(right_item, path)
        same = self.temp()
        self.emit(("icmp", same, "eq", Prim("i64"), left_length, right_length))
        self.labels += 1
        tag = f"streq{self.labels}"
        compare, differ, join = f"{tag}.bytes", f"{tag}.differ", f"{tag}.join"
        self.block.term = ("cbr", same, compare, differ)
        self.new_block(compare)
        self.lowering.declare(_MEMCMP)
        difference = self.temp()
        self.emit(
            ("call", difference, "memcmp", (left_pointer, right_pointer, left_length))
        )
        equal = self.temp()
        self.emit(("icmp", equal, "eq", Prim("i32"), difference, "0"))
        self.block.term = ("br", join)
        self.new_block(differ)
        self.block.term = ("br", join)
        self.new_block(join)
        dest = self.temp()
        self.emit(("phi", dest, Prim("bool"), ((equal, compare), ("false", differ))))
        return dest

    def str_concat(self, node: Node) -> str:
        """``(str-concat a b)``: one runtime call, one new buffer.

        The sum is guarded against wrapping before the runtime sees it; the
        runtime allocates the result for the process's life (see ``rt.h``) and
        hands the length back through a slot, so the ``:str`` built here
        carries the bytes the runtime actually produced.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'str-concat' takes two strings")
        left_pointer, left_length = self.str_parts(items[1], node.path)
        right_pointer, right_length = self.str_parts(items[2], node.path)
        total = self.temp()
        self.emit(("add", total, Prim("i64"), left_length, right_length))
        # Signed, not unsigned: both lengths are in [0, 2^63), so a sum that
        # wrapped landed in [2^63, 2^64) and reads back as a negative i64.
        # ``icmp ult total, left`` would be constant-false for two such
        # lengths (a wrap lands *above* the left operand as unsigned), so the
        # panic below could never run and clang deleted it at -O2.
        wrapped = self.temp()
        self.emit(("icmp", wrapped, "slt", Prim("i64"), total, "0"))
        self.labels += 1
        tag = f"concat{self.labels}"
        bad, ok = f"{tag}.overflow", f"{tag}.ok"
        self.block.term = ("cbr", wrapped, bad, ok)
        self.new_block(bad)
        self.panic(node.path, ARRAY_BOUNDS, "string length overflows")
        self.new_block(ok)
        slot = self.slot(Prim("i64"), path=node.path)
        self.lowering.declare(_RT_STR_CONCAT)
        data = self.temp()
        self.emit(
            (
                "call",
                data,
                "rt_str_concat",
                (left_pointer, left_length, right_pointer, right_length, slot),
            )
        )
        length = self.temp()
        self.emit(("load", length, Prim("i64"), slot))
        return self.str_value(data, length)

    def str_slice(self, node: Node) -> str:
        """``(str-slice s start end)``: bytes ``[start, end)``, as a view.

        No bytes are copied: a ``:str`` is a fat pointer, so the slice is the
        same buffer at an offset with a shorter length. Both bounds are checked
        against the length and against each other — one unsigned compare each,
        which also catches a negative bound — because a slice of a computed
        string is only known at run time.
        """
        items = node.items
        if len(items) != 4:
            raise diag(
                WRONG_ARITY, node.path, "'str-slice' takes a :str, a start and an end"
            )
        pointer, length = self.str_parts(items[1], node.path)
        start = self.index64(items[2], node.path, "slice index")
        end = self.index64(items[3], node.path, "slice index")
        starts_inside = self.temp()
        self.emit(("icmp", starts_inside, "ule", Prim("i64"), start, length))
        ends_inside = self.temp()
        self.emit(("icmp", ends_inside, "ule", Prim("i64"), end, length))
        ordered = self.temp()
        self.emit(("icmp", ordered, "ule", Prim("i64"), start, end))
        both_inside = self.temp()
        self.emit(("and", both_inside, Prim("bool"), starts_inside, ends_inside))
        all = self.temp()
        self.emit(("and", all, Prim("bool"), both_inside, ordered))
        self.labels += 1
        tag = f"slice{self.labels}"
        bad, ok = f"{tag}.bad", f"{tag}.ok"
        self.block.term = ("cbr", all, ok, bad)
        self.new_block(bad)
        self.panic(node.path, ARRAY_BOUNDS, "string slice out of bounds")
        self.new_block(ok)
        view = self.temp()
        self.emit(("gep", view, Prim("u8"), pointer, (("i64", start),)))
        size = self.temp()
        self.emit(("sub", size, Prim("i64"), end, start))
        return self.str_value(view, size)

    def read_all(self, node: Node) -> str:
        """``(read-all)``: one runtime call, one string value.

        The runtime reads until end of file and hands the byte count back
        through a slot, exactly as ``rt_str_concat`` does: a two-word return
        would cross the C ABI through an ``sret`` argument and the point of
        this boundary is to keep it to scalars and pointers. The buffer is the
        runtime's, untracked, so no ``free`` is owed for it.
        """
        items = node.items
        if len(items) != 1:
            raise diag(WRONG_ARITY, node.path, "'read-all' takes no arguments")
        slot = self.slot(Prim("i64"), path=node.path)
        self.lowering.declare(_RT_READ_ALL)
        data = self.temp()
        self.emit(
            ("call", data, "rt_read_all", (slot, self._c_argument(node.path)))
        )
        length = self.temp()
        self.emit(("load", length, Prim("i64"), slot))
        return self.str_value(data, length)

    # -- vectors ----------------------------------------------------------

    def empty_vector(self, ty: Vec, path: str) -> str:
        """An empty ``(vec T)``: no buffer, no length, no capacity."""
        self.lowering.use_type(ty, path)
        return self.vec_value(ty, "null", "0", "0")

    def vec_value(self, ty: Vec, data: str, length: str, capacity: str) -> str:
        """The three-word descriptor, built as a value."""
        whole = "undef"
        parts = ((Ptr(ty.elem), data), (Prim("i64"), length), (Prim("i64"), capacity))
        for index, (field_ty, value) in enumerate(parts):
            dest = self.temp()
            self.emit(("insertvalue", dest, ty, whole, field_ty, value, index))
            whole = dest
        return whole

    def vec_part(self, ty: Vec, whole: str, index: int) -> str:
        """One field of a descriptor: 0 the buffer, 1 the length, 2 the capacity."""
        field_ty = (Ptr(ty.elem), Prim("i64"), Prim("i64"))[index]
        dest = self.temp()
        self.emit(("extractvalue", dest, ty, field_ty, whole, index))
        return dest

    def vector(self, item: Node | Token, where: str, what: str) -> tuple[Vec, str]:
        """A vector operand: its type and its descriptor value."""
        ty = self.type_of_item(item, where)
        if not isinstance(ty, Vec):
            raise diag(
                TYPE_MISMATCH,
                _path_of(item, where),
                f"'{what}' needs a (vec T), found {ty}",
            )
        return ty, self.value(item, where)

    def vec_len(self, node: Node) -> str:
        """``(vec-len v)``: the length field."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "'vec-len' takes a vector")
        ty, whole = self.vector(items[1], node.path, "vec-len")
        return self.vec_part(ty, whole, 1)

    def vec_get(self, node: Node) -> str:
        """``(vec-get v i)``: bounds check the length, then ``load``."""
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "'vec-get' takes a vector and an index")
        ty, whole = self.vector(items[1], node.path, "vec-get")
        self.lowering.use_type(ty.elem, node.path)
        data = self.vec_part(ty, whole, 0)
        length = self.vec_part(ty, whole, 1)
        index = self.index64(items[2], node.path, "vector index")
        checked = self.bounds(length, index, node.path, "vector index")
        address = self.temp()
        self.emit(("gep", address, ty.elem, data, (("i64", checked),)))
        dest = self.temp()
        self.emit(("load", dest, ty.elem, address))
        return dest

    def vec_set(self, node: Node) -> None:
        """``(vec-set v i x)``: bounds check, ``gep``, ``store``.

        The descriptor does not change, so only the element is written.
        """
        items = node.items
        shape = "vec-set takes a vector name, an index and a value"
        ty, slot = self.vec_binding(node, 4, shape)
        self.lowering.use_type(ty.elem, node.path)
        whole = self.temp()
        self.emit(("load", whole, ty, slot))
        data = self.vec_part(ty, whole, 0)
        length = self.vec_part(ty, whole, 1)
        index = self.index64(items[2], node.path, "vector index")
        checked = self.bounds(length, index, node.path, "vector index")
        value = self.value(items[3], node.path)
        address = self.temp()
        self.emit(("gep", address, ty.elem, data, (("i64", checked),)))
        self.emit(("store", ty.elem, value, address))

    def vec_free(self, node: Node) -> None:
        """``(vec-free v)``: release the buffer and zero the descriptor.

        The zeroed descriptor is what makes a second ``vec-free`` harmless:
        ``rt_free`` treats NULL as free(NULL), and a later ``vec-len`` reads
        zero rather than a count of elements that no longer exist.
        """
        ty, slot = self.vec_binding(node, 2, "vec-free takes a vector name")
        self.lowering.use_type(ty.elem, node.path)
        whole = self.temp()
        self.emit(("load", whole, ty, slot))
        data = self.vec_part(ty, whole, 0)
        pointer = self.temp()
        self.emit(("bitcast", pointer, Ptr(ty.elem), _VOID_PTR, data))
        self.lowering.declare(_RT_FREE)
        self.emit(("call", "", "rt_free", (pointer,)))
        self.emit(("store", ty, "zeroinitializer", slot))

    def vec_binding(self, node: Node, arity: int, shape: str) -> tuple[Vec, str]:
        """The vector place a mutating form writes through: binding or parameter."""
        items = node.items
        if len(items) != arity:
            raise diag(WRONG_ARITY, node.path, shape)
        if not isinstance(items[1], Token):
            raise diag(WRONG_ARITY, node.path, shape)
        binding = self.vars.get(items[1].value)
        if binding is None:
            raise diag(NOT_FOUND, node.path, f"unknown name '{items[1].value}'")
        ty, slot = binding
        if not isinstance(ty, Vec):
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"'{node.kind}' needs a (vec T), found {ty}",
            )
        return ty, slot

    def vec_push(self, node: Node) -> None:
        """``(vec-push v x)``: append, growing the buffer when it is full.

        Growth is allocate, copy, release — ``rt_alloc``, ``memcpy`` and
        ``rt_free`` — rather than a ``realloc``, because the leak tracker owns
        every block's record: a block handed to ``realloc`` would leave the
        tracker describing an address it no longer controls. With that shape
        the old buffer is a released block, not a live one, so a debug build
        reports nothing at exit.

        The capacity goes to twice itself plus four, which makes the first push
        allocate four elements and every later one at least double: the copies
        a program pays for are logarithmic in its pushes. The sum is checked
        against the old capacity so a wrapped value can never look like a
        larger buffer.
        """
        items = node.items
        ty, slot = self.vec_binding(
            node, 3, "vec-push takes a vector name and a value"
        )
        self.lowering.use_type(ty.elem, node.path)
        value = self.value(items[2], node.path)
        origin = self.block.label
        current = self.temp()
        self.emit(("load", current, ty, slot))
        data = self.vec_part(ty, current, 0)
        length = self.vec_part(ty, current, 1)
        capacity = self.vec_part(ty, current, 2)
        full = self.temp()
        self.emit(("icmp", full, "eq", Prim("i64"), length, capacity))

        self.labels += 1
        tag = f"push{self.labels}"
        grow, room, overflow, append = (
            f"{tag}.grow",
            f"{tag}.room",
            f"{tag}.capacity",
            f"{tag}.append",
        )
        self.block.term = ("cbr", full, grow, append)

        self.new_block(grow)
        doubled = self.temp()
        self.emit(("mul", doubled, Prim("i64"), capacity, "2"))
        bigger = self.temp()
        self.emit(("add", bigger, Prim("i64"), doubled, "4"))
        grew = self.temp()
        self.emit(("icmp", grew, "ugt", Prim("i64"), bigger, capacity))
        self.block.term = ("cbr", grew, room, overflow)
        self.new_block(overflow)
        self.panic(node.path, ARRAY_BOUNDS, "vector capacity overflows")

        self.new_block(room)
        size = sizeof_of(ty.elem, self.cm.structs, self.cm.enums)
        self.no_overflow(bigger, size, node.path)
        if size == 1:
            bytes_ = bigger
        else:
            bytes_ = self.temp()
            self.emit(("mul", bytes_, Prim("i64"), bigger, str(size)))
        align = align_of(ty.elem, self.cm.structs, self.cm.enums)
        self.lowering.declare(_RT_ALLOC)
        raw = self.temp()
        path = self._c_argument(node.path)
        self.emit(("call", raw, "rt_alloc", (bytes_, str(align), path)))
        fresh = self.temp()
        self.emit(("bitcast", fresh, _VOID_PTR, Ptr(ty.elem), raw))
        if size == 1:
            copied_bytes = length
        else:
            copied_bytes = self.temp()
            self.emit(("mul", copied_bytes, Prim("i64"), length, str(size)))
        any_data = self.temp()
        self.emit(("icmp", any_data, "ne", Ptr(ty.elem), data, "null"))
        copy, copied = f"{tag}.copy", f"{tag}.copied"
        self.block.term = ("cbr", any_data, copy, copied)
        self.new_block(copy)
        destination = self.temp()
        self.emit(("bitcast", destination, Ptr(ty.elem), _VOID_PTR, fresh))
        source = self.temp()
        self.emit(("bitcast", source, Ptr(ty.elem), _VOID_PTR, data))
        self.lowering.declare(_MEMCPY)
        self.emit(("call", "", "memcpy", (destination, source, copied_bytes)))
        self.block.term = ("br", copied)
        self.new_block(copied)
        stale = self.temp()
        self.emit(("bitcast", stale, Ptr(ty.elem), _VOID_PTR, data))
        self.lowering.declare(_RT_FREE)
        self.emit(("call", "", "rt_free", (stale,)))
        self.block.term = ("br", append)

        self.new_block(append)
        buffer = self.temp()
        self.emit(("phi", buffer, Ptr(ty.elem), ((data, origin), (fresh, copied))))
        room_now = self.temp()
        self.emit(
            ("phi", room_now, Prim("i64"), ((capacity, origin), (bigger, copied)))
        )
        address = self.temp()
        self.emit(("gep", address, ty.elem, buffer, (("i64", length),)))
        self.emit(("store", ty.elem, value, address))
        bumped = self.temp()
        self.emit(("add", bumped, Prim("i64"), length, "1"))
        self.emit(("store", ty, self.vec_value(ty, buffer, bumped, room_now), slot))

    # -- memory ----------------------------------------------------------

    def alloc(self, node: Node) -> str:
        """``(alloc T n)``: one block of n T, as ``rt_alloc`` returns it.

        The count is in elements and ``rt_alloc`` takes bytes, so the block's
        size is one multiply by the element's layout. The multiply is guarded
        against a count that is already negative or too large: one unsigned
        compare makes ``count * size`` itself unable to wrap before the
        allocator sees it. That is the limit of the guarantee. Arithmetic that
        produced the count may itself have wrapped before this node ran, and a
        wrapped count can look small enough to pass; the guard bounds this
        multiply, not every value that reaches it.

        ``rt_alloc`` also takes the alignment and the node path. The path is a
        compiler-generated C string, and it is what the leak report prints at
        exit: a live block names the ``alloc`` that made it.
        """
        ty = self.type_of(node)
        if not isinstance(ty, Own):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'alloc' did not produce an owning pointer, found {ty}",
            )
        self.lowering.use_type(ty.elem, node.path)
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "alloc takes a type and an element count"
            )
        count = self.index64(items[2], node.path, "alloc count")
        size = sizeof_of(ty.elem, self.cm.structs, self.cm.enums)
        self.no_overflow(count, size, node.path)
        if size == 1:
            bytes_ = count
        else:
            bytes_ = self.temp()
            self.emit(("mul", bytes_, Prim("i64"), count, str(size)))
        path = self._c_argument(node.path)
        align = align_of(ty.elem, self.cm.structs, self.cm.enums)
        self.lowering.declare(_RT_ALLOC)
        raw = self.temp()
        self.emit(("call", raw, "rt_alloc", (bytes_, str(align), path)))
        cast = self.temp()
        self.emit(("bitcast", cast, _VOID_PTR, ty, raw))
        return cast

    def no_overflow(self, count: str, size: int, where: str) -> None:
        """Branch to ``rt_panic`` unless ``count * size`` fits a byte count.

        One unsigned compare: a count that is negative — written that way, or
        produced by arithmetic that wrapped — reads as a huge unsigned value
        and fails here, instead of becoming a huge ``size_t``. That is all it
        proves: this multiply cannot wrap. A count that wrapped to a small
        positive value before this node ran passes the guard, because the
        guard has no record of the value it used to be.
        """
        limit = (2**63 - 1) // size if size else 2**63 - 1
        self.labels += 1
        tag = f"alloc{self.labels}"
        ok, bad = f"{tag}.ok", f"{tag}.bad"
        test = self.temp()
        self.emit(("icmp", test, "ugt", Prim("i64"), count, str(limit)))
        self.block.term = ("cbr", test, bad, ok)
        self.new_block(bad)
        self.panic(where, ARRAY_BOUNDS, "allocation size overflows")
        self.new_block(ok)

    def deref(self, node: Node) -> str:
        """``(deref p)``: one ``load`` of the pointee type through p."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "deref takes one pointer")
        ty = self.type_of(node)
        self.lowering.use_type(ty, node.path)
        pointer = self.value(items[1], node.path)
        dest = self.temp()
        self.emit(("load", dest, ty, pointer))
        return dest

    def set_deref(self, node: Node) -> None:
        """``(set-deref p v)``: one ``store`` through p."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "set-deref takes a pointer and a value"
            )
        pointer_ty = self.type_of_item(items[1], node.path)
        elem = _pointee(pointer_ty)
        if elem is None:
            raise diag(
                TYPE_MISMATCH, node.path, f"deref needs a pointer, found {pointer_ty}"
            )
        self.lowering.use_type(elem, node.path)
        pointer = self.value(items[1], node.path)
        value = self.value(items[2], node.path)
        self.emit(("store", elem, value, pointer))

    def ptr_form(self, node: Node) -> str:
        """``(ptr p)``: the same pointer under a type that cannot free it."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "ptr takes one owning pointer")
        return self.value(items[1], node.path)

    def ptr_add(self, node: Node) -> str:
        """``(ptr-add p n)``: a ``gep`` scaled by the pointee's own size."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "ptr-add takes a pointer and an offset"
            )
        pointer_ty = self.type_of_item(items[1], node.path)
        elem = _pointee(pointer_ty)
        if elem is None:
            raise diag(
                TYPE_MISMATCH, node.path, f"ptr-add needs a pointer, found {pointer_ty}"
            )
        pointer = self.value(items[1], node.path)
        offset = self.index64(items[2], node.path, "pointer offset")
        dest = self.temp()
        self.emit(("gep", dest, elem, pointer, (("i64", offset),)))
        return dest

    # -- concurrency ------------------------------------------------------

    def spawn(self, node: Node) -> str:
        """``(spawn f args...)``: a heap record, a thunk, and ``rt_thread_new``.

        The arguments are evaluated here and copied into one record on the
        heap, because the new thread outlives this frame's slots. The record is
        allocated with ``rt_alloc`` and the spawn's own node path, so a program
        that forgets to join is told where the thread was started. The thunk is
        the one function whose signature the runtime knows, ``i8* (i8*)``: it
        casts the record back to its field list, calls the target, and returns
        null. ``rt_thread_new`` owns the record from the moment the thread
        starts and releases it when the target returns.
        """
        ty = self.type_of(node)
        if not (isinstance(ty, Prim) and ty.name == "thread"):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'spawn' did not produce a :thread, found {ty}",
            )
        items = node.items
        if len(items) < 2 or not isinstance(items[1], Token):
            raise diag(
                UNKNOWN_NODE, node.path, "spawn needs the name of a module function"
            )
        name = items[1].value
        symbol = self.lowering.symbols.get(name)
        sig = self.cm.fn_sigs.get(name)
        if symbol is None or sig is None:
            raise diag(
                UNKNOWN_FUNCTION,
                node.path,
                f"cannot start '{name}': no lowered definition in module "
                f"'{self.cm.module.name}'",
            )
        args = items[2:]
        fields = sig.params
        if len(args) != len(fields):
            raise diag(
                WRONG_ARITY,
                node.path,
                f"'{name}' takes {len(fields)} arguments, got {len(args)}",
            )
        # Evaluate the arguments in source order, before the record exists:
        # an argument may itself allocate or create a handle.
        values = tuple(self.value(arg, node.path) for arg in args)
        for field in fields:
            self.lowering.use_type(field, node.path)
        size = _fields_size(fields, self.cm.structs, self.cm.enums) if fields else 0
        align = (
            max(align_of(field, self.cm.structs, self.cm.enums) for field in fields)
            if fields
            else 1
        )
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_ALLOC)
        raw = self.temp()
        self.emit(("call", raw, "rt_alloc", (str(size), str(align), path)))
        if fields:
            base = self.temp()
            self.emit(("bitcastrec", base, fields, _VOID_PTR, raw))
            for index, (field, value) in enumerate(zip(fields, values)):
                address = self.temp()
                self.emit(("geprec", address, fields, base, index))
                self.emit(("store", field, value, address))
        code = self.spawn_thunk(fields, symbol, node.path)
        self.lowering.declare(_RT_THREAD_NEW)
        handle = self.temp()
        self.emit(("call", handle, "rt_thread_new", (path, code, raw)))
        return handle

    def spawn_thunk(
        self, fields: tuple[Ty, ...], target: str, where: str
    ) -> str:
        """Emit the ``i8* (i8*)`` entry point for one spawn site.

        One per site rather than one per target: the record's field list is
        the target's parameter list, so two sites with different argument types
        cannot share a body, and a site's thunk is a small function the caller
        takes the address of. Returns the thunk's address as an ``i8*``.
        """
        name = self.lowering.next_name("$spawn")
        symbol = self.lowering.symbol(name)
        fn = IrFn(
            name,
            [],
            _VOID_PTR,
            [IrBlock("entry")],
            symbol=symbol,
            internal=True,
        )
        builder = _Fn(
            self.lowering,
            fn,
            path=self.path,
            ret=_VOID_PTR,
            is_main=False,
            body=self.record,
        )
        # The runtime calls this through ``void *(*)(void *)``: one opaque
        # argument, one opaque result. Appending the parameter directly keeps
        # it out of the binding table, which has no slot for it.
        fn.params.append(("env", _VOID_PTR))
        if fields:
            base = builder.temp()
            builder.emit(("bitcastrec", base, fields, _VOID_PTR, "%env"))
            loaded: list[str] = []
            for index, field in enumerate(fields):
                builder.lowering.use_type(field, where)
                address = builder.temp()
                builder.emit(("geprec", address, fields, base, index))
                value = builder.temp()
                builder.emit(("load", value, field, address))
                loaded.append(value)
            arguments = tuple(loaded)
        else:
            arguments = ()
        builder.emit(("call", "", target, arguments))
        builder.block.term = ("ret", _VOID_PTR, "null")
        builder.finish()
        self.lowering.ir.functions.append(fn)
        code = self.temp()
        self.emit(("faddr", code, symbol))
        return code

    def join_form(self, node: Node) -> str:
        """``(join t)``: wait, then release the handle."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "join takes one thread")
        handle = self.value(items[1], node.path)
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_JOIN)
        self.emit(("call", "", "rt_join", (handle, path)))
        return ""

    def mutex_form(self, node: Node) -> str:
        """``(mutex)``: a runtime lock, allocated for the process's life."""
        if len(node.items) != 1:
            raise diag(WRONG_ARITY, node.path, "mutex takes no arguments")
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_MUTEX_NEW)
        handle = self.temp()
        self.emit(("call", handle, "rt_mutex_new", (path,)))
        return handle

    def mutex_op(self, node: Node, callee: str) -> str:
        """``(lock m)``/``(unlock m)``: one runtime call, the path for R0005."""
        items = node.items
        if len(items) != 2:
            raise diag(
                WRONG_ARITY, node.path, f"{node.kind} takes one mutex"
            )
        handle = self.value(items[1], node.path)
        path = self._c_argument(node.path)
        self.lowering.declare(
            _RT_MUTEX_LOCK if callee == "rt_mutex_lock" else _RT_MUTEX_UNLOCK
        )
        self.emit(("call", "", callee, (handle, path)))
        return ""

    def channel_of(self, item: Node | Token, where: str) -> Chan:
        """The ``(chan T)`` a value names, through its ``(shared ...)`` marker."""
        ty = self.type_of_item(item, where)
        if isinstance(ty, Shared):
            ty = ty.elem
        if not isinstance(ty, Chan):
            raise diag(TYPE_MISMATCH, where, f"expected a channel, found {ty}")
        return ty

    def chan_form(self, node: Node) -> str:
        """``(chan T n)``: one runtime ring of ``n`` elements of T."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "chan takes an element type and a capacity"
            )
        ty = self.type_of(node)
        elem = (
            ty.elem.elem
            if isinstance(ty, Shared) and isinstance(ty.elem, Chan)
            else None
        )
        if elem is None:
            raise diag(
                UNKNOWN_NODE, node.path, f"'chan' did not produce a channel, found {ty}"
            )
        self.lowering.use_type(elem, node.path)
        size = sizeof_of(elem, self.cm.structs, self.cm.enums)
        capacity = self.index64(items[2], node.path, "channel capacity")
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_CHAN_NEW)
        handle = self.temp()
        self.emit(("call", handle, "rt_chan_new", (str(size), capacity, path)))
        return handle

    def send_form(self, node: Node) -> str:
        """``(send c v)``: spill the element and hand its address to the ring.

        The channel is evaluated before the element, left to right: a send
        whose channel is itself a call blocks on that call first, exactly as
        every other form evaluates its operands in the order they are written.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, "send takes a channel and a value")
        chan = self.channel_of(items[1], node.path)
        elem = chan.elem
        self.lowering.use_type(elem, node.path)
        handle = self.value(items[1], node.path)
        slot = self.slot(elem, path=node.path)
        self.emit(("store", elem, self.value(items[2], node.path), slot))
        address = self.temp()
        self.emit(("bitcast", address, Ptr(elem), _VOID_PTR, slot))
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_CHAN_SEND)
        self.emit(("call", "", "rt_chan_send", (handle, address, path)))
        return ""

    def recv_form(self, node: Node) -> str:
        """``(recv c)``: give the runtime a slot, then load what it filled."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "recv takes one channel")
        chan = self.channel_of(items[1], node.path)
        elem = chan.elem
        self.lowering.use_type(elem, node.path)
        slot = self.slot(elem, path=node.path)
        handle = self.value(items[1], node.path)
        address = self.temp()
        self.emit(("bitcast", address, Ptr(elem), _VOID_PTR, slot))
        path = self._c_argument(node.path)
        self.lowering.declare(_RT_CHAN_RECV)
        self.emit(("call", "", "rt_chan_recv", (handle, address, path)))
        value = self.temp()
        self.emit(("load", value, elem, slot))
        return value

    def atomic_of(self, item: Node | Token, where: str) -> Atomic:
        """The ``(atomic T)`` a value names, through its ``(shared ...)`` marker."""
        ty = self.type_of_item(item, where)
        if isinstance(ty, Shared):
            ty = ty.elem
        if not isinstance(ty, Atomic):
            raise diag(TYPE_MISMATCH, where, f"expected an atomic, found {ty}")
        return ty

    def atomic_form(self, node: Node) -> str:
        """``(atomic T v)``: one heap word, initialised."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic takes a type and an initial value"
            )
        ty = self.type_of(node)
        elem = (
            ty.elem.elem
            if isinstance(ty, Shared) and isinstance(ty.elem, Atomic)
            else None
        )
        if elem is None:
            raise diag(
                UNKNOWN_NODE, node.path, f"'atomic' did not produce an atomic, found {ty}"
            )
        value = self.value(items[2], node.path)
        self.lowering.declare(_RT_ATOMIC_NEW)
        handle = self.temp()
        self.emit(("call", handle, "rt_atomic_new", (value,)))
        return handle

    def atomic_add(self, node: Node) -> str:
        """``(atomic-add a n)``: one fetch-add, the new value back."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic-add takes an atomic and a value"
            )
        self.atomic_of(items[1], node.path)
        handle = self.value(items[1], node.path)
        delta = self.value(items[2], node.path)
        self.lowering.declare(_RT_ATOMIC_ADD)
        value = self.temp()
        self.emit(("call", value, "rt_atomic_add", (handle, delta)))
        return value

    def atomic_load(self, node: Node) -> str:
        """``(atomic-load a)``: one acquire load."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "atomic-load takes one atomic")
        self.atomic_of(items[1], node.path)
        handle = self.value(items[1], node.path)
        self.lowering.declare(_RT_ATOMIC_LOAD)
        value = self.temp()
        self.emit(("call", value, "rt_atomic_load", (handle,)))
        return value

    def atomic_store(self, node: Node) -> str:
        """``(atomic-store a v)``: one release store."""
        items = node.items
        if len(items) != 3:
            raise diag(
                WRONG_ARITY, node.path, "atomic-store takes an atomic and a value"
            )
        self.atomic_of(items[1], node.path)
        handle = self.value(items[1], node.path)
        value = self.value(items[2], node.path)
        self.lowering.declare(_RT_ATOMIC_STORE)
        self.emit(("call", "", "rt_atomic_store", (handle, value)))
        return ""

    def free_stmt(self, node: Node) -> None:
        """``(free p)``: hand the block back, through the C boundary's ``void *``."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "free takes one pointer")
        pointer_ty = self.type_of_item(items[1], node.path)
        if _pointee(pointer_ty) is None:
            raise diag(
                TYPE_MISMATCH, node.path, f"free needs an (own T), found {pointer_ty}"
            )
        pointer = self.value(items[1], node.path)
        cast = self.temp()
        self.emit(("bitcast", cast, pointer_ty, _VOID_PTR, pointer))
        self.lowering.declare(_RT_FREE)
        self.emit(("call", "", "rt_free", (cast,)))

    def field(self, ty: StructRef, name: str, where: str) -> tuple[int, Ty]:
        """The declaration index and type of one field."""
        fields = self.cm.structs.get(ty.name)
        if fields is None:
            raise diag(UNKNOWN_NODE, where, f"struct '{ty.name}' is not lowered yet")
        for index, (field, field_ty) in enumerate(fields):
            if field == name:
                return index, field_ty
        raise diag(UNKNOWN_FIELD, where, f"struct '{ty.name}' has no field '{name}'")

    def place(self, item: Node | Token, ty: Ty, where: str) -> str:
        """A pointer to the aggregate ``item`` names, for indexing through it.

        A binding already has a slot, so its own is the place. Any other
        expression is a value without storage, which is spilled into one fresh
        slot: LLVM cannot index a value with a run-time index, and allocating
        the slot in ``entry`` keeps a loop from growing the frame.
        """
        if isinstance(item, Token) and item.kind == ATOM:
            binding = self.vars.get(item.value)
            if binding is not None:
                return binding[1]
        slot = self.slot(ty, path=where)
        self.emit(("store", ty, self.value(item, where), slot))
        return slot

    def index64(self, item: Node | Token, where: str, what: str = "array index") -> str:
        """An index widened to ``i64`` for one unsigned bounds test.

        A narrow or signed index is extended, never truncated: a negative index
        then reads as a huge unsigned number and fails the same test a too-large
        one does, instead of wrapping into range. ``what`` names the operand in
        the diagnostic, because an element count and a pointer offset are
        widened for the same reason an array index is.
        """
        value = self.value(item, where)
        ty = self.type_of_item(item, where)
        if not (isinstance(ty, Prim) and ty.name in INTEGERS):
            raise diag(
                INDEX_TYPE,
                _path_of(item, where),
                f"{what} must be an integer, found {ty}",
            )
        if ty.name in ("i64", "u64"):
            return value
        dest = self.temp()
        op = "zext" if ty.name.startswith("u") else "sext"
        self.emit((op, dest, ty, Prim("i64"), value))
        return dest

    def bounds(
        self,
        length: str,
        index: str,
        where: str,
        what: str = "array index",
    ) -> str:
        """Branch to ``rt_panic`` unless ``0 <= index < length``.

        One unsigned compare covers both ends: ``i64`` is the index's widened
        type, and a negative value is above ``length`` once read unsigned.
        ``length`` is an operand rather than a constant because a vector's
        length is a field of a value the program built; an array's is the
        literal its type spells. The panic names the indexing node, so a
        program that dies here says which operation was out of range.
        """
        self.labels += 1
        tag = f"arr{self.labels}"
        ok, bad = f"{tag}.ok", f"{tag}.bad"
        test = self.temp()
        self.emit(("icmp", test, "ult", Prim("i64"), index, length))
        self.block.term = ("cbr", test, ok, bad)
        self.new_block(bad)
        self.panic(where, ARRAY_BOUNDS, f"{what} out of bounds")
        self.new_block(ok)
        return index

    # -- values ----------------------------------------------------------

    def value(self, item: Node | Token, where: str) -> str:
        """Lower ``item`` to an operand: a local name or a literal constant."""
        if isinstance(item, Token):
            return self.literal(item, where)
        kind = item.kind
        if kind in _ARITH or kind in _DIVIDE:
            return self.arith(item)
        if kind in _COMPARE:
            return self.compare(item)
        if kind in _LOGICAL:
            return self.logical(item)
        if kind == "not":
            return self.negate(item)
        if kind == "if":
            return self.if_expr(item)
        if kind == "match":
            return self.match_expr(item)
        if kind == "make":
            return self.make(item)
        if kind == "get":
            return self.get_field(item)
        if kind == "arr-get":
            return self.arr_get(item)
        if kind == "str-len":
            return self.str_len(item)
        if kind == "str-concat":
            return self.str_concat(item)
        if kind == "str-eq":
            return self.str_eq_form(item)
        if kind == "str-slice":
            return self.str_slice(item)
        if kind == "read-all":
            return self.read_all(item)
        if kind == "vec-len":
            return self.vec_len(item)
        if kind == "vec-get":
            return self.vec_get(item)
        if kind == "alloc":
            return self.alloc(item)
        if kind == "deref":
            return self.deref(item)
        if kind == "ptr":
            return self.ptr_form(item)
        if kind == "ptr-add":
            return self.ptr_add(item)
        if kind == "spawn":
            return self.spawn(item)
        if kind == "join":
            return self.join_form(item)
        if kind == "mutex":
            return self.mutex_form(item)
        if kind == "lock":
            return self.mutex_op(item, "rt_mutex_lock")
        if kind == "unlock":
            return self.mutex_op(item, "rt_mutex_unlock")
        if kind == "chan":
            return self.chan_form(item)
        if kind == "send":
            return self.send_form(item)
        if kind == "recv":
            return self.recv_form(item)
        if kind == "atomic":
            return self.atomic_form(item)
        if kind == "atomic-add":
            return self.atomic_add(item)
        if kind == "atomic-load":
            return self.atomic_load(item)
        if kind == "atomic-store":
            return self.atomic_store(item)
        if kind == "fn":
            return self.closure(item)
        if kind in _STATEMENTS:
            raise diag(UNKNOWN_NODE, item.path, f"'{kind}' is a statement, not a value")
        # The result forms are the compiler's own, unless a definition or a
        # binding has taken the name — the same precedence the checker uses.
        if (
            kind in RESULT_FORMS
            and kind not in self.vars
            and kind not in self.cm.fn_sigs
        ):
            if kind in ("ok", "err"):
                return self.result_value(item)
            if kind in ("some", "none"):
                return self.optional_value(item)
            return self.try_expr(item)
        # A binding of a function type is called through its value, whether the
        # checker found it there or in the function namespace; the checker
        # resolves in that order, so this pass reads it the same way.
        binding = self.vars.get(kind)
        if binding is not None and isinstance(binding[0], FnTy):
            return self.indirect_call(item, binding[0])
        # A module-level ``fn`` shadows a builtin: the checker resolves
        # ``env.fns`` before ``BUILTINS``, so the lowerer must agree or a user
        # ``print`` would check as their own function and lower as the builtin.
        if kind in self.cm.fn_sigs:
            return self.call(item)
        if kind in BUILTINS:
            return self.builtin(item)
        tag = self.lowering.variant_tag(kind, item.path)
        if tag is not None and kind not in self.vars:
            return self.enum_value(item, tag)
        if kind == "":
            raise diag(UNKNOWN_NODE, item.path, "a form must start with a name")
        raise diag(
            UNKNOWN_NODE, item.path, f"'{kind}' is not lowered by this backend yet"
        )

    def literal(self, token: Token, where: str) -> str:
        if token.kind == NUMBER:
            if "." in token.value or "e" in token.value or "E" in token.value:
                # The checker recorded the width this literal was checked at;
                # an ``:f32`` needs single-precision spelling (see below).
                return _float_text(
                    token.value, where, self.record.literal_types.get(token)
                )
            # The checker has proved the value fits the resolved width; the
            # spelling is normalised so ``+5`` and ``007`` are LLVM's ``5`` and
            # ``7``.
            return str(int(token.value))
        if token.kind == STRING:
            return self.string_value(token.value.encode("utf-8"))
        if token.kind == ATOM:
            if token.value in ("true", "false"):
                return token.value
            if token.value not in self.vars and token.value in self.cm.fn_sigs:
                # A function's name is a value: its code address with no
                # environment, which is what a closure with no captures has.
                return self.fn_value(token.value, where)
            tag = self.lowering.variant_tag(token.value, where)
            if tag is not None and tag[2] is None and token.value not in self.vars:
                # A payload-less variant is a value: its tag and nothing else.
                # A binding of the same name shadows it, as it does in the
                # checker, where scope is searched before the variant set.
                self.lowering.use_type(tag[0], where)
                whole = self.temp()
                self.emit(
                    ("insertvalue", whole, tag[0], "undef", Prim("i32"), str(tag[1]), 0)
                )
                return whole
            return self.load(token.value, where)
        raise diag(UNKNOWN_NODE, where, f"a {token.kind.lower()} is not a value")

    def string_value(self, data: bytes) -> str:
        """One ``:str`` literal as the ``{ i8*, i64 }`` value it stands for.

        Two ``insertvalue`` steps from ``undef``: the pointer to the private
        global's first byte, then the byte count. The global itself is shared
        by every use of the same bytes, so ``"hi"`` twice is one global and two
        values.
        """
        pointer = self.temp()
        self.emit(("gaddr", pointer, self.lowering.intern(data)))
        partial = self.temp()
        self.emit(("insertvalue", partial, _STR, "undef", _PTR_U8, pointer, 0))
        whole = self.temp()
        self.emit(("insertvalue", whole, _STR, partial, Prim("i64"), str(len(data)), 1))
        return whole

    def load(self, name: str, where: str) -> str:
        binding = self.vars.get(name)
        if binding is None:
            raise diag(NOT_FOUND, where, f"unknown name '{name}'")
        ty, slot = binding
        dest = self.temp()
        self.emit(("load", dest, ty, slot))
        return dest

    def type_of(self, node: Node) -> Ty:
        """The type the checker recorded for ``node`` in this body."""
        ty = self.record.expr_types.get(node.path)
        if ty is None:
            raise diag(UNKNOWN_NODE, node.path, "the checker recorded no type here")
        return ty

    def type_of_item(self, item: Node | Token, where: str) -> Ty:
        """The type of one operand, literal or name, without lowering it.

        A node's type is the checker's record. A literal's type is not recorded,
        because the checker types it where it stands and an annotation may take
        it instead; the default here is the same default the checker uses when
        nothing narrower expects it. A name's type is its binding's, which is
        what makes ``(let b a)`` keep ``a``'s width.
        """
        if isinstance(item, Node):
            return self.type_of(item)
        if item.kind == STRING:
            return _STR
        if item.kind == NUMBER:
            # The same default the checker uses when nothing expects a width:
            # a float literal is ``:f64`` and an integer literal is ``:i64``.
            if "." in item.value or "e" in item.value or "E" in item.value:
                return Prim("f64")
            return Prim("i64")
        if item.kind == ATOM:
            binding = self.vars.get(item.value)
            if binding is not None:
                return binding[0]
            if item.value in ("true", "false"):
                return Prim("bool")
            raise diag(NOT_FOUND, where, f"unknown name '{item.value}'")
        return Prim("i64")

    def value_type(self, item: Node | Token, where: str) -> Ty:
        """The type of an unannotated initialiser (see :meth:`type_of_item`)."""
        return self.type_of_item(item, where)

    def operand_type(self, items: tuple[Node | Token, ...]) -> Ty:
        """The type a comparison runs at: the first operand that has one.

        A numeric literal has no width of its own — the checker gives it the
        type of the operand beside it — so ``(< 200 a)`` with ``a :u8`` is a
        ``:u8`` comparison. Only when every operand is a literal is the
        checker's default, ``:i64``, the answer. A string literal is ``:str``,
        which is how ``(= "a" "b")`` reaches the byte comparison.
        """
        for item in items:
            if isinstance(item, Node):
                return self.type_of(item)
            if isinstance(item, Token) and item.kind == STRING:
                return _STR
            if isinstance(item, Token) and item.kind == ATOM:
                binding = self.vars.get(item.value)
                if binding is not None:
                    return binding[0]
                if item.value in ("true", "false"):
                    return Prim("bool")
        return Prim("i64")

    def arith(self, node: Node) -> str:
        """``(op a b ...)``: one instruction per operand pair.

        An integer form is LLVM's wrapping arithmetic at the width the checker
        resolved; ``/`` and ``%`` go through :meth:`divide` because both can
        trap. A float form is the IEEE instruction at ``:f32`` or ``:f64``: no
        check, no branch, and no fast-math flag, so dividing by zero gives
        ``inf`` or ``nan`` rather than a panic, exactly as IEEE 754 says.
        """
        ty = self.type_of(node)
        is_float = isinstance(ty, Prim) and ty.name in _FLOATS
        if is_float and node.kind == "%":
            # Unreachable through the checker, which refuses it: kept so the IR
            # never carries a ``%`` the backend has no instruction for.
            raise diag(
                TYPE_MISMATCH,
                node.path,
                f"'%' needs integers, found {ty}",
            )
        args = node.items[1:]
        acc = self.value(args[0], node.path)
        for arg in args[1:]:
            right = self.value(arg, node.path)
            if node.kind in _DIVIDE and not is_float:
                acc = self.divide(node.kind, ty, acc, right, node.path)
            else:
                dest = self.temp()
                op = _FLOAT_ARITH[node.kind] if is_float else _ARITH[node.kind]
                self.emit((op, dest, ty, acc, right))
                acc = dest
        return acc

    def compare(self, node: Node) -> str:
        """``(op a b)``: one ``icmp`` whose predicate follows the operand width.

        The operand type is the checker's, so ``(< 1 a)`` with ``a :u8`` is an
        ``i8`` comparison of ``1`` and ``a``; an unsigned width swaps the
        signed predicate for the unsigned one. ``:bool`` is comparable with
        ``=`` and ``!=`` only, which the checker has already enforced.

        ``:str`` equality is not an ``icmp``: the operands are aggregates, so
        it is the byte comparison :meth:`str_compare` emits. ``!=`` is that
        answer, inverted.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        ty = self.operand_type(items[1:])
        if isinstance(ty, Prim) and ty.name == "str":
            if node.kind == "!=":
                equal = self.str_compare(items[1], items[2], node.path)
                dest = self.temp()
                self.emit(("icmp", dest, "eq", Prim("bool"), equal, "false"))
                return dest
            if node.kind == "=":
                return self.str_compare(items[1], items[2], node.path)
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'{node.kind}' on :str is not lowered by this backend yet",
            )
        predicate = _COMPARE[node.kind]
        if isinstance(ty, Prim) and ty.name in _FLOATS:
            # IEEE comparison: ordered predicates are false on a NaN, and
            # ``!=`` is ``une`` so a NaN compares unequal to everything. No
            # fast-math flag is set, so the compiler cannot optimise the NaN
            # case away.
            left = self.value(items[1], node.path)
            right = self.value(items[2], node.path)
            dest = self.temp()
            self.emit(
                ("fcmp", dest, _FLOAT_COMPARE[node.kind], ty, left, right)
            )
            return dest
        if not (isinstance(ty, Prim) and ty.name in INTEGERS + ("bool",)):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'{node.kind}' on {ty} is not lowered by this backend yet",
            )
        if ty.name.startswith("u") and predicate.startswith("s"):
            predicate = "u" + predicate[1:]
        left = self.value(items[1], node.path)
        right = self.value(items[2], node.path)
        dest = self.temp()
        self.emit(("icmp", dest, predicate, ty, left, right))
        return dest

    def logical(self, node: Node) -> str:
        """``and``/``or``: the right operand runs only when the left decides not.

        The join takes a ``phi`` from the block that produced the left value and
        the block that produced the right one, because neither operand is a
        branch: ``(and false (panic))`` must not panic.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        left = self.value(items[1], node.path)
        origin = self.block.label
        self.labels += 1
        tag = f"{node.kind}{self.labels}"
        rhs, join = f"{tag}.rhs", f"{tag}.join"
        if node.kind == "and":
            self.block.term = ("cbr", left, rhs, join)
        else:
            self.block.term = ("cbr", left, join, rhs)
        self.new_block(rhs)
        right = self.value(items[2], node.path)
        tail = self.block.label
        fell = self.block.term is None
        if fell:
            self.block.term = ("br", join)
        self.new_block(join)
        # On the short-circuit edge the left value is already the answer: false
        # for ``and``, true for ``or``. On the other edge it is the right value.
        pairs = [(right, tail), (left, origin)]
        if node.kind == "or":
            pairs.reverse()
        if not fell:
            pairs = [pair for pair in pairs if pair[1] != tail]
        dest = self.temp()
        self.emit(("phi", dest, Prim("bool"), tuple(pairs)))
        return dest

    def negate(self, node: Node) -> str:
        """``(not a)``: one ``icmp`` against the only other ``i1`` value."""
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, "'not' takes one operand")
        operand = self.value(items[1], node.path)
        dest = self.temp()
        self.emit(("icmp", dest, "eq", Prim("bool"), operand, "false"))
        return dest

    def if_expr(self, node: Node) -> str:
        """``(if c then else)``: two branches, one slot, one join.

        Both arms write the same slot and the join loads it. A ``phi`` would be
        smaller, but a branch may end in a terminator — ``(if c (ret 1) (ret
        2))`` is a void ``if`` — and a ``phi`` naming an edge that never
        branches is invalid IR.
        """
        items = node.items
        if len(items) != 4:
            raise diag(
                WRONG_ARITY, node.path, "if takes a condition, a then and an else"
            )
        ty = self.type_of(node)
        condition = self.value(items[1], node.path)
        self.labels += 1
        tag = f"if{self.labels}"
        then, otherwise, join = f"{tag}.then", f"{tag}.else", f"{tag}.join"
        slot = None if _is_void(ty) else self.slot(ty, path=node.path)
        self.block.term = ("cbr", condition, then, otherwise)
        fell = False
        for label, item in ((then, items[2]), (otherwise, items[3])):
            self.new_block(label)
            with self.scope():
                if slot is None:
                    # A void ``if`` has statement branches, so ``(if c (break)
                    # (println "x"))`` works: each branch is lowered exactly as
                    # it would be on its own.
                    if not isinstance(item, Node):
                        raise diag(
                            UNKNOWN_NODE, node.path, "a statement must be a form"
                        )
                    self.statement(item)
                else:
                    branch = self.value(item, node.path)
                    self.emit(("store", ty, branch, slot))
            if self.block.term is None:
                self.block.term = ("br", join)
                fell = True
        self.new_block(join)
        if not fell:
            # Every branch ended in a terminator, so nothing reaches the join:
            # closing it keeps the function from reading as one that falls off
            # its end, and anything after the ``if`` is unreachable.
            self.block.term = ("unreachable",)
        if slot is None:
            return ""
        dest = self.temp()
        self.emit(("load", dest, ty, slot))
        return dest

    def match_expr(self, node: Node) -> str:
        """``(match e (pat body...)...)``: a chain of tests over one scrutinee.

        The scrutinee is evaluated once. Each non-wildcard arm becomes a test
        block comparing its tag with the arm's and branching to that arm's body
        or to the next test; a ``_`` arm branches straight to its body. An
        integer arm tests a literal, an enum arm tests its variant's index, and
        a result or an optional tests the one-bit tag ``(ok ...)`` and
        ``(some ...)`` share.

        An integer match may leave an arm out — no finite list of literals
        covers the domain — and its fall-through is the runtime's R0003. An
        enum, result or optional match is proved exhaustive by the checker, so
        it has no fall-through at all: the last test's else edge is the join.
        """
        scrutinee, arms = match_shape(node)
        ty = self.type_of(node)
        value = self.value(scrutinee, node.path)
        tested = self.type_of_item(scrutinee, node.path)
        variants: tuple[tuple[str, Ty | None], ...] | None = None
        index_of: dict[str, str] = {}
        if isinstance(tested, Prim) and tested.name in INTEGERS:
            tag_ty, tag_value = tested, value
        elif isinstance(tested, EnumRef):
            variants = self.cm.enums.get(tested.name)
            if variants is None:
                raise diag(UNKNOWN_TYPE, node.path, f"unknown type '{tested.name}'")
            tag_ty = Prim("i32")
            index_of = {name: str(index) for index, (name, _) in enumerate(variants)}
            tag = self.temp()
            self.emit(("extractvalue", tag, tested, tag_ty, value, 0))
            tag_value = tag
        elif isinstance(tested, Res):
            variants = (("ok", tested.ok), ("err", tested.err))
            tag_ty = Prim("bool")
            index_of = {"ok": "true", "err": "false"}
            tag = self.temp()
            self.emit(("extractvalue", tag, tested, tag_ty, value, 0))
            tag_value = tag
        elif isinstance(tested, Opt):
            variants = (("some", tested.elem), ("none", None))
            tag_ty = Prim("bool")
            index_of = {"some": "true", "none": "false"}
            tag = self.temp()
            self.emit(("extractvalue", tag, tested, tag_ty, value, 0))
            tag_value = tag
        else:
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'match' on {tested} is not lowered by this backend yet",
            )
        # An enum payload lives in a memory area rather than a field, so the
        # value is stored once and every arm reads the word it expects.
        memory = None
        if isinstance(tested, EnumRef):
            memory = self.slot(tested, path=node.path)
            self.emit(("store", tested, value, memory))
        plan: list[tuple[str, str | None, Ty | None, str] | None] = []
        for pattern, _ in arms:
            if is_wildcard(pattern):
                plan.append(None)
                continue
            if variants is None:
                if not isinstance(pattern, Token) or pattern.kind != NUMBER:
                    raise diag(
                        UNKNOWN_NODE,
                        node.path,
                        "a match pattern is an integer literal or '_'",
                    )
                plan.append(("", None, None, str(int(pattern.value))))
                continue
            name, binding, payload = pattern_variant(
                pattern, variants, where=node.path, owner=str(tested)
            )
            plan.append((name or "", binding, payload, index_of[name or ""]))
        self.labels += 1
        tag_name = f"match{self.labels}"
        join, none = f"{tag_name}.join", f"{tag_name}.none"
        slot = None if _is_void(ty) else self.slot(ty, path=node.path)
        wildcard = any(is_wildcard(pattern) for pattern, _ in arms)
        fell = False
        self.block.term = ("br", f"{tag_name}.test0")
        for index, arm_plan in enumerate(plan):
            self.new_block(f"{tag_name}.test{index}")
            if arm_plan is None:
                self.block.term = ("br", f"{tag_name}.body{index}")
                continue
            if index + 1 < len(arms):
                following = f"{tag_name}.test{index + 1}"
            elif variants is not None:
                # The checker proved every variant covered, so the last test
                # cannot fail: its else edge joins the result directly instead
                # of reaching for a panic block that could never run.
                following = join
            else:
                following = none
            test = self.temp()
            self.emit(("icmp", test, "eq", tag_ty, tag_value, arm_plan[3]))
            self.block.term = ("cbr", test, f"{tag_name}.body{index}", following)
        for index, (_, body) in enumerate(arms):
            self.new_block(f"{tag_name}.body{index}")
            with self.scope():
                arm_plan = plan[index]
                if (
                    arm_plan is not None
                    and arm_plan[1] is not None
                    and arm_plan[2] is not None
                ):
                    self.bind_payload(
                        arm_plan[0],
                        arm_plan[1],
                        arm_plan[2],
                        tested,
                        value,
                        memory,
                        node.path,
                    )
                arm = self.arm_value(body, node.path, statement=slot is None)
            if slot is not None and arm:
                self.emit(("store", ty, arm, slot))
            if self.block.term is None:
                self.block.term = ("br", join)
                fell = True
        if not wildcard and variants is None:
            # An integer match may fall through: no finite arm list covers its
            # domain, so the unmatched path panics. A closed set never does.
            self.new_block(none)
            self.panic(node.path, MATCH_NO_ARM, "no match arm matched")
        self.new_block(join)
        if not fell:
            # Every arm returned, so nothing reaches the join (see ``if``).
            self.block.term = ("unreachable",)
        if slot is None:
            return ""
        dest = self.temp()
        self.emit(("load", dest, ty, slot))
        return dest

    def arm_value(
        self, body: tuple[Node | Token, ...], where: str, *, statement: bool
    ) -> str:
        """One arm's value: its statements run, then its last item is read.

        A void arm — one whose value is a ``ret``, a ``break`` or a ``set`` —
        lowers every item as a statement and yields nothing, which is what the
        checker typed the whole ``match`` as.
        """
        for item in body[:-1]:
            if self.block.term is not None:
                return ""
            if not isinstance(item, Node):
                raise diag(UNKNOWN_NODE, where, "a statement must be a form")
            self.statement(item)
        if self.block.term is not None:
            return ""
        if statement:
            if not isinstance(body[-1], Node):
                raise diag(UNKNOWN_NODE, where, "a statement must be a form")
            self.statement(body[-1])
            return ""
        return self.value(body[-1], where)

    def divide(self, kind: str, ty: Ty, left: str, right: str, path: str) -> str:
        """Emit ``left / right`` (or ``%``) with both traps guarded.

        The zero test comes first and calls ``rt_panic`` on the path of the
        operation. A signed division then branches around the one pair whose
        quotient does not fit, ``INT_MIN / -1``, so no ``idiv`` ever sees it.
        Both branches converge on a ``phi`` that carries the result.
        """
        if not (isinstance(ty, Prim) and ty.name in INTEGERS):
            raise diag(
                UNKNOWN_NODE, path, f"'{kind}' on {ty} is not lowered by this backend yet"
            )
        unsigned = ty.name.startswith("u")
        self.labels += 1
        tag = f"div{self.labels}"
        zero, ok = f"{tag}.zero", f"{tag}.ok"

        test = self.temp()
        self.emit(("icmp", test, "eq", ty, right, "0"))
        self.block.term = ("cbr", test, zero, ok)
        self.new_block(zero)
        self.panic(path, DIVISION_BY_ZERO, "division by zero")
        self.new_block(ok)

        if kind == "/":
            op = "udiv" if unsigned else "sdiv"
        else:
            op = "urem" if unsigned else "srem"

        if unsigned:
            dest = self.temp()
            self.emit((op, dest, ty, left, right))
            return dest

        low = _INT_MIN[ty.name]
        is_min = self.temp()
        is_neg_one = self.temp()
        overflows = self.temp()
        self.emit(("icmp", is_min, "eq", ty, left, str(low)))
        self.emit(("icmp", is_neg_one, "eq", ty, right, "-1"))
        self.emit(("and", overflows, Prim("bool"), is_min, is_neg_one))
        overflow, calc, join = f"{tag}.overflow", f"{tag}.calc", f"{tag}.join"
        self.block.term = ("cbr", overflows, overflow, calc)

        self.new_block(overflow)
        self.block.term = ("br", join)

        self.new_block(calc)
        quotient = self.temp()
        self.emit((op, quotient, ty, left, right))
        self.block.term = ("br", join)

        self.new_block(join)
        # ``INT_MIN / -1`` wraps to INT_MIN; ``INT_MIN % -1`` is 0. Neither is
        # computed, because computing either one would fault.
        wrapped = str(low) if kind == "/" else "0"
        result = self.temp()
        self.emit(("phi", result, ty, ((wrapped, overflow), (quotient, calc))))
        return result

    def call(self, node: Node) -> str:
        """A direct call: to a copy the checker chose, or to a definition.

        A generic call site has a recorded target — the copy the checker made
        and verified for these argument types. Falling back to the source name
        would call a function this module never emitted, or emit a second copy
        the checker never saw, so the record is the only source of truth here.
        """
        target = self.record.call_targets.get(node.path)
        if target is not None:
            instance = self.cm.instances.get(target)
            if instance is None:
                raise diag(
                    UNKNOWN_FUNCTION,
                    node.path,
                    f"no monomorphised copy named '{target}' was checked",
                )
            args = tuple(self.value(arg, node.path) for arg in node.items[1:])
            dest = "" if _is_void(instance.ret) else self.temp()
            self.emit(("call", dest, self.lowering.symbol(target), args))
            return dest
        symbol = self.lowering.symbols.get(node.kind)
        if symbol is None:
            raise diag(
                UNKNOWN_FUNCTION,
                node.path,
                f"cannot call '{node.kind}': no lowered definition in module "
                f"'{self.cm.module.name}'",
            )
        sig = self.cm.fn_sigs[node.kind]
        args = tuple(self.value(arg, node.path) for arg in node.items[1:])
        dest = "" if _is_void(sig.ret) else self.temp()
        self.emit(("call", dest, symbol, args))
        return dest

    # -- closures ---------------------------------------------------------

    def indirect_call(self, node: Node, sig: FnTy) -> str:
        """A call through a function value: extract both words, then ``icall``.

        The environment is passed ahead of the declared arguments, which is the
        one ABI a closure and a named function share. The code pointer is
        opaque in the value, so it is ``bitcast`` to the signature the checker
        gave the call — never called through an untyped pointer.
        """
        callee = self.load(node.kind, node.path)
        env = self.temp()
        self.emit(("extractvalue", env, sig, _VOID_PTR, callee, 0))
        code = self.temp()
        self.emit(("extractvalue", code, sig, _VOID_PTR, callee, 1))
        params = (_VOID_PTR, *sig.params)
        pointer = self.temp()
        self.emit(("fnptr", pointer, sig.ret, params, code))
        args = (env, *(self.value(arg, node.path) for arg in node.items[1:]))
        dest = "" if _is_void(sig.ret) else self.temp()
        self.emit(("icall", dest, sig.ret, params, pointer, args))
        return dest

    def fn_value(self, name: str, where: str) -> str:
        """A named function as a value: a wrapper's address, null environment.

        The value's ABI is always ``R (i8*, T...)``, but a definition takes
        ``(T...)``: calling one through the other's type would hand the
        environment pointer to the first parameter. A one-line wrapper per used
        function makes the two agree, so a named function and a closure are
        interchangeable values.
        """
        sig = self.cm.fn_sigs[name]
        wrapper = self.lowering.wrappers.get(name)
        if wrapper is None:
            symbol = self.lowering.symbols.get(name)
            if symbol is None:
                raise diag(
                    UNKNOWN_FUNCTION,
                    where,
                    f"cannot take the address of '{name}': no lowered definition",
                )
            helper = self.lowering.next_name("$fnvalue")
            fn = IrFn(
                helper,
                [],
                sig.ret,
                [IrBlock("entry")],
                symbol=self.lowering.symbol(helper),
                internal=True,
            )
            builder = _Fn(
                self.lowering,
                fn,
                path=self.path,
                ret=sig.ret,
                is_main=False,
                body=self.record,
            )
            builder.parameters(
                [
                    ("env", _VOID_PTR),
                    *[(f"p{index}", ty) for index, ty in enumerate(sig.params)],
                ],
                [self.path] * (len(sig.params) + 1),
            )
            args = tuple(
                builder.load(f"p{index}", self.path) for index in range(len(sig.params))
            )
            dest = "" if _is_void(sig.ret) else builder.temp()
            builder.emit(("call", dest, symbol, args))
            if dest:
                builder.block.term = ("ret", sig.ret, dest)
            else:
                builder.block.term = ("ret", Prim("void"), None)
            builder.finish()
            self.lowering.ir.functions.append(fn)
            wrapper = self.lowering.symbol(helper)
            self.lowering.wrappers[name] = wrapper
        code = self.temp()
        self.emit(("faddr", code, wrapper))
        return self.fn_pair(sig, "null", code)

    def fn_pair(self, sig: FnTy, env: str, code: str) -> str:
        """The two-word value ``{ environment, code }`` for a function type."""
        partial = self.temp()
        self.emit(("insertvalue", partial, sig, "undef", _VOID_PTR, env, 0))
        whole = self.temp()
        self.emit(("insertvalue", whole, sig, partial, _VOID_PTR, code, 1))
        return whole

    def closure(self, node: Node) -> str:
        """``(fn ...)``: build the environment, take the code's address.

        The environment is one stack slot per closure expression, holding a
        copy of each captured value. It is written where the expression
        appears, so a ``set`` of the outer binding afterwards reaches the
        binding and not the copy. A closure that captures nothing passes a null
        environment: there is nothing to point at.
        """
        info = self.record.closures.get(node.path)
        if info is None:
            raise diag(
                UNKNOWN_NODE, node.path, "the checker recorded no closure here"
            )
        name = self.lowering.next_lambda()
        symbol = self.lowering.symbol(name)
        env = self.environment(info, node.path)
        code = self.temp()
        self.emit(("faddr", code, symbol))
        value = self.fn_pair(FnTy(info.params, info.ret), env, code)
        self.lambda_fn(info, node, name, symbol)
        return value

    def environment(self, info: Closure, path: str) -> str:
        """The closure's environment as an ``i8*``, or ``null`` when empty."""
        if not info.captures:
            return "null"
        fields = tuple(ty for _, ty in info.captures)
        slot = f"{self.binding()}.addr"
        self.allocas.append(("allocarec", slot, fields))
        for index, (captured, ty) in enumerate(info.captures):
            self.lowering.use_type(ty, path)
            address = self.temp()
            self.emit(("geprec", address, fields, slot, index))
            self.emit(("store", ty, self.load(captured, path), address))
        env = self.temp()
        self.emit(("bitcastout", env, fields, _VOID_PTR, slot))
        return env

    def lambda_fn(self, info: Closure, node: Node, name: str, symbol: str) -> None:
        """Lower one closure body as a function of its own.

        The environment arrives as the first parameter. Each captured name is
        bound to the address of its field inside it, so a read of ``k`` inside
        the body is the same ``load`` a parameter's read is — and there is no
        slot to write to, which is why a capture cannot be assigned.
        """
        fn = IrFn(
            name,
            [],
            info.ret,
            [IrBlock("entry")],
            symbol=symbol,
            internal=True,
        )
        # The return type is materialised by the ``define`` line: a closure
        # that returns a result or an enum names it here first.
        self.lowering.use_type(info.ret, node.path)
        builder = _Fn(
            self.lowering,
            fn,
            path=node.path,
            ret=info.ret,
            is_main=False,
            body=self.record,
        )
        builder.captures(info, node.path)
        builder.parameters(
            list(zip(info.names, info.params)),
            [node.path] * len(info.params),
        )
        builder.body(list(node.items[_closure_body_start(node) :]))
        builder.finish()
        self.lowering.ir.functions.append(fn)

    def captures(self, info: Closure, path: str) -> None:
        """Bind each captured name to its field inside the environment."""
        self.fn.params.append(("env", _VOID_PTR))
        if not info.captures:
            return
        fields = tuple(ty for _, ty in info.captures)
        base = self.temp()
        self.emit(("bitcastrec", base, fields, _VOID_PTR, "%env"))
        for index, (name, ty) in enumerate(info.captures):
            self.lowering.use_type(ty, path)
            address = self.temp()
            self.emit(("geprec", address, fields, base, index))
            self.vars[name] = (ty, address)

    def builtin(self, node: Node) -> str:
        """``print``/``println``: format one value, then end the line.

        The argument keeps its own type and picks the entry point that already
        knows how to spell it: ``rt_print_str`` for bytes and their length,
        ``rt_print_i64``, ``rt_print_u64``, ``rt_print_f64``. A ``:bool`` is
        the one value with two spellings, so it branches on it and prints
        ``true`` or ``false`` — never its 0/1 representation, which is a fact
        about ``i1`` and not about the language.

        A string literal skips the aggregate: its bytes and its length are
        known here, and building a value only to take it apart again would be
        work with no observable effect.
        """
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes one value")
        arg = items[1]
        ty = self.type_of_item(arg, node.path)
        if not (isinstance(ty, Prim) and ty.name in PRINTABLE):
            raise diag(
                TYPE_MISMATCH,
                _path_of(arg, node.path),
                f"'{node.kind}' takes one of :i64, :u64, :f64, :bool or :str, found {ty}",
            )
        if ty.name == "str":
            self.print_string(arg, node.path)
        elif ty.name == "bool":
            self.print_bool(arg, node.path)
        else:
            decl = {
                "i64": _RT_PRINT_I64,
                "u64": _RT_PRINT_U64,
                "f64": _RT_PRINT_F64,
            }[ty.name]
            self.lowering.declare(decl)
            self.emit(("call", "", decl.name, (self.value(arg, node.path),)))
        if node.kind == "println":
            self.lowering.declare(_RT_PRINTLN)
            self.emit(("call", "", "rt_println", ()))
        return ""

    def print_string(self, item: Node | Token, where: str) -> None:
        """One ``:str`` through ``rt_print_str``, literal or computed."""
        self.lowering.declare(_RT_PRINT_STR)
        if isinstance(item, Token) and item.kind == STRING:
            data = item.value.encode("utf-8")
            pointer = self.temp()
            self.emit(("gaddr", pointer, self.lowering.intern(data)))
            length = str(len(data))
        else:
            pointer, length = self.str_parts(item, where)
        self.emit(("call", "", "rt_print_str", (pointer, length)))

    def print_bool(self, item: Node | Token, where: str) -> None:
        """``true`` or ``false``: two blocks, one per spelling."""
        condition = self.value(item, where)
        self.labels += 1
        tag = f"bool{self.labels}"
        yes, no, join = f"{tag}.true", f"{tag}.false", f"{tag}.join"
        self.block.term = ("cbr", condition, yes, no)
        for label, text in ((yes, b"true"), (no, b"false")):
            self.new_block(label)
            pointer = self.temp()
            self.emit(("gaddr", pointer, self.lowering.intern(text)))
            self.lowering.declare(_RT_PRINT_STR)
            self.emit(("call", "", "rt_print_str", (pointer, str(len(text)))))
            self.block.term = ("br", join)
        self.new_block(join)

    def panic(self, path: str, code: str, message: str) -> None:
        """Call ``rt_panic`` with three compiler-generated C strings.

        ``rt_panic`` never returns, so the block ends in ``unreachable``. The
        path is the node path of the operation, which is what a person needs to
        find the division that panicked.
        """
        self.lowering.declare(_RT_PANIC)
        args = tuple(
            self._c_argument(text) for text in (path, code, message)
        )
        self.emit(("call", "", "rt_panic", args))
        self.block.term = ("unreachable",)

    def _c_argument(self, text: str) -> str:
        pointer = self.temp()
        self.emit(("gaddr", pointer, self.lowering.c_string(text)))
        return pointer


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _pointee(ty: Ty) -> Ty | None:
    """The type a pointer points at, or ``None`` when ``ty`` is not a pointer."""
    if isinstance(ty, Shared):
        ty = ty.elem
    if isinstance(ty, (Own, Ptr)):
        return ty.elem
    return None


#: Bytes and alignment of every primitive, spelled as LLVM lays them out.
#: ``:bool`` is ``i1`` and an ``i1`` in memory is one byte; ``:str`` is the
#: sixteen-byte ``{ i8*, i64 }`` aggregate the spec fixes.
_SIZES = {
    "i8": 1,
    "u8": 1,
    "bool": 1,
    "i16": 2,
    "u16": 2,
    "i32": 4,
    "u32": 4,
    "f32": 4,
    "i64": 8,
    "u64": 8,
    "f64": 8,
    "str": 16,
    # Runtime handles: one opaque pointer each.
    "thread": 8,
    "mutex": 8,
}

_FLOATS = ("f32", "f64")


def enum_words(
    name: str,
    enums: dict[str, tuple[tuple[str, Ty | None], ...]],
    structs: dict[str, tuple[tuple[str, Ty], ...]],
    memo: dict[str, int] | None = None,
) -> int:
    """How many eight-byte words an enum's payload area must hold.

    The largest variant decides it: every value of the enum is one LLVM type,
    so the area is sized for the widest payload any variant carries. An enum
    with no payloads needs none, and its type is ``{ i32, [0 x i64] }``.

    ``memo`` is the caller's name-to-width table. An enum reached twice — as
    two variants' payload, or through a struct — is measured once, and the
    lookups a repeated one would make cannot walk a cycle a second time.
    """
    if memo is not None:
        done = memo.get(name)
        if done is not None:
            return done
    size = 0
    for _, payload in enums.get(name, ()):
        if payload is not None:
            size = max(size, sizeof_of(payload, structs, enums, memo))
    words = (size + 7) // 8
    if memo is not None:
        memo[name] = words
    return words


def _fields_size(
    fields: "tuple[Ty, ...]",
    structs: dict[str, tuple[tuple[str, Ty], ...]],
    enums: dict[str, tuple[tuple[str, Ty | None], ...]],
    memo: dict[str, int] | None = None,
) -> int:
    """C layout for one field list: each field padded to its own alignment."""
    offset = 0
    align = 1
    for field in fields:
        field_align = align_of(field, structs, enums, memo)
        offset = (offset + field_align - 1) // field_align * field_align
        offset += sizeof_of(field, structs, enums, memo)
        align = max(align, field_align)
    return (offset + align - 1) // align * align


def sizeof_of(
    ty: Ty,
    structs: dict[str, tuple[tuple[str, Ty], ...]],
    enums: dict[str, tuple[tuple[str, Ty | None], ...]] | None = None,
    memo: dict[str, int] | None = None,
) -> int:
    """The size in bytes of one ``ty``, as LLVM lays it out.

    A struct is its fields in declaration order, each padded to its own
    alignment and the whole padded to the largest of them — C's rule, which is
    the rule LLVM applies to an identified struct. An empty struct is zero
    bytes here, because ``{ }`` is zero bytes; the language never allocates one
    more widely than that, and nothing reads through such a pointer.

    A tagged value is laid out from the fields ``type_text`` spells: a
    ``(res T E)`` is ``{ i1, T, E }`` and a ``(opt T)`` is ``{ i1, T }``, so
    the same padding rule answers both. An enum is ``{ i32, [K x i64] }``,
    eight bytes of tag area and ``K`` words of payload.
    """
    enums = enums or {}
    if isinstance(ty, Prim):
        return _SIZES.get(ty.name, 1)
    if isinstance(ty, Shared):
        return sizeof_of(ty.elem, structs, enums, memo)
    if isinstance(ty, (Chan, Atomic)):
        # One opaque pointer to the runtime object.
        return 8
    if isinstance(ty, (Ptr, Own)):
        return 8
    if isinstance(ty, FnTy):
        # ``{ i8*, i8* }``: the environment and the code pointer.
        return 16
    if isinstance(ty, Vec):
        # ``{ T*, i64, i64 }``: a buffer pointer and two counts.
        return 24
    if isinstance(ty, Arr):
        return ty.n * sizeof_of(ty.elem, structs, enums, memo)
    if isinstance(ty, EnumRef):
        # The tag, then the payload area: ``{ i32, [K x i64] }`` pads the i32
        # out to the array's alignment, so the tag costs one word.
        return 8 + 8 * enum_words(ty.name, enums, structs, memo)
    if isinstance(ty, Opt):
        return _fields_size((Prim("bool"), ty.elem), structs, enums, memo)
    if isinstance(ty, Res):
        return _fields_size((Prim("bool"), ty.ok, ty.err), structs, enums, memo)
    if isinstance(ty, StructRef):
        fields = structs.get(ty.name, ())
        return _fields_size(tuple(field for _, field in fields), structs, enums, memo)
    return 1


def align_of(
    ty: Ty,
    structs: dict[str, tuple[tuple[str, Ty], ...]],
    enums: dict[str, tuple[tuple[str, Ty | None], ...]] | None = None,
    memo: dict[str, int] | None = None,
) -> int:
    """The alignment in bytes of one ``ty``: what ``rt_alloc`` is asked for."""
    enums = enums or {}
    if isinstance(ty, Prim):
        if ty.name in _FLOATS:
            return 4 if ty.name == "f32" else 8
        if ty.name == "str":
            # ``%str = { i8*, i64 }``: two eight-byte fields. The aggregate is
            # sixteen bytes wide and eight-byte aligned; alignment is the
            # largest field's, not the size.
            return 8
        return min(_SIZES.get(ty.name, 1), 16)
    if isinstance(ty, Shared):
        return align_of(ty.elem, structs, enums, memo)
    if isinstance(ty, (Chan, Atomic)):
        return 8
    if isinstance(ty, (Ptr, Own)):
        return 8
    if isinstance(ty, FnTy):
        # Two pointers: the alignment is the pointer's, not the size.
        return 8
    if isinstance(ty, Vec):
        # A pointer and two counts: the alignment is the pointer's.
        return 8
    if isinstance(ty, EnumRef):
        # The payload area is sixteen-byte words at best eight-byte aligned,
        # and so is the tag.
        return 8
    if isinstance(ty, Arr):
        return align_of(ty.elem, structs, enums, memo)
    if isinstance(ty, Opt):
        return max(1, align_of(ty.elem, structs, enums, memo))
    if isinstance(ty, Res):
        return max(
            1,
            align_of(ty.ok, structs, enums, memo),
            align_of(ty.err, structs, enums, memo),
        )
    if isinstance(ty, StructRef):
        fields = structs.get(ty.name, ())
        return max([align_of(field, structs, enums, memo) for _, field in fields] or [1])
    return 1


def _is_marker(item: Node | Token) -> bool:
    """Whether ``item`` is the ``:`` in front of a composite type."""
    return isinstance(item, Token) and item.kind == ATOM and item.value == ":"


def _closure_body_start(node: Node) -> int:
    """The first statement of a ``(fn ...)`` expression, after its signature.

    ``(fn :i64 ((a :i64)) ...)`` puts the body at index three; a composite
    return type is written behind the marker, ``(fn :(i64) ...)`` — the marker
    and the list are two items — so the body moves to index four.
    """
    return 4 if len(node.items) > 1 and _is_marker(node.items[1]) else 3


def _is_true(item: Node | Token) -> bool:
    """Whether a loop condition is the literal ``true``, which never exits.

    A ``Node`` or any other name is not constant: only the exact atom ``true``
    lets the head skip its exit edge. ``(while flag …)`` with ``flag :bool``
    keeps both edges, even if the flag happens to be true.
    """
    return isinstance(item, Token) and item.kind == ATOM and item.value == "true"


def _is_i64(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "i64"


def _path_of(item: Node | Token, where: str) -> str:
    """The item's own node path, or ``where`` when a token has none."""
    return item.path if isinstance(item, Node) and item.path else where


def _float_text(text: str, where: str, widened: Ty | None = None) -> str:
    """Spell a float literal the way LLVM reads an operand of its width.

    LLVM's decimal form needs a digit, a dot and then whatever exponent the
    value has, so ``2.0`` stays ``2.0`` and ``1e300`` becomes ``1.0e+300``.
    Python's ``repr`` is the shortest text that reads back as the same double,
    which is also what the runtime prints, so a literal and the value it
    formats agree.

    One decimal cannot serve both widths. LLVM accepts a decimal constant for a
    ``float`` only when the value is exactly single precision, so ``0.1`` at
    ``:f32`` is refused as "floating point constant invalid for type". The
    checker records the width each literal was checked at, and a value the
    ``float`` cannot hold exactly is spelled in hexadecimal: the bits of the
    double that *is* the single-precision value, a form LLVM reads at either
    width because it names an exact number. The rounding happens here once, at
    compile time, instead of being left for LLVM to reject.
    """
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise diag(
            LITERAL_RANGE, where, f"float literal '{text}' is not a finite :f64"
        )
    if isinstance(widened, Prim) and widened.name == "f32":
        single = _single(value)
        if single != value:
            return _hex_float(single)
    spelled = repr(value)
    if "e" in spelled or "E" in spelled:
        mantissa, _, exponent = spelled.partition("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{exponent}"
    if "." not in spelled:
        return spelled + ".0"
    return spelled


def _single(value: float) -> float:
    """``value`` rounded to ``:f32``, as the double that holds those bits."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _hex_float(value: float) -> str:
    """``value`` as LLVM's hexadecimal double literal, ``0x3FF19999A0000000``."""
    return "0x{:016X}".format(struct.unpack("<Q", struct.pack("<d", value))[0])
