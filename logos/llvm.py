"""Print an :class:`logos.ir.IrModule` as LLVM IR text.

The text is the interface to clang: nothing here links or optimises, it only
spells the IR the way LLVM 14 reads it. One function owns that spelling, so a
change in how a type or an operand prints happens in one place.

Typed pointers, not ``ptr``
--------------------------
LLVM 14 parses ``ptr`` only in its experimental ``-opaque-pointers`` mode: give
clang 14 a module that says ``ptr`` and it answers "ptr type is only supported
in -opaque-pointers mode / expected type". This emitter therefore spells
pointers the way clang 14 spells them, as ``i8*`` at the C boundary and
``[N x i8]*`` for a string global. Nothing here needs a pointer's element type
to be known, which is why the boundary is enough.

Strings
-------
A string global is an array of exactly its own bytes — no NUL terminator,
because a Logos string carries its length and ``rt_write`` is handed that
length. Every byte outside printable ASCII is escaped as ``\\XX``, so a string
containing a quote, a backslash or a newline round-trips.

Symbols
-------
A function is printed at ``fn.symbol`` when it has one (the lowerer mangles a
program function to ``lg.<module>.<name>``) and with ``internal`` linkage when
``fn.internal`` is set; ``main`` keeps its name and external linkage. A name
LLVM does not read unquoted is printed as a quoted identifier, so no source
name and no module name can produce text clang refuses.

Structs
-------
A named struct is an LLVM identified type, ``%struct.<name>``, defined once at
the top of the module as its fields in declaration order:
``%struct.pair = type { i8, i64 }``. That order is what gives the aggregate C
layout: LLVM lays identified structs out like C, with natural alignment and
padding between fields, so the same declaration is 16 bytes here and 16 in C.
Nothing here reorders or packs fields. An array is ``[n x T]``, which is also
C's layout for the same bytes.
"""

from __future__ import annotations

import re

from logos.ir import IrBlock, IrDecl, IrFn, IrGlobal, IrModule
from logos.types import Arr, Own, Prim, Ptr, StructRef, Ty, Vec

#: Closed primitive spelling. ``:str`` is the frozen 16-byte fat pointer the
#: spec defines. A ``(vec T)`` is a literal three-word struct, so its layout is
#: read off the type itself and needs no identified name.
_PRIM = {
    "i8": "i8",
    "i16": "i16",
    "i32": "i32",
    "i64": "i64",
    "u8": "i8",
    "u16": "i16",
    "u32": "i32",
    "u64": "i64",
    "bool": "i1",
    "f32": "float",
    "f64": "double",
    "str": "{ i8*, i64 }",
    "void": "void",
}

#: Opcodes that print as ``dest = op ty a, b``.
_BINARY = ("add", "sub", "mul", "sdiv", "srem", "udiv", "urem", "and")

#: A name LLVM reads unquoted: ``[-a-zA-Z$._][-a-zA-Z$._0-9]*``.
_UNQUOTED = re.compile(r"[-a-zA-Z$._][-a-zA-Z$._0-9]*\Z")

#: Struct layouts the lowerer has materialised this process, so a caller can
#: ask for a definition line by name alone (``struct_llty("pair")``). Emitted
#: IR never reads this table — the module carries its own struct list — so a
#: second compilation cannot make the first one's text wrong. The table is
#: reset at the start of every lowering and rebuilt by :func:`emit`, so an
#: answer always describes the module processed last.
_STRUCTS: dict[str, tuple[Ty, ...]] = {}


def emit(m: IrModule) -> str:
    """Return the LLVM IR text for ``m``, trailing newline included.

    The registry is rebuilt from ``m`` before the text is spelled, so
    :func:`struct_llty` describes the module just emitted and never an earlier
    one that happened to use the same struct name.
    """
    reset_structs()
    for struct in m.structs:
        register_struct(struct.name, struct.fields)
    return _Text(m).module()


def _named(sigil: str, name: str) -> str:
    """Spell ``name`` after its ``@`` or ``%``, quoted unless LLVM reads it bare.

    A source name is sanitised by the lowerer before it reaches the IR, but a
    mangled symbol still carries the module name, and nothing about a Logos
    atom guarantees LLVM's identifier grammar. Quoting is total: any byte
    outside printable ASCII, and any quote or backslash, is escaped ``\\XX``.
    """
    if _UNQUOTED.match(name):
        return f"{sigil}{name}"
    parts = []
    for byte in name.encode("utf-8"):
        if 0x20 <= byte < 0x7F and byte not in (0x22, 0x5C):
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02X}")
    return f'{sigil}"' + "".join(parts) + '"'


