"""Lower a checked module to the IR.

Scope
-----
This is the first backend, so it lowers what the first binary needs: integer
literals, ``let``, ``ret``, calls, ``print`` and ``println`` over a string
literal, and checked integer division. A construct the checker accepts but this
pass does not lower yet is reported as a diagnostic at its own path — never a
``KeyError`` or a ``ValueError``, and never silently miscompiled. ``if``,
``while``, ``set``, ``match`` and the rest arrive with the tasks that own them.

Values and slots
----------------
Every binding lives in a named stack slot, and every read of it is an explicit
``load`` into a fresh temporary. That is deliberately unoptimised: clang's
``-O2`` promotes the slots away, and until it does the IR shows one line per
source-level operation. A parameter is stored into its slot on entry, so a
parameter and a ``let`` are read the same way.

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
by every use of the same bytes. ``println`` writes those bytes and then the one
shared newline global, never a ``printf`` format: the length comes from the
literal, not from a scan for a terminator that was never written. A panic
argument is the one exception: it is a NUL-terminated C string, because
``rt_panic`` reads it with ``strlen``.
"""

from __future__ import annotations

from typing import NoReturn

from logos.check import BUILTINS, DIRECTIVES, CheckedModule, fn_shape, let_shape
from logos.diag import (
    DIVISION_BY_ZERO,
    MISSING_RETURN,
    NOT_FOUND,
    TYPE_MISMATCH,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    diag,
)
from logos.ir import NEWLINE, TRIPLE, IrBlock, IrDecl, IrFn, IrGlobal, IrModule
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.types import INTEGERS, Prim, Ptr, Ty

#: Logos arithmetic to LLVM opcode. ``/`` and ``%`` are not here: they are
#: checked before they are emitted, and their opcode depends on signedness.
_ARITH = {"+": "add", "-": "sub", "*": "mul"}

#: The two operations that can trap.
_DIVIDE = ("/", "%")

#: ``INT_MIN`` for every signed width, the dividend of the one overflowing
#: division. ``INT_MIN / -1`` wraps to ``INT_MIN`` on x86-64; the hardware would
#: fault, so the lowerer never lets that pair reach an ``idiv``.
_INT_MIN = {
    "i8": -(2**7),
    "i16": -(2**15),
    "i32": -(2**31),
    "i64": -(2**63),
}

#: Statements the checker accepts that this backend has no lowering for yet.
_PENDING = ("set", "if", "while", "match", "break", "continue")

#: ``rt_write``'s C signature, spelled exactly: ``long (int, const void*, size_t)``.
#: The return value is ignored, but the declaration has to match or the call is
#: undefined behaviour at the ABI level.
_RT_WRITE = IrDecl("rt_write", (Prim("i32"), Ptr(Prim("u8")), Prim("i64")), Prim("i64"))

