"""Compiler diagnostics: one code, one node path, one sentence.

Task 3 grows this module with the resolver's error codes. The shape is fixed
here because the parser already has to report E0001.
"""

from __future__ import annotations


class LogosError(Exception):
    """A diagnostic carrying a code, the path of the node it concerns, and a message."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def render(self) -> str:
        """Return the one-line form printed by the compiler."""
        if self.path:
            return f"{self.code} {self.path}: {self.message}"
        return f"{self.code}: {self.message}"

    def __str__(self) -> str:
        return self.render()


def diag(code: str, path: str, message: str) -> LogosError:
    """Build a diagnostic. Raising the result is the caller's job."""
    return LogosError(code, path, message)