def type_text(ty: Ty) -> str:
    """Spell ``ty`` as an LLVM type, or raise ``ValueError`` with no layout.

    A ``(ptr T)`` and an ``(own T)`` are the same machine value, ``T*``: the
    difference between them is what the checker knows about it, not what the
    backend stores. ``T`` still has to have a layout of its own, so the walk
    that refuses an unlowered type descends through both before asking here.

    A ``(vec T)`` is three words in a literal struct, ``{ T*, i64, i64 }``:
    the buffer, its length in elements and its capacity in elements. It is
    spelled as a literal rather than an identified struct so two vectors of
    different elements cannot share a name, and so the layout is read off the
    type itself with no registry to keep in step.
    """
    if isinstance(ty, Prim):
        spelled = _PRIM.get(ty.name)
        if spelled is not None:
            return spelled
    if isinstance(ty, (Ptr, Own)):
        return f"{type_text(ty.elem)}*"
    if isinstance(ty, Vec):
        return f"{{ {type_text(ty.elem)}*, i64, i64 }}"
    if isinstance(ty, Arr):
        return f"[{ty.n} x {type_text(ty.elem)}]"
    if isinstance(ty, StructRef):
        return struct_name(ty.name)
    raise ValueError(f"no LLVM layout for type {ty}")


def struct_name(name: str) -> str:
    """The LLVM identified type for a Logos struct: ``%struct.<name>``.

    Quoted as a whole when the source name needs it: ``%struct.point`` reads
    bare, and an atom with a space in it becomes ``%"struct.odd name"``, which
    is one valid identified-type name rather than a broken concatenation.
    """
    return _named("%", f"struct.{name}")


def reset_structs() -> None:
    """Forget every registered layout, so the next module starts clean."""
    _STRUCTS.clear()


def register_struct(name: str, fields: "tuple[Ty, ...]") -> None:
    """Record a struct's layout so :func:`struct_llty` can spell it by name."""
    _STRUCTS[name] = tuple(fields)


def struct_llty(name: str) -> str:
    """The definition line for struct ``name``: ``%struct.name = type { ... }``.

    Layout is the field list in declaration order, C's natural alignment. The
    lowerer registers each struct it materialises, so this is readable after a
    module is lowered; an unknown name is a ``ValueError`` rather than an empty
    definition that would silently agree with itself.
    """
    fields = _STRUCTS.get(name)
    if fields is None:
        raise ValueError(f"no layout registered for struct '{name}'")
    return struct_line(name, fields)


def struct_line(name: str, fields: "tuple[Ty, ...]") -> str:
    """Spell one struct's definition from its field list, without the registry."""
    inner = ", ".join(type_text(field) for field in fields)
    body = f"{{ {inner} }}" if inner else "{ }"
    return f"{struct_name(name)} = type {body}"


