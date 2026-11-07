"""The intermediate representation: flat blocks of instruction tuples.

Shape
-----
An ``IrModule`` is a target triple, its private globals, the runtime functions
it calls but does not define, and its functions. A function is a name, its
parameters, its return type and a list of blocks. A block is a label, a list of
instructions, and exactly one terminator.

Instructions are tuples rather than classes because the IR exists to be read
and printed, not to be walked by a dozen visitors: item 0 is the opcode and the
rest are operands. The vocabulary is closed and small, and
:func:`logos.llvm.emit` raises on an opcode it does not know, so a lowering bug
fails loudly instead of printing something that merely looks like LLVM IR.

=========================  ===================================================
Opcode                     Tuple
=========================  ===================================================
``alloca``                 ``(dest, ty)`` — a named stack slot
``store``                  ``(ty, value, slot)``
``load``                   ``(dest, ty, slot)``
``gaddr``                  ``(dest, global)`` — address of a global's byte 0
``add sub mul``            ``(dest, ty, left, right)``
``sdiv srem udiv urem``    ``(dest, ty, left, right)``
``and``                    ``(dest, ty, left, right)`` — on an ``i1``
``icmp``                   ``(dest, pred, ty, left, right)``
``insertvalue``            ``(dest, agg_ty, agg, elem_ty, value, index)``
``extractvalue``           ``(dest, agg_ty, elem_ty, agg, index)``
``gep``                    ``(dest, base_ty, base, ((ty, index), ...))``
``sext zext``              ``(dest, from_ty, to_ty, value)``
``bitcast``                ``(dest, from_ty, to_ty, value)``
``phi``                    ``(dest, ty, ((value, label), ...))``
``trunc``                  ``(dest, from_ty, to_ty, value)``
``call``                   ``(dest, callee, (arg, ...))``
``allocarec``              ``(dest, fields)`` — a stack slot of a literal struct
``geprec``                 ``(dest, fields, base, index)`` — address of one field
``bitcastrec``             ``(dest, fields, from_ty, value)`` — value to record ``*``
``bitcastout``             ``(dest, fields, to_ty, value)`` — record ``*`` to value
``faddr``                  ``(dest, symbol)`` — a function's address as ``i8*``
``fnptr``                  ``(dest, ret, params, value)`` — ``i8*`` to a callable
``icall``                  ``(dest, ret, params, callee, (arg, ...))``
=========================  ===================================================

Terminators:

=========================  ===================================================
``ret``                    ``(ty, value_or_None)`` — ``None`` is ``ret void``
``br``                     ``(label,)``
``cbr``                    ``(cond, then_label, else_label)``
``unreachable``            ``()`` — after a call to a ``_Noreturn`` function
=========================  ===================================================

Names
-----
Every local and every block has a name; nothing is numbered implicitly by the
printer. A local is spelled with its ``%`` (``%t3``, ``%v.0.addr``) because an
operand may equally be a literal (``41``) and that sigil is what tells the two
apart. A global (``.str0``), a function (``lg.m.add``) and a label (``entry``)
are spelled bare; the printer adds ``@`` or ``%`` where LLVM wants one and
quotes the name when it is not something LLVM reads unquoted.

Ordering
--------
``globals`` are in first-use order, so the same source always produces the
same module byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from logos.types import Ty

#: The one target this compiler produces code for.
TRIPLE = "x86_64-unknown-linux-gnu"

@dataclass(frozen=True, slots=True)
class IrGlobal:
    """A private constant in the module: ``data`` is the exact byte range.

    No terminator is appended. A Logos string carries its own length, so the
    bytes here are exactly the bytes ``rt_write`` is handed.
    """

    name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class IrDecl:
    """A function this module calls but does not define."""

    name: str
    params: tuple[Ty, ...]
    ret: Ty


@dataclass(frozen=True, slots=True)
class IrEnum:
    """One enum type: its payload width, in eight-byte words.

    An enum's LLVM type is ``{ i32, [words x i64] }`` — a tag and a payload
    area wide enough for its largest variant. The width is a fact the spelling
    pass cannot read off a name, so the module carries it: the same reason
    ``IrStruct`` carries its fields.
    """

    name: str
    words: int


@dataclass(frozen=True, slots=True)
class IrStruct:
    """One named struct type: its fields in declaration order.

    The printer spells it ``%struct.<name> = type { ... }``; the field list is
    what gives it C layout, so the order must never be sorted or grouped. Only
    structs a function actually materialises are listed, and every dependency
    is listed before the struct that names it.
    """

    name: str
    fields: tuple[Ty, ...]


@dataclass(slots=True)
class IrBlock:
    """One basic block: a label, its instructions and its terminator."""

    label: str
    instrs: list[tuple] = field(default_factory=list)
    term: tuple | None = None


@dataclass(slots=True)
class IrFn:
    """One function. ``params`` pairs each emitted name with its type, in order.

    ``name`` is the source name the checker and the diagnostics use; ``symbol``
    is the linker-visible one, empty when it is the source name (``main``). A
    program function is ``internal`` and mangled ``lg.<module>.<name>``, so a
    source name can never interpose on a libc or runtime symbol. A parameter's
    name is already the safe spelling the lowerer chose (``v.0``), not the
    source's.
    """

    name: str
    params: list[tuple[str, Ty]]
    ret: Ty
    blocks: list[IrBlock] = field(default_factory=list)
    symbol: str = ""
    internal: bool = False


@dataclass(slots=True)
class IrModule:
    """A whole translation unit.

    ``declares``, ``structs`` and ``enums`` are the fields beyond the three the
    plan names, and all have defaults, so ``IrModule(triple, globals,
    functions)`` still constructs a module with nothing to declare and no named
    types.
    """

    triple: str
    globals: list[IrGlobal]
    functions: list[IrFn]
    declares: list[IrDecl] = field(default_factory=list)
    structs: list[IrStruct] = field(default_factory=list)
    enums: list[IrEnum] = field(default_factory=list)
