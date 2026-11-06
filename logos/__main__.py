"""Allow ``python3 -m logos`` to run the compiler."""

import sys

from logos.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
