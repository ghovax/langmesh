"""The chat transcript's recall surface: user-message history for up/down-arrow recall."""

from __future__ import annotations

from fastapi import APIRouter

from langmeshd.commons import state

router = APIRouter()


@router.get("/messages/history")
async def get_message_history(working_directory: str = ""):
    """Return the last 100 user messages sent in this project, newest first."""
    if not working_directory:
        return {"messages": []}
    assert state.turn_store is not None
    messages = await state.turn_store.get_user_messages(working_directory)
    return {"messages": messages}


@router.post("/messages/history")
async def save_message_history(body: dict):
    """Persist a user message for up/down arrow recall within the project."""
    assert state.turn_store is not None
    await state.turn_store.add_user_message(body["working_directory"], body["message"])
    return {"ok": True}
