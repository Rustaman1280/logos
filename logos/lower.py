"""Lower a checked module to the IR.

Scope
-----
This is the first backend, so it lowers what the first binary needs: integer
literals, ``let``, ``ret``, calls, ``print`` and ``println`` over a string
literal. A construct the checker accepts but this pass does not lower yet is
reported as a diagnostic at its own path — never a ``KeyError``, and never
silently miscompiled. ``if``, ``while``, ``set``, ``match`` and the rest arrive
with the tasks that own them.

Values and slots
----------------
Every binding lives in a named stack slot, and every read of it is an explicit
``load`` into a fresh temporary. That is deliberately unoptimised: clang's
``-O2`` promotes the slots away, and until it does the IR shows one line per
source-level operation. A parameter is stored into its slot on entry, so a
parameter and a ``let`` are read the same way.

Names
-----
A slot is ``%<name>.addr``, a temporary is ``%t<n>``, and a function, global or
block is spelled bare. Slots keep the source name because that is what makes
the emitted IR readable; temporaries are numbered per function. Every name is
checked against the ones already in use, so a parameter called ``t1`` cannot
collide with the first temporary.

Strings
-------
A string literal becomes one private global holding exactly its bytes, shared
by every use of the same bytes. ``println`` writes those bytes and then the one
shared newline global, never a ``printf`` format: the length comes from the
literal, not from a scan for a terminator that was never written.
"""

from __future__ import annotations

from typing import NoReturn

from logos.check import BUILTINS, DIRECTIVES, CheckedModule, fn_shape, let_shape
from logos.diag import MISSING_RETURN, NOT_FOUND, TYPE_MISMATCH, UNKNOWN_NODE, diag
from logos.ir import NEWLINE, TRIPLE, IrBlock, IrDecl, IrFn, IrGlobal, IrModule
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.types import Prim, Ptr, Ty

#: Logos arithmetic to LLVM opcode. Every integer type divides as signed here;
#: the unsigned spellings arrive with the unsigned comparisons, in the task that
#: gives ``:u8``..``:u64`` their own narrowing rules.
_ARITH = {"+": "add", "-": "sub", "*": "mul", "/": "sdiv", "%": "srem"}

#: Statements the checker accepts that this backend has no lowering for yet.
_PENDING = ("set", "if", "while", "match", "break", "continue")

#: ``rt_write``'s C signature, spelled exactly: ``long (int, const void*, size_t)``.
#: The return value is ignored, but the declaration has to match or the call is
#: undefined behaviour at the ABI level.
_RT_WRITE = IrDecl("rt_write", (Prim("i32"), Ptr(Prim("u8")), Prim("i64")), Prim("i64"))

#: ``main`` returns a C exit status, whatever the Logos program says.
_ENTRY_RET = Prim("i32")

_STDOUT = "1"


def lower_module(cm: CheckedModule) -> IrModule:
    """Lower every function of ``cm``; a module with a ``main`` is a program.

    Tests are compiled out: they are grammar nodes for the harness, and a
    build must not carry their bodies into the binary.
    """
    lowering = _Lower(cm)
    for name, node in cm.module.fns.items():
        lowering.fn(name, node)
    return lowering.close()


