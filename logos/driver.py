"""Build Logos programs: front end, IR, clang.

The driver is the only part of the compiler that writes into ``build/`` and the
only part that runs another process. It owns two paths, and both are anchored
to the repository rather than to the caller's working directory, so a build
works the same from the root of the tree, from a test, or from anywhere else:

* the IR of a source file, ``build/<name>.ll``, kept on disk because it is the
  artefact a person reads when a program misbehaves;
* the C runtime, ``logos/runtime/rt.c``, linked into every binary.

The link is one clang invocation::

    clang {opt} -target x86_64-unknown-linux-gnu build/<name>.ll logos/runtime/rt.c
          -o <out> -lpthread -lm

clang compiles the IR text and ``rt.c``, then links both with libc. There are no
LLVM bindings, no ``llc`` and no ``lld`` in this pipeline; the IR text is the
whole interface.
"""

from __future__ import annotations

import subprocess
from importlib import resources
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


def _runtime_file(name: str) -> Path:
    """The runtime source ``name``, from the installed package if it shipped.

    ``logos.runtime`` is a real package and ``rt.c``/``rt.h`` are its package
    data, so a wheel always carries them. ``importlib.resources`` resolves the
    file inside the installed package. A source checkout resolves to the same
    directory; if the package is incomplete (an old install, or a zipped
    distribution whose data is not a real file), fall back to ``logos/runtime``
    beside this module so a checkout still builds.
    """
    try:
        resource = resources.files("logos.runtime").joinpath(name)
        path = Path(str(resource))
    except (ImportError, ModuleNotFoundError):
        path = None
    if path is not None and path.is_file():
        return path
    return Path(__file__).resolve().parent / "runtime" / name


#: The C runtime, linked into every Logos binary.
RUNTIME_C = _runtime_file("rt.c")

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
    *,
    debug: bool = False,
) -> str:
    """Compile ``src_path`` to a native binary and return its path.

    ``out`` defaults to ``build/<name>``. A failed build leaves no artefact
    behind: the binary and the ``.ll`` at the output paths are removed before
    the error is re-raised, so nothing can run a stale program or be read as
    the text that failed. ``keep_ir=False`` removes the IR after a success.

    ``debug`` makes ``main`` report every block still live at exit. A release
    build does not, because its stderr belongs to the program.
    """
    target = out if out is not None else str(BUILD_DIR / Path(src_path).stem)
    ll = ir_path(src_path)
    try:
        cm = check_module(load(src_path))
        if "main" not in cm.module.fns:
            raise BuildError(f"{src_path}: no 'main' function to build")
        module = lower.lower_module(cm, debug=debug)
        return _link(module, ll, target, opt, keep_ir, debug=debug)
    except BaseException:
        _discard(ll, Path(target))
        raise


def run_test(
    src_path: str, test_name: str, out: str, *, opt: str = "-O2", debug: bool = True
) -> str:
    """Build one ``(test ...)`` body of ``src_path`` as a standalone program.

    The test body becomes ``main``: that is what lets the harness run a test as
    an ordinary process and compare its output and exit status. The module's
    own functions are lowered alongside it, so a test may call one.

    A test build is a debug build by default: its ``main`` reports every block
    still live at exit, which is what makes a leaked block visible to the
    runner instead of silently passing.
    """
    ll = test_ir_path(src_path, test_name)
    try:
        cm = check_module(load(src_path))
        module = lower.lower_test(cm, test_name, debug=debug)
        return _link(module, ll, out, opt, True, debug=debug)
    except BaseException:
        _discard(ll, Path(out))
        raise


def _link(
    module: IrModule,
    ll: Path,
    out: str,
    opt: str,
    keep_ir: bool,
    *,
    debug: bool = False,
) -> str:
    """Emit ``module`` to ``ll``, then hand both it and the runtime to clang.

    ``debug`` also defines ``LOGOS_DEBUG`` for the C runtime. That is what makes
    the runtime quarantine a released block instead of returning it to malloc,
    so a freed address cannot be recycled and the double-free check stays exact.
    """
    ll.parent.mkdir(parents=True, exist_ok=True)
    ll.write_text(llvm.emit(module), encoding="utf-8")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    command = [
        CLANG,
        *(["-DLOGOS_DEBUG=1"] if debug else []),
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
        _discard(ll, Path(out))
        raise BuildError(
            "clang failed: "
            + " ".join(command)
            + ("\n" + done.stderr.strip() if done.stderr.strip() else "")
        )
    if not keep_ir:
        ll.unlink()
    return out


def _discard(*paths: Path) -> None:
    """Remove build artefacts, ignoring what is already gone.

    Called on every failure path. An ``OSError`` is swallowed on purpose: the
    caller is about to raise the error that actually matters, and a permission
    problem while cleaning up must not replace it.
    """
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _read(path: Path) -> str:
    """Read a source file, reporting an unreadable one as a build failure."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(f"{path}: {error.strerror}") from None
