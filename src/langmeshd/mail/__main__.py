"""`python -m langmeshd.mail`: IDLE a mailbox and drive the daemon, starting it if needed."""

from __future__ import annotations

import asyncio
import logging
import sys

from langmeshd.cli.client import ensure_daemon
from langmeshd.commons.paths import log_file_path


def main(argv: list[str] | None = None) -> int:
    del argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file_path("langmesh-mail")),
        ],
    )
    from langmeshd.mail.service import run

    ensure_daemon()
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