def lower_test(cm: CheckedModule, test_name: str) -> IrModule:
    """Lower one ``(test ...)`` body into ``main``.

    The body's harness directives are skipped, because they are instructions to
    the runner and not expressions. Everything else is ordinary grammar.
    """
    node = cm.module.tests.get(test_name)
    if node is None:
        raise diag(
            NOT_FOUND,
            "",
            f"no test named '{test_name}' in module '{cm.module.name}'",
        )
    lowering = _Lower(cm)
    fn = IrFn("main", [], _ENTRY_RET, [IrBlock("entry")])
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

    def __init__(self, cm: CheckedModule) -> None:
        self.cm = cm
        self.ir = IrModule(TRIPLE, [], [])
        self.strings: dict[bytes, str] = {}
        self.declares: list[IrDecl] = []
        self.newline = False

    def intern(self, data: bytes) -> str:
        """The global holding ``data``, created on first use."""
        name = self.strings.get(data)
        if name is None:
            name = f".str{len(self.strings)}"
            self.strings[data] = name
            self.ir.globals.append(IrGlobal(name, data))
        return name

    def declare(self, decl: IrDecl) -> None:
        if decl not in self.declares:
            self.declares.append(decl)

    def fn(self, name: str, node: Node) -> None:
        ret, params, start = fn_shape(node, self.cm.types)
        is_main = name == "main"
        if is_main:
            if params:
                raise diag(TYPE_MISMATCH, node.path, "'main' takes no parameters")
            if not _is_void(ret):
                raise diag(
                    TYPE_MISMATCH, node.path, f"'main' must return :void, found {ret}"
                )
            fn = IrFn(name, [], _ENTRY_RET, [IrBlock("entry")])
        else:
            fn = IrFn(name, list(params), ret, [IrBlock("entry")])
        builder = _Fn(self, fn, path=node.path, ret=ret, is_main=is_main)
        builder.parameters(params)
        builder.body(list(node.items[start:]))
        builder.finish()
        self.ir.functions.append(fn)

    def close(self) -> IrModule:
        """Finish the module: the shared newline last, then the declarations."""
        if self.newline:
            self.ir.globals.append(IrGlobal(NEWLINE, b"\n"))
        self.ir.declares = list(self.declares)
        return self.ir


