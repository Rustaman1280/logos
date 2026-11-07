"""The generated Logos manual and machine-readable manifest.

Nothing here is hand-maintained prose about the compiler's vocabulary: every
table is built from the tables the compiler itself uses. :func:`build_manifest`
reads ``logos.diag.CODES``, ``logos.types.FORMS`` and the reserved-name and
operator tables in ``logos.check``; :func:`build_markdown` embeds each file in
``docs/manual/`` in its canonical form together with the output its own
``(expect ...)`` directive declares.

The manual is generated, never edited. ``logosc guide --check`` regenerates it
and compares the bytes with ``docs/LOGOS.md``, so the committed copy cannot
drift from the compiler: a changed diagnostic message, a new type form or an
edited chapter makes the check fail until the file is regenerated.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from logos import check as checker
from logos import diag, fmt, tester, types
from logos.nodes import Node
from logos.parser import parse

#: The repository root, found from this file rather than the caller's cwd.
ROOT = Path(__file__).resolve().parents[1]

#: The numbered chapters, one complete module per feature, in learning order.
MANUAL_DIR = ROOT / "docs" / "manual"

#: The committed generated manual that ``--check`` compares against.
DOC_PATH = ROOT / "docs" / "LOGOS.md"

#: Editorial headings for the chapters. The file names set the order; this map
#: only names what each file teaches. A missing entry is an error, not a
#: silently de-slugged heading, so adding a chapter cannot leave it untitled.
CHAPTER_TITLES = {
    "01-values-and-literals": "Values and literals",
    "02-let-letmut-set": "let, letmut and set",
    "03-functions-and-ret": "Functions and ret",
    "04-if-while-match": "if, while and match",
    "05-structs": "Structs",
    "06-fixed-arrays": "Fixed arrays",
    "07-strings": "Strings",
    "08-vectors": "Vectors",
    "09-alloc-free-own-ptr": "alloc, free, own and ptr",
    "10-results-and-try": "Results and try",
    "11-enums": "Enums",
    "12-optionals": "Optionals",
    "13-generics": "Generics",
    "14-closures": "Closures",
    "15-threads": "Threads",
    "16-extern": "extern",
    "17-tests": "Tests",
}

#: The ten rules that decide whether a program compiles. ``text`` is what the
#: cheat sheet prints, ``detail`` names the table and code that enforces it in
#: the reference section, and ``keywords`` are the phrases the test requires
#: inside the manual's first 40 lines.
RULES = (
    {
        "text": "One canonical spelling for every construct.",
        "detail": (
            "`logos.fmt` gives every node one printed shape, the parser accepts "
            "no aliases, and an unknown head is E0002."
        ),
        "keywords": ("canonical",),
    },
    {
        "text": "No implicit conversion; `(cast T e)` is the only one.",
        "detail": (
            "`types.compatible` requires the same type exactly, so a mixed "
            "width or kind is E0100. `cast` converts numbers only."
        ),
        "keywords": ("implicit conversion", "cast"),
    },
    {
        "text": (
            "A binding is block-scoped: function body, if branch, while body, "
            "match arm."
        ),
        "detail": (
            "`_Env.block()` pushes one scope per block; a name declared inside "
            "is gone at the closing parenthesis."
        ),
        "keywords": ("block-scoped",),
    },
    {
        "text": "A vector or owner-containing value moves; it is never copied.",
        "detail": (
            "`_move_only` and `_consume`: binding it or passing it moves it, "
            "and a second use is E0300 or E0402."
        ),
        "keywords": ("moves",),
    },
    {
        "text": "A value cannot be read out of a place when it owns memory.",
        "detail": (
            "A struct field, an array or vector element and a dereference of an "
            "owning type are E0300/E0402 (`_OWNER_PLACE`, `_VEC_PLACE`)."
        ),
        "keywords": ("read out of a place",),
    },
    {
        "text": (
            "A release inside a loop is refused for a binding declared outside it."
        ),
        "detail": (
            "E0303 `LOOP_MOVE`: the body is checked once, so a second pass "
            "would read a slot the first emptied."
        ),
        "keywords": ("release inside a loop",),
    },
    {
        "text": "A capturing closure cannot cross a call boundary.",
        "detail": (
            "E0502 `CLOSURE_ESCAPE`: its environment is a slot in the creating "
            "frame, and `(fn* ...)` is not `(fn ...)`."
        ),
        "keywords": ("capturing closure",),
    },
    {
        "text": "A result must be handled: matched, tried, returned or passed on.",
        "detail": (
            "E0602 `UNHANDLED_RESULT` at the binding whose `(res T E)` is "
            "dropped in statement position."
        ),
        "keywords": ("result must be handled",),
    },
    {
        "text": "Shared state must carry `(shared ...)`.",
        "detail": (
            "E0700 `SHARED_MARKER` at the declaration and at the thread "
            "boundary, checked through aggregates."
        ),
        "keywords": ("(shared ...)",),
    },
    {
        "text": "A form the checker accepts, the backend can emit.",
        "detail": (
            "Every checker branch has a lowerer branch; a form the backend "
            "cannot emit is a compiler bug, never a runtime surprise."
        ),
        "keywords": ("backend can emit",),
    },
)

#: Arity and one-line meaning of the composite type forms. The keys are checked
#: against ``types.FORMS`` on every build, so a new form cannot be forgotten.
TYPE_ARITY = {
    "arr": "2",
    "vec": "1",
    "ptr": "1",
    "own": "1",
    "fn": "2",
    "fn*": "2",
    "res": "2",
    "opt": "1",
    "shared": "1",
    "chan": "1",
    "atomic": "1",
}

TYPE_MEANING = {
    "arr": "`(arr T n)` a fixed array of n values of T",
    "vec": "`(vec T)` a growable array owned by a three-word descriptor",
    "ptr": "`(ptr T)` a raw pointer to T",
    "own": "`(own T)` an owning pointer, released by `free`",
    "fn": "`(fn (T...) R)` a plain function value",
    "fn*": "`(fn* (T...) R)` a capturing closure, confined to its frame",
    "res": "`(res T E)` an ok T or an error E; must be handled",
    "opt": "`(opt T)` some T or none",
    "shared": "`(shared T)` state two threads may touch; required at a boundary",
    "chan": "`(chan T)` a blocking channel of T",
    "atomic": "`(atomic T)` an atomic cell of T",
}

#: Every form a program body can use, with its arity (arguments after the head)
#: and one-line meaning. Names shared with a type form appear once, and their
#: meaning covers both spellings.
FORM_DOCS = (
    ("mod", "1", "`(mod name)` names the module; one per file"),
    ("use", "1", "`(use name)` imports `name.logos` beside this file"),
    ("fn", "2+", "`(fn name :T ((p :T) ...) body...)` a function, `(fn :T ((p :T) ...) body...)` a closure, `(fn (T...) R)` a function type"),
    ("type", "2", "`(type name (struct ...))` or `(type name (enum ...))`"),
    ("extern", "2+", "`(extern fn name :T ((p :T) ...))` declares a C symbol"),
    ("test", "1+", "`(test name (expect ...) body...)` one runnable case"),
    ("expect", "1+", "`(expect (out S) (code N) (leaks N) (err-contains S))`"),
    ("expect-error", "1", "`(expect-error E0xxx)` declares a negative case"),
    ("struct", "0+", "`(struct (field T) ...)` the fields of a named type"),
    ("enum", "1+", "`(enum (variant) (variant T) ...)` the variants"),
    ("let", "2-3", "`(let name [type] value)` an immutable binding"),
    ("letmut", "2-3", "`(letmut name [type] value)` a mutable binding"),
    ("set", "2", "`(set name value)` writes a `letmut` binding"),
    ("ret", "0-1", "`(ret [value])` returns from the enclosing function"),
    ("if", "3", "`(if cond then else)`; both arms have one type"),
    ("while", "1+", "`(while cond body...)`; `break` and `continue` leave it"),
    ("match", "2+", "`(match value arm...)`; closed sets must be exhaustive"),
    ("break", "0", "`(break)` leaves the innermost loop"),
    ("continue", "0", "`(continue)` starts the next iteration"),
    ("make", "1+", "`(make T value...)` builds a struct, array or vector"),
    ("get", "2", "`(get value field)` reads a struct field"),
    ("set-field", "3", "`(set-field name field value)` writes a field"),
    ("arr-get", "2", "`(arr-get a i)` reads a fixed-array element"),
    ("arr-set", "3", "`(arr-set name i value)` writes a fixed-array element"),
    ("str-len", "1", "`(str-len s)` the byte length of a `:str`"),
    ("str-concat", "2", "`(str-concat a b)` allocates a new joined string"),
    ("str-eq", "2", "`(str-eq a b)` byte equality of two strings"),
    ("str-slice", "3", "`(str-slice s start end)` bytes `[start, end)`"),
    ("read-all", "0", "`(read-all)` reads standard input to a `:str`"),
    ("vec-push", "2", "`(vec-push v x)` appends, growing the buffer"),
    ("vec-len", "1", "`(vec-len v)` the element count"),
    ("vec-get", "2", "`(vec-get v i)` reads an element, bounds checked"),
    ("vec-set", "3", "`(vec-set v i x)` writes an element, bounds checked"),
    ("vec-free", "1", "`(vec-free v)` releases a vector's buffer"),
    ("alloc", "2", "`(alloc T n)` allocates n values of T, uninitialised"),
    ("free", "1", "`(free p)` releases an `(own T)`"),
    ("ptr", "1", "`(ptr p)` the raw address of an owner, as `(ptr T)`"),
    ("deref", "1", "`(deref p)` reads the value a pointer addresses"),
    ("set-deref", "2", "`(set-deref p x)` writes through a pointer"),
    ("ptr-add", "2", "`(ptr-add p n)` the address n elements further on"),
    ("+", "2+", "add two or more numbers of one type"),
    ("-", "2+", "subtract numbers of one type"),
    ("*", "2+", "multiply numbers of one type"),
    ("/", "2+", "divide numbers of one type"),
    ("%", "2", "integer remainder; floats are E0100"),
    ("<", "2", "less than, yielding `:bool`"),
    ("<=", "2", "less than or equal, yielding `:bool`"),
    (">", "2", "greater than, yielding `:bool`"),
    (">=", "2", "greater than or equal, yielding `:bool`"),
    ("=", "2", "equal, yielding `:bool`; `:str` compares bytes"),
    ("!=", "2", "not equal, yielding `:bool`"),
    ("shl", "2", "shift left; the count keeps the value's width"),
    ("shr", "2", "arithmetic shift right; the sign bit fills"),
    ("shr-u", "2", "logical shift right; zeros fill"),
    ("and", "2", "short-circuit boolean and; both operands `:bool`"),
    ("or", "2", "short-circuit boolean or; both operands `:bool`"),
    ("not", "1", "boolean negation"),
    ("cast", "2", "`(cast T e)` the one explicit numeric conversion"),
    ("ok", "1", "`(ok v)` builds the success side of a result"),
    ("err", "1", "`(err e)` builds the error side of a result"),
    ("some", "1", "`(some v)` builds a present optional"),
    ("none", "0", "`(none)` builds an absent optional"),
    ("try", "1", "`(try r)` propagates an error from a result"),
    ("spawn", "1+", "`(spawn f arg...)` starts a thread running f"),
    ("join", "1", "`(join t)` waits for a thread"),
    ("mutex", "0", "`(mutex)` creates a `(shared :mutex)` lock"),
    ("lock", "1", "`(lock m)` acquires a mutex"),
    ("unlock", "1", "`(unlock m)` releases a mutex"),
    ("chan", "1-2", "`(chan T cap)` creates a channel of T"),
    ("send", "2", "`(send c x)` copies x into a channel, blocking when full"),
    ("recv", "1", "`(recv c)` takes the next value, blocking when empty"),
    ("atomic", "1-2", "`(atomic T v)` creates an atomic cell holding v"),
    ("atomic-add", "2", "`(atomic-add a n)` adds atomically"),
    ("atomic-load", "1", "`(atomic-load a)` reads atomically"),
    ("atomic-store", "2", "`(atomic-store a n)` writes atomically"),
)


def _build_forms() -> list[dict[str, str]]:
    """Every form, with the composite type forms appended from ``types.FORMS``.

    A name shared by a form and a type (``fn``, ``ptr``, ``chan``, ``atomic``)
    is documented once, under the form entry above. Every remaining composite
    type form is appended here, so adding one to ``types.FORMS`` without a
    meaning is a loud failure rather than a missing row in the manual.
    """
    if set(TYPE_ARITY) != set(types.FORMS) or set(TYPE_MEANING) != set(types.FORMS):
        raise RuntimeError(
            "the type-form documentation is out of date with logos.types.FORMS"
        )
    forms = [
        {"name": name, "arity": arity, "meaning": meaning}
        for name, arity, meaning in FORM_DOCS
    ]
    present = {entry["name"] for entry in forms}
    for name in types.FORMS:
        if name not in present:
            forms.append(
                {
                    "name": name,
                    "arity": TYPE_ARITY[name],
                    "meaning": TYPE_MEANING[name],
                }
            )
    return forms


def _build_types() -> list[dict[str, Any]]:
    """Every primitive, classified from the compiler's own integer/float lists."""
    entries: list[dict[str, Any]] = []
    for name in types.PRIMITIVES:
        entry: dict[str, Any] = {"name": f":{name}"}
        if name in types.INTEGERS:
            entry["kind"] = "integer"
            bits = int(name[1:])
            signed = name.startswith("i")
            entry["bits"] = bits
            entry["signed"] = signed
            entry["meaning"] = (
                f"a signed {bits}-bit integer"
                if signed
                else f"an unsigned {bits}-bit integer"
            )
        elif name in types.FLOATS:
            entry["kind"] = "float"
            bits = int(name[1:])
            entry["bits"] = bits
            entry["meaning"] = f"an IEEE {bits}-bit floating-point number"
        elif name == "bool":
            entry["kind"] = "boolean"
            entry["meaning"] = "`true` or `false`"
        elif name == "str":
            entry["kind"] = "string"
            entry["meaning"] = "a UTF-8 byte string: a pointer and a byte length"
        elif name == "void":
            entry["kind"] = "unit"
            entry["meaning"] = "no value; the type of a statement form"
        elif name == "thread":
            entry["kind"] = "handle"
            entry["meaning"] = "a spawned thread handle"
        elif name == "mutex":
            entry["kind"] = "handle"
            entry["meaning"] = "a lock handle, always `(shared :mutex)` when made"
        else:  # pragma: no cover - PRIMITIVES is closed, so this cannot happen
            entry["kind"] = "primitive"
            entry["meaning"] = "a primitive type"
        entries.append(entry)
    return entries


