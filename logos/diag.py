"""Compiler diagnostics: one code, one location, one sentence.

Every diagnostic code lives in ``CODES``. Passes import the code they report
instead of spelling a literal, so ``E0001`` cannot mean two different things in
two modules. ``E0001``-``E0006`` are the read-only passes (lexing, parsing,
resolution); ``E01xx`` is the type checker.

Runtime codes
-------------
A ``R0xxx`` code is not a compiler diagnostic: it is a stable name the C
runtime prints when a program panics at run time, so ``.expect`` tests can
quote it.

* ``R0001`` division by zero — emitted by the lowerer before every ``/`` and
  ``%`` that reaches a zero divisor.
* ``R0002`` double free — reserved for the owning allocator; raised by the
  runtime, not by the compiler.
* ``R0003`` failed runtime check — reserved for a runtime assertion (bounds,
  channel state) that cannot be proven at compile time.

R0002 and R0003 are reserved names only: nothing emits them yet.
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

#: The runtime panic code the lowerer emits for a division by zero. The other
#: runtime codes (``R0002`` double free, ``R0003`` failed runtime check) are
#: reserved in the module docstring and are raised by the runtime itself.
DIVISION_BY_ZERO = "R0001"


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
