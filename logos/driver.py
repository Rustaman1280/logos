"""Build Logos programs: front end, IR, clang.

The driver is the only part of the compiler that writes into ``build/`` and the
only part that runs another process. It owns two paths, and both are anchored
to the repository rather than to the caller's working directory, so a build
works the same from the root of the tree, from a test, or from anywhere else:

* the IR of a source file, ``build/<name>.ll``, kept on disk because it is the
  artefact a person reads when a program misbehaves;
* the C runtime, ``runtime/rt.c``, linked into every binary.

The link is one clang invocation::

    clang {opt} -target x86_64-unknown-linux-gnu build/<name>.ll runtime/rt.c
          -o <out> -lpthread -lm

clang compiles the IR text and ``rt.c``, then links both with libc. There are no
LLVM bindings, no ``llc`` and no ``lld`` in this pipeline; the IR text is the
whole interface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from logos import llvm, lower
from logos.check import check_module
from logos.ir import TRIPLE, IrModule
from logos.nodes import Node
from logos.parser import parse
from logos.resolve import Module, resolve

#: The C compiler that turns IR text plus the runtime into an ELF.
CLANG = "clang"

#: The repository root, found from this file rather than from the caller's cwd.
ROOT = Path(__file__).resolve().parents[1]

#: Where the emitted IR is kept.
BUILD_DIR = ROOT / "build"

#: The C runtime, linked into every Logos binary.
RUNTIME_C = ROOT / "runtime" / "rt.c"

#: Extra link inputs. Neither is used yet; both are unconditional so a program
#: that starts a thread does not need a different build command.
LINK_FLAGS = ("-lpthread", "-lm")


class BuildError(Exception):
    """A program the front end accepted could not become a binary."""


def load(src_path: str) -> Module:
    """Parse and resolve ``src_path``, loading imports from its own directory.

    An import named ``math`` is answered by ``math.logos`` beside the file that
    asked for it. That is also where the file-name-equals-module-name rule
    lives, because it is a property of the file system, not of the grammar.
    """
    path = Path(src_path)
    root = parse(_read(path), str(path))

    def loader(name: str) -> Node:
        sibling = path.parent / f"{name}.logos"
        if not sibling.is_file():
            raise FileNotFoundError(name)
        return parse(_read(sibling), str(sibling))

    return resolve(root, loader, str(path))


def ir_path(src_path: str) -> Path:
    """Where :func:`build` writes the IR for ``src_path``: ``build/<name>.ll``."""
    return BUILD_DIR / f"{Path(src_path).stem}.ll"


def test_ir_path(src_path: str, test_name: str) -> Path:
    """Where :func:`run_test` writes its IR, beside the build's but not over it."""
    safe = test_name.replace("/", "_").replace("\\", "_")
    return BUILD_DIR / f"{Path(src_path).stem}.{safe}.ll"


def build(
    src_path: str,
    out: str | None = None,
    opt: str = "-O2",
    keep_ir: bool = True,
) -> str:
    """Compile ``src_path`` to a native binary and return its path.

    ``out`` defaults to ``build/<name>``. The IR is written before clang runs,
    so a failed build leaves the text that failed behind; ``keep_ir=False``
    removes it again once the binary exists.
    """
    cm = check_module(load(src_path))
    if "main" not in cm.module.fns:
        raise BuildError(f"{src_path}: no 'main' function to build")
    module = lower.lower_module(cm)
    target = out if out is not None else str(BUILD_DIR / Path(src_path).stem)
    return _link(module, ir_path(src_path), target, opt, keep_ir)


def run_test(
    src_path: str, test_name: str, out: str, *, opt: str = "-O2"
) -> str:
    """Build one ``(test ...)`` body of ``src_path`` as a standalone program.

    The test body becomes ``main``: that is what lets the harness run a test as
    an ordinary process and compare its output and exit status.
    """
    cm = check_module(load(src_path))
    module = lower.lower_test(cm, test_name)
    return _link(module, test_ir_path(src_path, test_name), out, opt, True)


def _link(
    module: IrModule, ll: Path, out: str, opt: str, keep_ir: bool
) -> str:
    """Emit ``module`` to ``ll``, then hand both it and the runtime to clang."""
    ll.parent.mkdir(parents=True, exist_ok=True)
    ll.write_text(llvm.emit(module), encoding="utf-8")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    command = [
        CLANG,
        opt,
        "-target",
        TRIPLE,
        str(ll),
        str(RUNTIME_C),
        "-o",
        out,
        *LINK_FLAGS,
    ]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise BuildError(
            "clang failed: "
            + " ".join(command)
            + ("\n" + done.stderr.strip() if done.stderr.strip() else "")
        )
    if not keep_ir:
        ll.unlink()
    return out


def _read(path: Path) -> str:
    """Read a source file, reporting an unreadable one as a build failure."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(f"{path}: {error.strerror}") from None
