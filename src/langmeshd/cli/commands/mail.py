"""`langmesh mail`: IDLE a mailbox and drive the daemon, one turn per inbound message."""

from __future__ import annotations

import asyncio
import logging
import sys

from langmeshd.cli.client import ensure_daemon
from langmeshd.commons.paths import log_file_path


def run(_arguments) -> int:  # noqa: ANN001 — argparse namespace, matching serve.run
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file_path("langmesh-mail")),
        ],
        force=True,
    )
    ensure_daemon()
    from langmeshd.mail.service import run as run_mail

    try:
        return asyncio.run(run_mail())
    except KeyboardInterrupt:
        return 0
