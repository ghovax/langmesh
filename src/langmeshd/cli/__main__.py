"""`langmesh`, the command: serve the interface, with the daemon behind it.

The command line has two long-running clients: `serve` makes the interface available, and
`mail` IDLEs a mailbox and drives the daemon over its API. `mail check` proves that mailbox
without IDLEing. `mail auth` signs the mailbox in with OAuth. Creating and messaging sessions, answering permission requests, recurring
work, remote agents, configuration, and sign-in otherwise happen in the interface, or
programmatically against the daemon's API. A session
composes with its peers through [tools](agent-system.md), over the same control plane; it
does not shell out to this command.
"""

from __future__ import annotations

import argparse
import logging
import sys

from langmeshd.cli.client import DaemonError


def _note(message: str) -> None:
    """A diagnostic, through the logger and never on stdout, which carries data."""
    logging.getLogger("langmesh").info(message)


def _command_serve(arguments: argparse.Namespace) -> int:
    """Make LangMesh available: the interface in front of it, with the daemon behind it."""
    from langmeshd.cli.commands import serve

    return serve.run(arguments)


def _command_mail(arguments: argparse.Namespace) -> int:
    """IDLE a mailbox and drive the daemon as a client, one turn per inbound message."""
    from langmeshd.cli.commands import mail

    return mail.run(arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="langmesh",
        description="Serve the interface, or sit in front of the daemon as a mail client.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser(
        "serve", help="make LangMesh available: the interface and the daemon behind it"
    )
    serve.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help="port to listen on (default 8824; 8825 with --reach)",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (default 127.0.0.1; this surface drives the daemon, so keep it local)",
    )
    serve.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="also open a browser at the served address (off by default: serving is not a reason to take over the screen)",
    )
    serve.add_argument(
        "--reach",
        action="store_true",
        help="serve the paired door: a durable pairing link, token-gated, for your own devices over a transport you choose",
    )
    serve.set_defaults(handler=_command_serve)

    mail = subparsers.add_parser(
        "mail",
        help="IDLE an allowlisted mailbox and drive the daemon; replies go out over SMTP",
    )
    mail_sub = mail.add_subparsers(dest="mail_command", required=False)
    mail_sub.add_parser(
        "check",
        help="prove mailbox config, IMAP login, and SMTP auth without IDLEing",
    )
    mail_sub.add_parser(
        "auth",
        help="sign in with OAuth and write the mailbox refresh token",
    )
    mail.set_defaults(handler=_command_mail)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Prose on stderr with nothing in front of it, forced because a library may already have configured logging.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    logging.getLogger("langmesh").setLevel(logging.INFO)
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except DaemonError as error:
        _note(f"langmesh: {error}")
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # A closed pipe is a normal way to use a command, so it must not print a traceback.
        import os

        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        # 128 + SIGPIPE, the exit status a shell expects from a program a pipe closed under.
        return 141


if __name__ == "__main__":
    sys.exit(main())
