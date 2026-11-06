"""Lower a checked module to the IR.

Scope
-----
This pass lowers what the check accepts: integer literals, ``let``/``letmut``
and ``set``, ``ret``, calls, ``if``, ``while``, ``match``, ``break``/
``continue``, comparisons, ``and``/``or``/``not``, ``print``/``println`` over
any ``:str`` value, and checked integer division. A construct the checker
accepts but this pass does not lower yet is reported as a diagnostic at its own
path — never a ``KeyError`` or a ``ValueError``, and never silently
miscompiled.

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

A declared type with no LLVM layout — ``(vec T)``, ``(arr T n)``, ``(own T)``,
``(res T E)`` and the rest — is E0002 at the node that materialises it, never a
``ValueError`` from the printer: the type vocabulary is larger than this
backend, and the refusal must carry a path.

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
coming apart. ``print``/``println`` extract both fields and hand them to one
``rt_write``, never a ``printf`` format: the length is the value's own, not a
scan for a terminator that was never written. A panic argument is the one
exception: it is a NUL-terminated C string, because ``rt_panic`` reads it with
``strlen``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from logos import llvm
from logos.check import (
    BUILTINS,
    DIRECTIVES,
    CheckedModule,
    fn_param_nodes,
    fn_shape,
    is_wildcard,
    let_shape,
    match_shape,
)
from logos.diag import (
    DIVISION_BY_ZERO,
    MATCH_NO_ARM,
    MISSING_RETURN,
    NOT_FOUND,
    TYPE_MISMATCH,
    UNKNOWN_FUNCTION,
    UNKNOWN_NODE,
    WRONG_ARITY,
    diag,
)
from logos.ir import NEWLINE, TRIPLE, IrBlock, IrDecl, IrFn, IrGlobal, IrModule
from logos.lexer import ATOM, NUMBER, STRING, Token
from logos.nodes import Node
from logos.types import INTEGERS, Prim, Ptr, Ty

#: Logos arithmetic to LLVM opcode. ``/`` and ``%`` are not here: they are
#: checked before they are emitted, and their opcode depends on signedness.
_ARITH = {"+": "add", "-": "sub", "*": "mul"}

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
_STATEMENTS = ("let", "letmut", "set", "ret", "while", "break", "continue")

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


def lowered_type(ty: Ty, path: str) -> Ty:
    """Return ``ty`` if the printer has a layout for it, else raise E0002.

    The checker's type vocabulary is larger than this backend's: a parameter,
    binding or result may name ``(vec :i64)``, ``(arr :i64 4)``, ``(own T)`` or
    ``(res T E)`` and still type-check. Materialising one would reach
    :func:`logos.llvm.type_text` and escape as a bare ``ValueError`` with no
    node path, so every place that materialises a type passes through here and
    the refusal is a diagnostic at the node that named it.
    """
    try:
        llvm.type_text(ty)
    except ValueError:
        raise diag(UNKNOWN_NODE, path, f"{ty} is not lowered yet") from None
    return ty


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
        # The signature is the first place a declared type is materialised:
        # refusing it here keeps the function's ``define`` line from reaching
        # the printer with a type it cannot spell.
        lowered_type(ret, node.path)
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
        lowered_type(ty, path)
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
        elif kind == "ret":
            self.ret_stmt(node)
        elif kind == "while":
            self.while_stmt(node)
        elif kind in ("break", "continue"):
            self.jump(node)
        else:
            self.value(node, node.path)

    def bind(self, node: Node) -> None:
        name, declared, init = let_shape(node, self.cm.types)
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
        if kind in _STATEMENTS:
            raise diag(UNKNOWN_NODE, item.path, f"'{kind}' is a statement, not a value")
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
            return self.string_value(token.value.encode("utf-8"))
        if token.kind == ATOM:
            if token.value in ("true", "false"):
                return token.value
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
        """The type the checker recorded for ``node``."""
        ty = self.cm.expr_types.get(node.path)
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
        checker's default, ``:i64``, the answer.
        """
        for item in items:
            if isinstance(item, Node):
                return self.type_of(item)
            if isinstance(item, Token) and item.kind == ATOM:
                binding = self.vars.get(item.value)
                if binding is not None:
                    return binding[0]
                if item.value in ("true", "false"):
                    return Prim("bool")
        return Prim("i64")

    def arith(self, node: Node) -> str:
        ty = self.type_of(node)
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

    def compare(self, node: Node) -> str:
        """``(op a b)``: one ``icmp`` whose predicate follows the operand width.

        The operand type is the checker's, so ``(< 1 a)`` with ``a :u8`` is an
        ``i8`` comparison of ``1`` and ``a``; an unsigned width swaps the
        signed predicate for the unsigned one. ``:bool`` is comparable with
        ``=`` and ``!=`` only, which the checker has already enforced.
        """
        items = node.items
        if len(items) != 3:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes two operands")
        ty = self.operand_type(items[1:])
        if not (isinstance(ty, Prim) and ty.name in INTEGERS + ("bool",)):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'{node.kind}' on {ty} is not lowered by this backend yet",
            )
        predicate = _COMPARE[node.kind]
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
        block comparing it with the arm's literal and branching to that arm's
        body or to the next test; a ``_`` arm branches straight to its body.
        Without a ``_`` arm the chain ends on ``rt_panic``: the checker cannot
        yet prove an integer match exhaustive, and inventing a value or falling
        into undefined behaviour would both be worse than the panic.
        """
        scrutinee, arms = match_shape(node)
        ty = self.type_of(node)
        value = self.value(scrutinee, node.path)
        tested = self.type_of_item(scrutinee, node.path)
        if not (isinstance(tested, Prim) and tested.name in INTEGERS):
            raise diag(
                UNKNOWN_NODE,
                node.path,
                f"'match' on {tested} is not lowered by this backend yet",
            )
        self.labels += 1
        tag = f"match{self.labels}"
        join, none = f"{tag}.join", f"{tag}.none"
        slot = None if _is_void(ty) else self.slot(ty, path=node.path)
        wildcard = any(is_wildcard(pattern) for pattern, _ in arms)
        fell = False
        self.block.term = ("br", f"{tag}.test0")
        for index, (pattern, _) in enumerate(arms):
            self.new_block(f"{tag}.test{index}")
            if is_wildcard(pattern):
                self.block.term = ("br", f"{tag}.body{index}")
                continue
            if not isinstance(pattern, Token) or pattern.kind != NUMBER:
                # ``match_shape`` refuses anything else, so this keeps the
                # lowerer's own reading total: no ``AttributeError`` from a
                # list pattern and no ``ValueError`` from ``int``.
                raise diag(
                    UNKNOWN_NODE,
                    node.path,
                    "a match pattern is an integer literal or '_'",
                )
            following = none if index + 1 == len(arms) else f"{tag}.test{index + 1}"
            test = self.temp()
            self.emit(("icmp", test, "eq", tested, value, str(int(pattern.value))))
            self.block.term = ("cbr", test, f"{tag}.body{index}", following)
        for index, (_, body) in enumerate(arms):
            self.new_block(f"{tag}.body{index}")
            with self.scope():
                arm = self.arm_value(body, node.path, statement=slot is None)
            if slot is not None and arm:
                self.emit(("store", ty, arm, slot))
            if self.block.term is None:
                self.block.term = ("br", join)
                fell = True
        if not wildcard:
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
        """``print``/``println``: one ``rt_write`` per byte range.

        The argument is any lowered ``:str`` — a literal, a binding, a call's
        result or a ``match`` — so a program prints what it computed and not
        only what it spelled. A literal skips the aggregate: its bytes and its
        length are known here, and building a value only to take it apart again
        would be work with no observable effect.
        """
        items = node.items
        if len(items) != 2:
            raise diag(WRONG_ARITY, node.path, f"'{node.kind}' takes one :str")
        arg = items[1]
        ty = self.type_of_item(arg, node.path)
        if not (isinstance(ty, Prim) and ty.name == "str"):
            raise diag(
                UNKNOWN_NODE, _where(arg, node), f"'{node.kind}' takes a :str, found {ty}"
            )
        self.lowering.declare(_RT_WRITE)
        if isinstance(arg, Token) and arg.kind == STRING:
            data = arg.value.encode("utf-8")
            pointer = self.temp()
            self.emit(("gaddr", pointer, self.lowering.intern(data)))
            length = str(len(data))
        else:
            whole = self.value(arg, node.path)
            pointer = self.temp()
            self.emit(("extractvalue", pointer, _STR, _PTR_U8, whole, 0))
            length = self.temp()
            self.emit(("extractvalue", length, _STR, Prim("i64"), whole, 1))
        self.emit(("call", "", "rt_write", (_STDOUT, pointer, length)))
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


def _is_void(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "void"


def _is_true(item: Node | Token) -> bool:
    """Whether a loop condition is the literal ``true``, which never exits.

    A ``Node`` or any other name is not constant: only the exact atom ``true``
    lets the head skip its exit edge. ``(while flag …)`` with ``flag :bool``
    keeps both edges, even if the flag happens to be true.
    """
    return isinstance(item, Token) and item.kind == ATOM and item.value == "true"


def _is_i64(ty: Ty) -> bool:
    return isinstance(ty, Prim) and ty.name == "i64"


def _where(item: Node | Token, owner: Node) -> str:
    return item.path if isinstance(item, Node) and item.path else owner.path
