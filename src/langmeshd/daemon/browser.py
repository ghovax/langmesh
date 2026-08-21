"""Daemon-owned browser discovery and download placement."""

from __future__ import annotations

from pathlib import Path

from langmesh.base.primitives.identifiers import new_id
from langmeshd.commons.paths import browser_downloads_directory

_SUPPORT_ROOT = Path.home() / "Library" / "Application Support"
_BROWSER_DATA = {
    "chrome": _SUPPORT_ROOT / "Google" / "Chrome",
    "edge": _SUPPORT_ROOT / "Microsoft Edge",
    "brave": _SUPPORT_ROOT / "BraveSoftware" / "Brave-Browser",
}


def browser_endpoint(browser: str) -> str | None:
    """Read a browser's current DevTools endpoint from its application-owned profile."""
    directory = _BROWSER_DATA.get(browser)
    if directory is None:
        return None
    try:
        lines = (directory / "DevToolsActivePort").read_text().splitlines()
    except OSError:
        return None
    port = lines[0].strip() if lines else ""
    path = lines[1].strip() if len(lines) > 1 else ""
    return f"ws://127.0.0.1:{port}{path}" if port and path else None


def save_browser_download(download) -> dict[str, str]:
    """Place a browser download in the daemon's application data and describe it."""
    name = Path(str(download.suggested_filename or "download")).name
    destination = browser_downloads_directory() / f"{new_id('download')}-{name}"
    download.save_as(str(destination))
    return {"path": str(destination), "url": str(download.url), "name": name}


__all__ = ["browser_endpoint", "save_browser_download"]
