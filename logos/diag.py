"""Compiler diagnostics: one code, one location, one sentence.

Every diagnostic code lives in ``CODES``. Passes import the code they report
instead of spelling a literal, so ``E0001`` cannot mean two different things in
two modules. Task 3 extends the table with the resolver's codes.
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
}

UNBALANCED = "E0001"
UNKNOWN_NODE = "E0002"
DUPLICATE = "E0003"
CIRCULAR = "E0004"
NOT_FOUND = "E0005"
LEXICAL = "E0006"


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
