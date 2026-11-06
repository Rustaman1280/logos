"""Lexer for Logos source.

The lexer is the only part of the front end that knows about characters. It
emits five token kinds and no newlines or comments: Logos has one construct per
node name and no comments, so whitespace carries no meaning.

Token positions are one-based. ``col`` counts characters, so a tab is one
column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from logos.diag import diag

LPAREN = "LPAREN"
RPAREN = "RPAREN"
ATOM = "ATOM"
STRING = "STRING"
NUMBER = "NUMBER"

#: Unbalanced parentheses are the only diagnostic code this pass owns. Lexical
#: errors reuse it until Task 3 fixes the full code table.
MALFORMED = "E0001"

_NUMBER = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z")

#: The only escapes a Logos string may contain. Canonical printing re-emits
#: exactly these spellings, so a decoded string always round-trips.
ESCAPES = {
    "\\": "\\",
    '"': '"',
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
}

_DELIMITERS = "()\""


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical token. ``STRING`` values hold the decoded text."""

    kind: str
    value: str
    line: int
    col: int


def tokenize(src: str, file: str) -> list[Token]:
    """Split ``src`` into tokens, or raise ``LogosError`` with code E0001.

    ``file`` names the source in lexical diagnostics, which happen before any
    node exists to own a path.
    """
    return _Lexer(src, file).run()


class _Lexer:
    def __init__(self, src: str, file: str) -> None:
        self.src = src
        self.file = file
        self.index = 0
        self.line = 1
        self.col = 1

    def run(self) -> list[Token]:
        tokens: list[Token] = []
        while self.index < len(self.src):
            ch = self.src[self.index]
            if ch == "\n":
                self.index += 1
                self.line += 1
                self.col = 1
            elif ch.isspace():
                self.index += 1
                self.col += 1
            elif ch == "(":
                tokens.append(Token(LPAREN, ch, self.line, self.col))
                self._skip(1)
            elif ch == ")":
                tokens.append(Token(RPAREN, ch, self.line, self.col))
                self._skip(1)
            elif ch == '"':
                tokens.append(self._string())
            else:
                tokens.append(self._atom())
        return tokens

    def _skip(self, count: int) -> None:
        self.index += count
        self.col += count

    def _string(self) -> Token:
        line, col = self.line, self.col
        self._skip(1)
        chars: list[str] = []
        while self.index < len(self.src):
            ch = self.src[self.index]
            if ch == '"':
                self._skip(1)
                return Token(STRING, "".join(chars), line, col)
            if ch == "\\":
                if self.index + 1 >= len(self.src):
                    break
                escape = self.src[self.index + 1]
                if escape not in ESCAPES:
                    raise diag(
                        MALFORMED,
                        self.file,
                        f"line {self.line}, column {self.col}: unknown escape \\{escape}",
                    )
                chars.append(ESCAPES[escape])
                self._skip(2)
            elif ch == "\n":
                chars.append(ch)
                self.index += 1
                self.line += 1
                self.col = 1
            else:
                chars.append(ch)
                self._skip(1)
        raise diag(
            MALFORMED,
            self.file,
            f"line {line}, column {col}: unterminated string",
        )

    def _atom(self) -> Token:
        line, col = self.line, self.col
        start = self.index
        while self.index < len(self.src):
            ch = self.src[self.index]
            if ch.isspace() or ch in _DELIMITERS:
                break
            self.index += 1
            self.col += 1
        value = self.src[start : self.index]
        kind = NUMBER if _NUMBER.match(value) else ATOM
        return Token(kind, value, line, col)
