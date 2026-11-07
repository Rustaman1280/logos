#!/usr/bin/env python3
"""Run the Logos corpus from a checkout: ``python3 tests/run.py``.

The command is the same ``logosc test`` surface, so this adds nothing but the
``sys.path`` entry a plain script run needs: executing a file inside ``tests/``
puts ``tests/`` first, not the repository root that holds the ``logos`` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from logos.cli import main  # noqa: E402  (after the path is fixed)

if __name__ == "__main__":
    raise SystemExit(main(["test", *sys.argv[1:]]))