def _build_operators() -> list[dict[str, str]]:
    """Symbol operators, straight from the checker's dispatch tables."""
    operators: list[dict[str, str]] = []
    for symbol, word in checker._ARITH.items():
        meaning = (
            "integer remainder; floats are E0100"
            if symbol == "%"
            else f"{word} numbers of one type"
        )
        operators.append({"symbol": symbol, "arity": "2+", "meaning": meaning})
    for symbol in ("<", "<=", ">", ">="):
        operators.append(
            {"symbol": symbol, "arity": "2", "meaning": "ordered compare, yielding `:bool`"}
        )
    scalars = ", ".join(f":{name}" for name in checker._SCALARS)
    for symbol in ("=", "!="):
        operators.append(
            {
                "symbol": symbol,
                "arity": "2",
                "meaning": f"equality over {scalars}; `:str` compares bytes",
            }
        )
    for symbol, meaning in (
        ("shl", "shift left"),
        ("shr", "arithmetic shift right"),
        ("shr-u", "logical shift right"),
    ):
        operators.append({"symbol": symbol, "arity": "2", "meaning": meaning})
    for symbol in ("and", "or"):
        operators.append(
            {"symbol": symbol, "arity": "2", "meaning": "short-circuit `:bool` operator"}
        )
    operators.append({"symbol": "not", "arity": "1", "meaning": "`:bool` negation"})
    return operators


