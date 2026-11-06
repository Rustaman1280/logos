"""Compiler diagnostics: one code, one location, one sentence.

Every diagnostic code lives in ``CODES``. Passes import the code they report
instead of spelling a literal, so ``E0001`` cannot mean two different things in
two modules. ``E0001``-``E0006`` are the read-only passes (lexing, parsing,
resolution); ``E01xx`` is the type checker, ``E02xx`` struct fields, ``E03xx``
ownership, ``E04xx`` the string and vector operations and ``E05xx`` generics.

Runtime codes
-------------
A ``R0xxx`` code is not a compiler diagnostic: it is a stable name the C
runtime prints when a program panics at run time, so ``.expect`` tests can
quote it.

* ``R0001`` division by zero — emitted by the lowerer before every ``/`` and
  ``%`` that reaches a zero divisor.
* ``R0002`` double free — raised by the runtime's leak tracker when a block is
  handed back twice. The message is ``double free``; the path is the node that
  allocated the block, because a pointer that has already been released no
  longer names anything. It is exactly that case: a free of an address no
  ``alloc`` ever produced is ``R0004``, not a double free.
* ``R0003`` failed runtime check — emitted by the lowerer for a ``match`` on
  an integer whose arms have no ``_``: Task 11 makes that a static diagnostic
  (E0600) once exhaustiveness is checked, and until then the unmatched path
  panics instead of returning a fabricated value. The same code is emitted for
  an index outside ``0..n`` — of a fixed array, of a vector, or of a string
  slice — for an allocation count or a string length whose byte size would
  overflow, and for an allocation the allocator could not satisfy: all are
  runtime checks that cannot be proven at compile time, and all panic with the
  node path of the operation. It is reserved for checks of that kind (channel
  state and the rest).
* ``R0004`` free of a pointer that was never allocated — raised by the same
  tracker when ``rt_free`` is handed an address this process never allocated.
  The location is ``<runtime>``: no ``alloc`` node made the block, so there is
  no node path to name.

R0002 and R0004 are runtime names only: nothing in the compiler emits them.
A runtime check with no node behind it prints ``<runtime>`` in the location
field, so a panic line always starts with a location.
"""

from __future__ import annotations

#: Diagnostic codes and their meaning. Codes are stable: tests and ``.expect``
#: files quote them.
CODES = {
    "E0001": "unbalanced parentheses",
    "E0002": "unknown node",
    "E0003": "duplicate definition",
    "E0004": "circular import",
    "E0005": "name not found",
    "E0006": "lexical error",
    "E0100": "type mismatch",
    "E0101": "unknown type",
    "E0102": "wrong arity",
    "E0103": "unknown function",
    "E0104": "not mutable",
    "E0105": "missing return",
    "E0106": "integer literal out of range",
    "E0200": "unknown field",
    "E0201": "field type mismatch",
    "E0300": "use after move",
    "E0301": "free of a non-owning pointer",
    "E0302": "move out of an immutable binding",
    "E0400": "index type mismatch",
    "E0401": "string not a str",
    "E0402": "vector value not copyable",
    "E0500": "instantiation limit reached",
    "E0501": "unbound type variable",
}

UNBALANCED = "E0001"
UNKNOWN_NODE = "E0002"
DUPLICATE = "E0003"
CIRCULAR = "E0004"
NOT_FOUND = "E0005"
LEXICAL = "E0006"
TYPE_MISMATCH = "E0100"
UNKNOWN_TYPE = "E0101"
WRONG_ARITY = "E0102"
UNKNOWN_FUNCTION = "E0103"
NOT_MUTABLE = "E0104"
MISSING_RETURN = "E0105"
LITERAL_RANGE = "E0106"
UNKNOWN_FIELD = "E0200"
FIELD_TYPE_MISMATCH = "E0201"
USE_AFTER_MOVE = "E0300"
FREE_NON_OWN = "E0301"
MOVE_IMMUTABLE = "E0302"
INDEX_TYPE = "E0400"
NOT_A_STR = "E0401"
VEC_MOVE = "E0402"

#: More distinct instantiations of generic functions than the compiler will
#: monomorphise. 256 copies is the documented ceiling: a program that asks for
#: a 257th is refused rather than allowed to grow the binary without bound.
INSTANTIATION_LIMIT = "E0500"

#: A type variable that nothing binds: a call whose arguments fix no type for
#: it, or an annotation that names one the enclosing definition never declared.
UNBOUND_TYPE_VAR = "E0501"

#: The runtime panic code the lowerer emits for a division by zero. ``R0002``
#: double free stays a runtime-only name; the other code the lowerer emits is
#: ``MATCH_NO_ARM``.
DIVISION_BY_ZERO = "R0001"

#: The runtime check a ``match`` with no ``_`` arm reaches when no literal
#: matches. Task 11 turns this into the static E0600 non-exhaustive match.
MATCH_NO_ARM = "R0003"

#: The runtime check an array index outside ``0..n`` reaches. The same code as
#: an unmatched ``match``: both are bounds that only the running program knows.
ARRAY_BOUNDS = "R0003"

#: The runtime code for ``free`` of an address no ``alloc`` produced. Like
#: ``R0002`` it is a runtime name only; the compiler never emits it, and the
#: location it prints is ``<runtime>`` because no node allocated the block.
FREE_UNKNOWN = "R0004"


class LogosError(Exception):
    """A diagnostic carrying a code, a location and a message.

    ``path`` is the node path the diagnostic concerns, or the empty string when
    no node exists (lexing, unbalanced parentheses). In that case ``file``,
    ``line`` and ``col`` name the source position instead.
    """

    def __init__(
        self,
        code: str,
        path: str,
        message: str,
        *,
        file: str = "",
        line: int = 0,
        col: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    @property
    def location(self) -> str:
        """The node path, or ``file:line:column`` when no node exists."""
        if self.path:
            return self.path
        if self.file:
            return f"{self.file}:{self.line}:{self.col}"
        return ""

    def render(self) -> str:
        """Return the one-line form printed by the compiler."""
        location = self.location
        if location:
            return f"{self.code} {location}: {self.message}"
        return f"{self.code}: {self.message}"

    def __str__(self) -> str:
        return self.render()


def diag(
    code: str,
    path: str,
    message: str,
    *,
    file: str = "",
    line: int = 0,
    col: int = 0,
) -> LogosError:
    """Build a diagnostic. Raising the result is the caller's job."""
    return LogosError(code, path, message, file=file, line=line, col=col)
