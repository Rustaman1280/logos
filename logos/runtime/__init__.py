"""The C runtime that ships with the compiler.

``rt.c`` and ``rt.h`` are compiled into every Logos binary, so an installed
copy of ``logosc`` has to carry them. They live inside the ``logos`` package,
as package data (see ``pyproject.toml``), so setuptools includes them in every
wheel regardless of build front end. ``logos.driver`` resolves them through
``importlib.resources`` and falls back to this directory in a source checkout.
"""
