"""The labeled fields one mail turn sends, matching the GitHub mention JSON shape."""

from __future__ import annotations

import json


def turn_payload(*, subject: str, message: str, first: bool) -> dict[str, str]:
    """Opening turns name the subject; later mail on the same thread is only the new body."""
    payload: dict[str, str] = {}
    if first:
        stripped = subject.strip()
        if stripped:
            payload["subject"] = stripped
    payload["message"] = message
    return payload


def turn_text(*, subject: str, message: str, first: bool) -> str:
    """One JSON object with a key in front of each field. Not a From/Subject header block."""
    return json.dumps(
        turn_payload(subject=subject, message=message, first=first),
        ensure_ascii=False,
        indent=2,
    )