class _Fn:
    """One function's lowering: its block, its slots, its temporaries.

    ``ret`` is the type the Logos signature declares, which for ``main`` is
    ``:void`` even though the IR entry point returns ``i32``.
    """

    def __init__(
        self, lowering: _Lower, fn: IrFn, *, path: str, ret: Ty, is_main: bool
    ) -> None:
        self.lowering = lowering
        self.cm = lowering.cm
        self.fn = fn
        self.path = path
        self.ret = ret
        self.is_main = is_main
        self.block = fn.blocks[0]
        self.count = 0
        self.used: set[str] = set()
        self.vars: dict[str, tuple[Ty, str]] = {}

    # -- names and emission ---------------------------------------------

    def temp(self) -> str:
        while True:
            self.count += 1
            name = f"%t{self.count}"
            if name not in self.used:
                self.used.add(name)
                return name

    def emit(self, instr: tuple) -> None:
        self.block.instrs.append(instr)

    def parameters(self, params: list[tuple[str, Ty]]) -> None:
        for name, ty in params:
            slot = f"%{name}.addr"
            self.used.update((f"%{name}", slot))
            self.emit(("alloca", slot, ty))
            self.emit(("store", ty, f"%{name}", slot))
            self.vars[name] = (ty, slot)

    def finish(self) -> None:
        """Give the body its terminator, unless it ended in one already."""
        if self.block.term is not None:
            return
        if self.is_main:
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
        elif kind == "ret":
            self.ret_stmt(node)
        elif kind in _PENDING:
            self.pending(node)
        else:
            self.value(node, node.path)

    def bind(self, node: Node) -> None:
        name, declared, init = let_shape(node, self.cm.types)
        ty = declared if declared is not None else self.value_type(init, node.path)
        slot = f"%{name}.addr"
        self.used.add(slot)
        self.emit(("alloca", slot, ty))
        self.emit(("store", ty, self.value(init, node.path), slot))
        self.vars[name] = (ty, slot)

    def ret_stmt(self, node: Node) -> None:
        args = node.items[1:]
        if args:
            value = self.value(args[0], node.path)
        else:
            value = None
        if self.is_main:
            self.block.term = ("ret", _ENTRY_RET, "0")
        elif value is None:
            self.block.term = ("ret", Prim("void"), None)
        else:
            self.block.term = ("ret", self.ret, value)

    # -- values ----------------------------------------------------------

    def value(self, item: Node | Token, where: str) -> str:
        """Lower ``item`` to an operand: a local name or a literal constant."""
        if isinstance(item, Token):
            return self.literal(item, where)
        kind = item.kind
        if kind in _ARITH:
            return self.arith(item)
        if kind in _PENDING or kind in ("let", "letmut", "ret"):
            self.pending(item)
        if kind in self.cm.fn_sigs:
            return self.call(item)
        if kind in BUILTINS:
            return self.builtin(item)
        if kind == "":
            raise diag(UNKNOWN_NODE, item.path, "a form must start with a name")
        raise diag(
            UNKNOWN_NODE, item.path, f"'{kind}' is not lowered by this backend yet"
        )

    def literal(self, token: Token, where: str) -> str:
        if token.kind == NUMBER:
            if "." in token.value or "e" in token.value or "E" in token.value:
                raise diag(
                    UNKNOWN_NODE,
                    where,
                    f"float literal '{token.value}' is not lowered yet",
                )
            return token.value
        if token.kind == STRING:
            raise diag(
                UNKNOWN_NODE,
                where,
                "a :str value is not lowered yet; print takes a literal",
            )
        if token.kind == ATOM:
            if token.value in ("true", "false"):
                return token.value
            return self.load(token.value, where)
        raise diag(UNKNOWN_NODE, where, f"a {token.kind.lower()} is not a value")

    def load(self, name: str, where: str) -> str:
        binding = self.vars.get(name)
        if binding is None:
            raise diag(NOT_FOUND, where, f"unknown name '{name}'")
        ty, slot = binding
        dest = self.temp()
        self.emit(("load", dest, ty, slot))
        return dest

    def value_type(self, item: Node | Token, where: str) -> Ty:
        """The type of an unannotated initialiser.

        A node's type is the checker's. A literal's type is not recorded,
        because the checker types it where it stands: an integer literal is
        ``:i64`` and a string literal ``:str`` when nothing narrower expected
        it, and an annotated binding takes its declared type instead — which is
        why this is only asked when there is no annotation. A float literal
        never reaches the default, because :meth:`literal` rejects it.
        """
        if isinstance(item, Node):
            ty = self.cm.expr_types.get(item.path)
            if ty is None:
                raise diag(UNKNOWN_NODE, item.path, "the checker recorded no type here")
            return ty
        if item.kind == STRING:
            return Prim("str")
        if item.kind == ATOM and item.value in ("true", "false"):
            return Prim("bool")
        return Prim("i64")

    def arith(self, node: Node) -> str:
        ty = self.cm.expr_types.get(node.path)
        if ty is None:
            raise diag(UNKNOWN_NODE, node.path, "the checker recorded no type here")
        op = _ARITH[node.kind]
        args = node.items[1:]
        acc = self.value(args[0], node.path)
        for arg in args[1:]:
            right = self.value(arg, node.path)
            dest = self.temp()
            self.emit((op, dest, ty, acc, right))
            acc = dest
        return acc

    def call(self, node: Node) -> str:
        sig = self.cm.fn_sigs[node.kind]
        args = tuple(self.value(arg, node.path) for arg in node.items[1:])
        dest = "" if _is_void(sig.ret) else self.temp()
        self.emit(("call", dest, node.kind, args))
        return dest

    def builtin(self, node: Node) -> str:
        """``print`` and ``println``: one ``rt_write`` per byte range."""
        arg = node.items[1]
        if not (isinstance(arg, Token) and arg.kind == STRING):
            raise diag(
                UNKNOWN_NODE,
                _where(arg, node),
                "print takes a string literal until :str values are lowered",
            )
        data = arg.value.encode("utf-8")
        self.lowering.declare(_RT_WRITE)
        pointer = self.temp()
        self.emit(("gaddr", pointer, self.lowering.intern(data)))
        self.emit(("call", "", "rt_write", (_STDOUT, pointer, str(len(data)))))
        if node.kind == "println":
            self.lowering.newline = True
            newline = self.temp()
            self.emit(("gaddr", newline, NEWLINE))
            self.emit(("call", "", "rt_write", (_STDOUT, newline, "1")))
        return ""

    def pending(self, node: Node) -> NoReturn:
        raise diag(
            UNKNOWN_NODE,
            node.path,
            f"'{node.kind}' is not lowered by this backend yet",
        )


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _where(item: Node | Token, owner: Node) -> str:
    return item.path if isinstance(item, Node) and item.path else owner.path
