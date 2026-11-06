"""Parse Logos source into a sexpr tree with node paths.

The parser knows no grammar. ``(`` opens a node, the head atom names its kind,
and ``)`` closes it; everything else is a ``Token``. Arity, node vocabulary and
types belong to the resolver and the checker, so a file with an unknown form
still parses and can still be printed.
"""

from __future__ import annotations

from logos.diag import UNBALANCED, diag
from logos.lexer import ATOM, LPAREN, RPAREN, Token, tokenize
from logos.nodes import FILE, Node, assign_paths


def parse(src: str, file: str) -> Node:
    """Parse ``src`` into a ``file`` node.

    Raise ``LogosError`` with code E0001. A diagnostic inside a form carries
    that form's path; one that happens before a node exists carries
    ``file:line:column``.
    """
    tokens = tokenize(src, file)
    forms: list[Node] = []
    stack: list[Node] = []

    for token in tokens:
        if token.kind == LPAREN:
            stack.append(Node("", (), "", token.line, token.col))
        elif token.kind == RPAREN:
            if not stack:
                raise diag(
                    UNBALANCED,
                    "",
                    "unbalanced ')'",
                    file=file,
                    line=token.line,
                    col=token.col,
                )
            node = stack.pop()
            node.kind = _kind(node)
            if stack:
                stack[-1].items += (node,)
            else:
                forms.append(node)
        elif not stack:
            raise diag(
                UNBALANCED,
                "",
                "form outside parentheses",
                file=file,
                line=token.line,
                col=token.col,
            )
        else:
            stack[-1].items += (token,)

    if stack:
        # Attach the still-open nodes to each other so the partial tree can be
        # given paths; the innermost one supplies the diagnostic's path.
        for node in stack:
            node.kind = _kind(node)
        for depth in range(len(stack) - 1, 0, -1):
            stack[depth - 1].items += (stack[depth],)
        partial = Node(FILE, tuple(forms) + (stack[0],), "", 1, 1)
        assign_paths(partial)
        unclosed = stack[-1]
        raise diag(
            UNBALANCED,
            unclosed.path,
            "unbalanced '('",
            file=file,
            line=unclosed.line,
            col=unclosed.col,
        )

    return assign_paths(Node(FILE, tuple(forms), "", 1, 1))


def _kind(node: Node) -> str:
    """Name a form after its head atom; a bare list has no kind."""
    if not node.items:
        return ""
    head = node.items[0]
    if isinstance(head, Token) and head.kind == ATOM:
        return head.value
    return ""
