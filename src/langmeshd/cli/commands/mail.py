"""`langmesh mail`: IDLE a mailbox and drive the daemon. The session mails through `submit_email`."""

from __future__ import annotations

import asyncio
import logging
import sys

from langmeshd.cli.client import ensure_daemon
from langmeshd.commons.paths import log_file_path
from langmeshd.commons.secret_import import import_into_files


def check_mailbox() -> int:
    """Prove IMAP and SMTP from configuration and secret files, without starting IDLE or the daemon."""
    from langmeshd.mail.service import check as check_mail

    return asyncio.run(check_mail())


def run(arguments) -> int:  # noqa: ANN001 — argparse namespace, matching serve.run
    import_into_files()
    if getattr(arguments, "mail_command", None) == "check":
        return check_mailbox()
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
