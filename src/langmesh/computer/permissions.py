"""TCC checks and deep-links for the screen tools. One grant matters: Accessibility."""

from __future__ import annotations

import subprocess
from contextlib import suppress

import ApplicationServices as AS

from langmesh.base.primitives.limits import current_limits


def accessibility_granted() -> bool:
    """Whether this process may read the AX tree and post synthesized input."""
    return bool(AS.AXIsProcessTrusted())  # type: ignore[attr-defined]


def request_accessibility() -> bool:
    """Check trust and, if untrusted, surface the prompt that deep-links to the Accessibility pane."""
    options = {AS.kAXTrustedCheckOptionPrompt: True}  # type: ignore[attr-defined]
    return bool(AS.AXIsProcessTrustedWithOptions(options))  # type: ignore[attr-defined]


def open_accessibility_settings() -> None:
    _open("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")


def _open(url: str) -> None:
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", url], check=False, timeout=current_limits().open_url
        )
