"""Sessions routes."""

from __future__ import annotations
from fastapi import APIRouter
from langmeshd.commons.database import SessionRecord, WorkspaceRecord
from langmesh.base.confinement.paths import uploads_directory
import asyncio
import re
from langmeshd.rest.routes.observations import registry_snapshot
from langmesh.protocol.dtos import (
    SessionDraftRequest,
)
from langmeshd.commons import state
from langmeshd.commons.services.broadcast import _publish_broadcast
from langmeshd.commons.services.sessions import (
    _remove_upload_file,
    _session_draft,
    _sessions_payload,
    _update_session_draft,
)

router = APIRouter()


@router.get("/sessions")
async def list_sessions():
    return await asyncio.to_thread(_sessions_payload)


@router.get("/sessions/{session_id}/draft")
async def session_draft(session_id: str):
    return {"input_draft": await asyncio.to_thread(_session_draft, session_id)}


@router.put("/sessions/{session_id}/draft")
async def update_session_draft(session_id: str, request: SessionDraftRequest):
    await asyncio.to_thread(_update_session_draft, session_id, request.input_draft)
    return {"ok": True}


@router.get("/sessions/{session_id}/record")
async def session_record(
    session_id: str,
):
    """The active workspace's current observational memory, addressed through its session."""
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        record = (
            database_session.query(SessionRecord).filter(SessionRecord.id == session_id).first()
        )
        working_directory = (
            str(record.runtime_working_directory or record.working_directory or "")
            if record is not None
            else ""
        )
    finally:
        database_session.close()
    if not working_directory:
        return {
            "entries": {"observations": [], "directives": []},
            "revision": 0,
            "metadata": {},
            "error": "",
        }
    return await registry_snapshot(working_directory)


@router.get("/sessions/{session_id}/goal-reviews")
async def session_goal_reviews(session_id: str):
    """The independent review sessions linked to one working session."""
    assert state.turn_store is not None
    return {"reviews": await state.turn_store.goal_reviews_for_session(session_id)}


@router.get("/sessions/{session_id}/turns")
async def session_turns(session_id: str):
    """Every turn a session has had, with its history and artifacts, for replay."""
    assert state.turn_store is not None
    turns = await state.turn_store.turns_for_session(session_id)
    return {
        "turns": [turn.model_dump(by_alias=True, exclude_none=True, mode="json") for turn in turns]
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Permanently delete a session and all its tasks. Aborts the context first."""
    # Settle any pause first: a session's live state is its process, so deleting it means ending it.
    await state.session_deleted(session_id)
    # Delete the context's tasks, then reclaim uploads no surviving session still references.
    if state.turn_store is not None:
        uploads_root = str(uploads_directory())
        upload_pattern = re.compile(re.escape(uploads_root) + r"/[^\"\\]+")
        referenced_uploads: set[str] = set()
        for text in await state.turn_store.session_message_texts(session_id):
            referenced_uploads.update(upload_pattern.findall(text))
        # One call drops the context's tasks and its conversation checkpoint.
        await state.turn_store.delete_session(session_id)
        for path_string in referenced_uploads:
            if not await state.turn_store.any_history_references(path_string):
                await asyncio.to_thread(_remove_upload_file, path_string, uploads_root)

    # The durable state is gone; finish by removing the sidebar record.
    def _delete_record() -> bool:
        assert state.session_factory is not None
        database_session = state.session_factory()
        try:
            record = (
                database_session.query(SessionRecord).filter(SessionRecord.id == session_id).first()
            )
            if record is None:
                database_session.commit()
                return False
            # Clear the workspace pointer, or the next client to open it looks for a session nothing can serve.
            database_session.query(WorkspaceRecord).filter(
                WorkspaceRecord.last_session_id == session_id
            ).update({WorkspaceRecord.last_session_id: ""})
            database_session.delete(record)
            database_session.commit()
            return True
        finally:
            database_session.close()

    deleted = await asyncio.to_thread(_delete_record)
    _publish_broadcast({"type": "sessions_changed"})
    return {"status": "deleted" if deleted else "not_found", "session_id": session_id}