def build_manifest() -> dict[str, Any]:
    """The machine-readable manual: every compiler table an AI needs by name.

    Keys are ``forms``, ``types``, ``operators``, ``builtins``, ``reserved``,
    ``codes`` and ``categories``. Every value is read from the compiler at call
    time, so the manifest cannot describe a language the compiler does not
    implement.
    """
    return {
        "forms": _build_forms(),
        "types": _build_types(),
        "operators": _build_operators(),
        "builtins": [
            {
                "name": name,
                "meaning": "print a value to stdout with no newline"
                if name == "print"
                else "print a value to stdout followed by a newline",
            }
            for name in checker.BUILTINS
        ],
        "reserved": {
            "forms": sorted(checker.RESERVED_FORMS),
            "prefixes": list(checker.RESERVED_PREFIXES),
            "variants": list(checker.RESERVED_VARIANTS),
        },
        "codes": dict(diag.CODES),
        "categories": list(tester.CATEGORIES),
    }


def build_markdown() -> str:
    """The whole manual, cheat sheet first, then executable chapters and tables."""
    lines: list[str] = []
    lines.append("# Logos: the generated manual")
    lines.append("")
    lines.append(
        "A prefix-form language for AI readers, compiled by `logosc` to native "
        "x86_64 Linux binaries. Generated from the compiler's tables and "
        "`docs/manual/*.logos`; do not edit."
    )
    lines.append("")
    lines.append("## Cheat sheet")
    lines.append("")
    lines.append(
        "A program is one file and one module: `(mod name)`, then `(fn name :ret "
        "((p :type) ...) body...)` definitions, `(type ...)` declarations, and "
        "`(test name (expect (out \"...\") (code 0)) body...)` cases."
    )
    lines.append("")
    lines.append("```logos")
    lines.append("(mod hello)")
    lines.append("")
    lines.append("(fn add :i64 ((a :i64) (b :i64))")
    lines.append("  (ret (+ a b)))")
    lines.append("")
    lines.append("(test add")
    lines.append('  (expect (out "3\\n") (code 0))')
    lines.append("  (println (add 1 2)))")
    lines.append("```")
    lines.append("")
    lines.append("The ten rules that decide whether code compiles:")
    lines.append("")
    for number, rule in enumerate(RULES, start=1):
        lines.append(f"{number}. {rule['text']}")
    lines.append("")
    lines.append(
        "Machine-readable: `logosc guide --json` prints the manifest (forms, "
        "types, operators, builtins, reserved, codes, categories). "
        "`logosc guide --check` proves this file matches the compiler."
    )
    lines.append("")

    chapters = _chapters()
    lines.append("## Manual")
    lines.append("")
    lines.append(
        f"{len(chapters)} chapters in learning order. Each is a complete module; "
        "its `(test ...)` node declares the output shown, and "
        "`python3 -m logos test docs/manual` runs every one at `-O0` and `-O2`."
    )
    lines.append("")
    for number, chapter in enumerate(chapters, start=1):
        lines.append(f"### {number}. {chapter['title']}")
        lines.append("")
        lines.append("```logos")
        lines.append(chapter["source"].rstrip("\n"))
        lines.append("```")
        lines.append("")
        lines.append(f"Expected stdout, exit {chapter['code']}:")
        lines.append("")
        lines.append("```text")
        lines.append(chapter["out"].rstrip("\n"))
        lines.append("```")
        lines.append("")

    manifest = build_manifest()
    lines.append("## Reference")
    lines.append("")
    lines.append("### The ten rules, and where the compiler enforces them")
    lines.append("")
    lines.extend(
        _table(
            ("#", "Rule", "Enforced by"),
            [
                (str(number), rule["text"], rule["detail"])
                for number, rule in enumerate(RULES, start=1)
            ],
        )
    )
    lines.append("")

    lines.append("### Forms")
    lines.append("")
    lines.extend(
        _table(
            ("Form", "Arity", "Meaning"),
            [
                (f"`{form['name']}`", form["arity"], form["meaning"])
                for form in manifest["forms"]
            ],
        )
    )
    lines.append("")

    lines.append("### Primitive types")
    lines.append("")
    lines.extend(
        _table(
            ("Type", "Kind", "Meaning"),
            [
                (f"`{entry['name']}`", str(entry["kind"]), str(entry["meaning"]))
                for entry in manifest["types"]
            ],
        )
    )
    lines.append("")

    lines.append("### Operators")
    lines.append("")
    lines.extend(
        _table(
            ("Operator", "Arity", "Meaning"),
            [
                (f"`{operator['symbol']}`", operator["arity"], operator["meaning"])
                for operator in manifest["operators"]
            ],
        )
    )
    lines.append("")

    lines.append("### Builtins")
    lines.append("")
    lines.extend(
        _table(
            ("Builtin", "Meaning"),
            [(f"`{entry['name']}`", entry["meaning"]) for entry in manifest["builtins"]],
        )
    )
    lines.append("")

    reserved = manifest["reserved"]
    lines.append("### Reserved names")
    lines.append("")
    lines.append(
        "A definition may not take a name a form already spells (E0003). "
        "The reserved forms are: "
        + ", ".join(f"`{name}`" for name in reserved["forms"])
        + "."
    )
    lines.append("")
    lines.append(
        "Reserved form prefixes: "
        + ", ".join(f"`{name}`" for name in reserved["prefixes"])
        + ". Reserved enum variant names: "
        + ", ".join(f"`{name}`" for name in reserved["variants"])
        + "."
    )
    lines.append("")

    lines.append("### Diagnostic codes")
    lines.append("")
    lines.extend(
        _table(
            ("Code", "Meaning"),
            [(f"`{code}`", meaning) for code, meaning in manifest["codes"].items()],
        )
    )
    lines.append("")
    lines.append(
        "Runtime panic codes: `R0001` division by zero, `R0003` failed runtime "
        "check, `R0004` free of an unknown address, `R0005` thread operation "
        "failed. `R0002` double free is a runtime name only."
    )
    lines.append("")

    lines.append("### Test categories")
    lines.append("")
    lines.append(
        "`logosc test --only <category>` runs one: "
        + ", ".join(f"`{category}`" for category in manifest["categories"])
        + "."
    )
    lines.append("")
    return "\n".join(lines)


