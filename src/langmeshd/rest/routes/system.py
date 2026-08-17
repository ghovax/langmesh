"""The macOS system-permission surface: what the daemon can reach, and how to grant it.

Two generic endpoints instead of a path per permission: the subject is carried in the JSON, so
adding a permission never means adding a route.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from langmeshd.commons.services import workspaces as _workspaces
from langmeshd.rest.services import filesystem as _system

router = APIRouter()

#: Every system permission the daemon can be granted, and the probe that reads whether it has it.
_PERMISSIONS = {
    "full_disk_access": _workspaces._full_disk_access_granted,
    "accessibility": _system._accessibility_granted,
}
#: How to open the settings pane that grants each permission.
_OPEN = {
    "full_disk_access": _workspaces._open_full_disk_access_settings,
    "accessibility": _system._open_accessibility_settings,
}
#: The prompt that asks for the permission, where opening the pane alone is not enough.
_PROMPT = {
    "full_disk_access": None,
    "accessibility": _system._request_accessibility,
}


class PermissionOpenRequest(BaseModel):
    permission: Literal["full_disk_access", "accessibility"]


@router.get("/system/permissions")
async def system_permissions():
    """Which macOS system permissions the daemon holds, keyed by permission name."""
    granted = {}
    for name, probe in _PERMISSIONS.items():
        granted[name] = await asyncio.to_thread(probe)
    return {"permissions": granted}


@router.post("/system/permissions/open")
async def open_system_permission(request: PermissionOpenRequest):
    """Open (and prompt for, where required) the settings pane that grants one permission."""
    if request.permission not in _PERMISSIONS:
        raise HTTPException(status_code=404, detail=f"No system permission named {request.permission!r}.")
    prompt = _PROMPT.get(request.permission)
    if prompt is not None:
        await asyncio.to_thread(prompt)
    await asyncio.to_thread(_OPEN[request.permission])
    return {"ok": True}
