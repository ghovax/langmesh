"""`python -m langmeshd.mail`: run the IMAP/SMTP client in front of a running daemon."""

from __future__ import annotations

import asyncio
import logging
import sys

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

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