class _Text:
    """One module's spelling pass, holding the context each line needs."""

    def __init__(self, m: IrModule) -> None:
        self.m = m
        self.globals = {g.name: g for g in m.globals}
        self.sigs: dict[str, tuple[tuple[Ty, ...], Ty]] = {
            d.name: (d.params, d.ret) for d in m.declares
        }
        for fn in m.functions:
            self.sigs[fn.symbol or fn.name] = (
                tuple(ty for _, ty in fn.params),
                fn.ret,
            )

    # -- module ----------------------------------------------------------

    def module(self) -> str:
        lines = ["; generated by logos", f'target triple = "{self.m.triple}"', ""]
        # A dependency is listed before the struct that names it, so every
        # ``%struct.inner`` a definition refers to already exists on its left.
        for struct in self.m.structs:
            lines.append(struct_line(struct.name, struct.fields))
        if self.m.structs:
            lines.append("")
        for global_ in self.m.globals:
            lines.append(self.global_line(global_))
        if self.m.globals:
            lines.append("")
        for decl in self.m.declares:
            lines.append(self.decl_line(decl))
        if self.m.declares:
            lines.append("")
        for index, fn in enumerate(self.m.functions):
            if index:
                lines.append("")
            lines.extend(self.fn_lines(fn))
        return "\n".join(lines) + "\n"

    def global_line(self, global_: IrGlobal) -> str:
        array = f"[{len(global_.data)} x i8]"
        init = _c_string(global_.data) if global_.data else "zeroinitializer"
        return f"@{global_.name} = private unnamed_addr constant {array} {init}"

    def decl_line(self, decl: IrDecl) -> str:
        params = ", ".join(type_text(ty) for ty in decl.params)
        return f"declare {type_text(decl.ret)} {_named('@', decl.name)}({params})"

    # -- functions -------------------------------------------------------

    def fn_lines(self, fn: IrFn) -> list[str]:
        params = ", ".join(
            f"{type_text(ty)} {_named('%', name)}" for name, ty in fn.params
        )
        linkage = "internal " if fn.internal else ""
        symbol = _named("@", fn.symbol or fn.name)
        lines = [f"define {linkage}{type_text(fn.ret)} {symbol}({params}) {{"]
        for block in fn.blocks:
            lines.extend(self.block_lines(block))
        lines.append("}")
        return lines

    def block_lines(self, block: IrBlock) -> list[str]:
        lines = [f"{block.label}:"]
        for instr in block.instrs:
            lines.append(f"  {self.instr(instr)}")
        lines.append(f"  {self.term(block)}")
        return lines

    # -- instructions ----------------------------------------------------

    def instr(self, instr: tuple) -> str:
        op = instr[0]
        if op == "alloca":
            _, dest, ty = instr
            return f"{dest} = alloca {type_text(ty)}"
        if op == "store":
            _, ty, value, slot = instr
            spelled = type_text(ty)
            return f"store {spelled} {value}, {spelled}* {slot}"
        if op == "load":
            _, dest, ty, slot = instr
            spelled = type_text(ty)
            return f"{dest} = load {spelled}, {spelled}* {slot}"
        if op == "gaddr":
            _, dest, name = instr
            size = self.size(name)
            return (
                f"{dest} = getelementptr inbounds [{size} x i8], "
                f"[{size} x i8]* {_named('@', name)}, i64 0, i64 0"
            )
        if op in _BINARY:
            _, dest, ty, left, right = instr
            spelled = type_text(ty)
            return f"{dest} = {op} {spelled} {left}, {right}"
        if op == "phi":
            _, dest, ty, incoming = instr
            spelled = type_text(ty)
            pairs = ", ".join(f"[ {value}, %{label} ]" for value, label in incoming)
            return f"{dest} = phi {spelled} {pairs}"
        if op == "trunc":
            _, dest, from_ty, to_ty, value = instr
            return f"{dest} = trunc {type_text(from_ty)} {value} to {type_text(to_ty)}"
        if op == "icmp":
            _, dest, pred, ty, left, right = instr
            spelled = type_text(ty)
            return f"{dest} = icmp {pred} {spelled} {left}, {right}"
        if op == "insertvalue":
            _, dest, agg_ty, agg, elem_ty, value, index = instr
            return (
                f"{dest} = insertvalue {type_text(agg_ty)} {agg}, "
                f"{type_text(elem_ty)} {value}, {index}"
            )
        if op == "extractvalue":
            _, dest, agg_ty, elem_ty, agg, index = instr
            return (
                f"{dest} = extractvalue {type_text(agg_ty)} {agg}, {index}"
            )
        if op == "gep":
            _, dest, base_ty, base, indices = instr
            spelled = type_text(base_ty)
            steps = ", ".join(f"{ty} {index}" for ty, index in indices)
            return (
                f"{dest} = getelementptr {spelled}, {spelled}* {base}, {steps}"
            )
        if op in ("sext", "zext"):
            _, dest, from_ty, to_ty, value = instr
            return (
                f"{dest} = {op} {type_text(from_ty)} {value} "
                f"to {type_text(to_ty)}"
            )
        if op == "bitcast":
            _, dest, from_ty, to_ty, value = instr
            return (
                f"{dest} = bitcast {type_text(from_ty)} {value} "
                f"to {type_text(to_ty)}"
            )
        if op == "call":
            return self.call(instr)
        raise ValueError(f"unknown IR opcode '{op}'")

    def call(self, instr: tuple) -> str:
        _, dest, callee, args = instr
        sig = self.sigs.get(callee)
        if sig is None:
            # The lowerer owns the diagnostic for a call with no definition;
            # reaching this means the IR was built by hand and is inconsistent.
            raise ValueError(f"call to undefined function '@{callee}'")
        params, ret = sig
        if len(params) != len(args):
            raise ValueError(
                f"'{callee}' takes {len(params)} arguments, call passes {len(args)}"
            )
        spelled = ", ".join(
            f"{type_text(ty)} {arg}" for ty, arg in zip(params, args)
        )
        text = f"call {type_text(ret)} {_named('@', callee)}({spelled})"
        return f"{dest} = {text}" if dest else text

    def size(self, name: str) -> int:
        global_ = self.globals.get(name)
        if global_ is None:
            raise ValueError(f"reference to undeclared global '@{name}'")
        return len(global_.data)

    # -- terminators -----------------------------------------------------

    def term(self, block: IrBlock) -> str:
        term = block.term
        if term is None:
            raise ValueError(f"block '{block.label}' has no terminator")
        op = term[0]
        if op == "ret":
            _, ty, value = term
            spelled = type_text(ty)
            return "ret void" if value is None else f"ret {spelled} {value}"
        if op == "br":
            _, target = term
            return f"br label %{target}"
        if op == "cbr":
            _, cond, then_label, else_label = term
            return f"br i1 {cond}, label %{then_label}, label %{else_label}"
        if op == "unreachable":
            return "unreachable"
        raise ValueError(f"unknown IR terminator '{op}'")


def _c_string(data: bytes) -> str:
    """Spell ``data`` as an LLVM ``c"..."`` literal, one escape per odd byte."""
    parts = []
    for byte in data:
        if 0x20 <= byte < 0x7F and byte not in (0x22, 0x5C):
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02X}")
    return 'c"' + "".join(parts) + '"'