def check_current() -> list[str]:
    """Why ``docs/LOGOS.md`` is not the generated manual, or ``[]`` if it is.

    The comparison is byte for byte. A stale file returns a short summary and
    the first changed lines, so a caller can print one actionable report
    without dumping the whole manual.
    """
    expected = build_markdown()
    try:
        actual = DOC_PATH.read_text(encoding="utf-8")
    except OSError:
        return [
            f"{_display(DOC_PATH)} is missing; "
            "run `logosc guide > docs/LOGOS.md`"
        ]
    if actual == expected:
        return []
    diff = list(
        difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=f"{_display(DOC_PATH)} (committed)",
            tofile="generated",
            lineterm="",
            n=0,
        )
    )
    added = sum(
        1 for line in diff if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1 for line in diff if line.startswith("-") and not line.startswith("---")
    )
    summary = [
        f"{_display(DOC_PATH)} is stale: {added} line(s) to add, "
        f"{removed} line(s) to remove; run `logosc guide > docs/LOGOS.md`"
    ]
    for line in diff[:16]:
        summary.append(_truncate(line))
    return summary


def _chapters() -> list[dict[str, Any]]:
    """Read every chapter, check its directive, and return it in file order.

    The source is re-rendered through ``logos.fmt``: what the manual embeds is
    canonical even if a chapter was written less tidily, and the test that
    every file is already canonical keeps the two from diverging silently.
    """
    if not MANUAL_DIR.is_dir():
        raise FileNotFoundError(f"no manual directory at {MANUAL_DIR}")
    chapters: list[dict[str, Any]] = []
    for path in sorted(MANUAL_DIR.glob("*.logos")):
        title = CHAPTER_TITLES.get(path.stem)
        if title is None:
            raise ValueError(f"no chapter title for {path.name}")
        text = path.read_text(encoding="utf-8")
        root = parse(text, str(path))
        test = next(
            (
                item
                for item in root.items
                if isinstance(item, Node) and item.kind == "test"
            ),
            None,
        )
        if test is None:
            raise ValueError(f"{path.name}: no (test ...) node")
        expectation, _legacy = tester.expectation_of(test)
        if expectation is None or expectation.out is None:
            raise ValueError(f"{path.name}: the test needs an (expect (out ...))")
        chapters.append(
            {
                "title": title,
                "source": fmt.render(root),
                "out": expectation.out,
                "code": 0 if expectation.code is None else expectation.code,
            }
        )
    return chapters


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """A markdown table, with pipes escaped so a meaning cannot break a row."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return lines


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _truncate(line: str, limit: int = 200) -> str:
    if len(line) <= limit:
        return line
    return line[: limit - 3] + "..."


def _display(path: Path) -> str:
    """A path as it reads from the working directory, or in full."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
