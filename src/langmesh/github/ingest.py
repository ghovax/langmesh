"""Durable ids of GitHub comments this thread's session has already taken.

Stdlib only: the ack step reads this before the venv exists. A comment the
running job steered is recorded here so the queued job for that comment does
not start a second turn. Keys are ``issues:{id}`` or ``pulls:{id}``.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_DIRECTORY = ".github/langmesh"
INGESTED_FILE = "ingested.json"


def ingested_path(workspace: Path) -> Path:
    return workspace / STATE_DIRECTORY / INGESTED_FILE


def load_ingested(workspace: Path) -> set[str]:
    path = ingested_path(workspace)
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if str(item).strip()}


def save_ingested(workspace: Path, ids: set[str]) -> None:
    path = ingested_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def record_ingested(workspace: Path, key: str) -> None:
    key = key.strip()
    if not key or key.endswith(":0"):
        return
    ids = load_ingested(workspace)
    ids.add(key)
    save_ingested(workspace, ids)


def already_ingested(workspace: Path, key: str) -> bool:
    key = key.strip()
    return bool(key) and key in load_ingested(workspace)
