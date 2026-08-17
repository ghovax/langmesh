"""The langmeshd app: one executable, two entry points (the CLI and the daemon).

This package is a user of the `langmesh` library, never part of it.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # `langmeshd` rather than `daemon`, since the command line has its own `daemon` verb.
    if arguments and arguments[0] == "langmeshd":
        from langmeshd.daemon.__main__ import main as daemon_main

        sys.argv = [sys.argv[0], *arguments[1:]]
        return daemon_main()

    from langmeshd.cli.__main__ import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    sys.exit(main())
