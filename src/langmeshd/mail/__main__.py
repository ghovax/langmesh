"""`python -m langmeshd.mail`: the same as `langmesh mail`."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from langmeshd.cli.__main__ import main as cli_main

    return cli_main(["mail", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    sys.exit(main())