#: ``rt_panic``'s C signature: ``_Noreturn void (const char*, const char*, const char*)``.
#: All three arguments are NUL-terminated C strings the compiler emits itself.
_RT_PANIC = IrDecl(
    "rt_panic",
    (Ptr(Prim("u8")), Ptr(Prim("u8")), Ptr(Prim("u8"))),
    Prim("void"),
)

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

    The module's own functions are lowered too: a test body may call one, and a
    call with no definition is a diagnostic rather than a symbol the linker
    would fail to find. A module-level ``main`` is skipped, because the test
    body is the entry point and two definitions of ``@main`` cannot coexist.
    """
    node = cm.module.tests.get(test_name)
    if node is None:
        raise diag(
            NOT_FOUND,
            "",
            f"no test named '{test_name}' in module '{cm.module.name}'",
        )
    lowering = _Lower(cm)
    lowering.symbols.pop("main", None)
    for name, fn_node in cm.module.fns.items():
        if name == "main":
            continue
        lowering.fn(name, fn_node)
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

    def __init__(self, cm: CheckedModule) -> None:
        self.cm = cm
        self.ir = IrModule(TRIPLE, [], [])
        self.strings: dict[bytes, str] = {}
        self.declares: list[IrDecl] = []
        self.newline = False
        #: Every source function this module can call, mapped to its symbol.
        #: Built before any body is lowered, so source order is irrelevant and
        #: a call to a name that is not here is E0103 in the lowerer.
        self.symbols: dict[str, str] = {
            name: self.symbol(name) for name in cm.module.fns
        }

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

    def declare(self, decl: IrDecl) -> None:
        if decl not in self.declares:
            self.declares.append(decl)

    def fn(self, name: str, node: Node) -> None:
        ret, params, start = fn_shape(node, self.cm.types)
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
    """One function's lowering: its blocks, its slots, its temporaries.

    ``ret`` is the type the Logos signature declares, which for ``main`` is
    ``:void`` or ``:i64`` even though the IR entry point returns ``i32``.
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
        self.bindings = 0
        self.labels = 0
        self.vars: dict[str, tuple[Ty, str]] = {}

    # -- names and emission ---------------------------------------------

    def temp(self) -> str:
        """A fresh temporary. ``%t<n>`` cannot collide with ``%v.<n>``."""
        self.count += 1
        return f"%t{self.count}"

    def binding(self) -> str:
        """A fresh, IR-safe local for one source binding: ``%v.<n>``."""
        name = f"%v.{self.bindings}"
        self.bindings += 1
        return name

    def new_block(self, label: str) -> None:
        """Start a new block in this function and make it current."""
        self.block = IrBlock(label)
        self.fn.blocks.append(self.block)

    def emit(self, instr: tuple) -> None:
        self.block.instrs.append(instr)

    def parameters(self, params: list[tuple[str, Ty]]) -> None:
        for source, ty in params:
            safe = self.binding()
            slot = f"{safe}.addr"
            self.emit(("alloca", slot, ty))
            self.emit(("store", ty, safe, slot))
            self.vars[source] = (ty, slot)
            self.fn.params.append((safe.lstrip("%"), ty))

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
        slot = f"{self.binding()}.addr"
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
            self.block.term = ("ret", _ENTRY_RET, self.exit_status(value))
        elif value is None:
            self.block.term = ("ret", Prim("void"), None)
        else:
            self.block.term = ("ret", self.ret, value)

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

    # -- values ----------------------------------------------------------

    def value(self, item: Node | Token, where: str) -> str:
        """Lower ``item`` to an operand: a local name or a literal constant."""
        if isinstance(item, Token):
            return self.literal(item, where)
        kind = item.kind
        if kind in _ARITH or kind in _DIVIDE:
            return self.arith(item)
        if kind in _PENDING or kind in ("let", "letmut", "ret"):
            self.pending(item)
        # A module-level ``fn`` shadows a builtin: the checker resolves
        # ``env.fns`` before ``BUILTINS``, so the lowerer must agree or a user
        # ``print`` would check as their own function and lower as the builtin.
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
            # The checker has proved the value fits the resolved width; the
            # spelling is normalised so ``+5`` and ``007`` are LLVM's ``5`` and
            # ``7``.
            return str(int(token.value))
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
        args = node.items[1:]
        acc = self.value(args[0], node.path)
        for arg in args[1:]:
            right = self.value(arg, node.path)
            if node.kind in _DIVIDE:
                acc = self.divide(node.kind, ty, acc, right, node.path)
            else:
                dest = self.temp()
                self.emit((_ARITH[node.kind], dest, ty, acc, right))
                acc = dest
        return acc

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

    def pending(self, node: Node) -> NoReturn:
        raise diag(
            UNKNOWN_NODE,
            node.path,
            f"'{node.kind}' is not lowered by this backend yet",
        )


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _is_i64(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "i64"


def _where(item: Node | Token, owner: Node) -> str:
    return item.path if isinstance(item, Node) and item.path else owner.path
