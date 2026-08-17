"""Observational-memory registry routes, addressed by working directory."""

from __future__ import annotations

from fastapi import APIRouter

from langmeshd.commons import state


router = APIRouter()


async def registry_snapshot(working_directory: str) -> dict[str, object]:
    """The registry snapshot for one working directory, errors included as feedback."""
    empty = {
        "entries": {"observations": [], "directives": []},
        "revision": 0,
        "metadata": {},
        "error": "",
    }
    if not working_directory:
        return empty
    watcher = state.observation_registry_watcher
    if watcher is None:
        return {**empty, "error": "Registry watcher is unavailable."}
    snapshot = await watcher.register(working_directory)
    return {
        "entries": snapshot.get("entries") or {"observations": [], "directives": []},
        "revision": int(snapshot.get("revision") or 0),
        "metadata": snapshot.get("metadata") or {},
        "error": str(snapshot.get("error") or ""),
    }


@router.get("/observations/record")
async def observation_record(working_directory: str = ""):
    """The current observational memory of a workspace or location, without needing a session."""
    return await registry_snapshot(working_directory)
