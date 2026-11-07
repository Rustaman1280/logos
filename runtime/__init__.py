"""The C runtime, shipped as package data beside the compiler.

``rt.c`` and ``rt.h`` are compiled into every Logos binary, so they have to be
present in an installed copy of the toolchain and not only in a source tree.
This package exists to give setuptools a place to put them: ``driver.py`` looks
for ``runtime/rt.c`` next to the ``logos`` package first and falls back to the
repository's ``runtime/`` directory.
"""
