"""Turning inbound messages into what the model reads, and runtime events into what the client renders."""

from __future__ import annotations

from typing import Optional

from a2a.types import DataPart, FilePart, Part, TextPart

from langmesh.base.content.attachments import (
    all_attachments as _all_attachments,
    attachment_payload,
    compose_turn_input,
    image_attachments as _image_attachments,
)
from langmesh.base.content.message_content import content_block_metadata
from langmesh.base.confinement.paths import uploads_directory
from langmesh.protocol.events import (
    ToolMetadata,
    ToolResultEvent,
    WarningEvent,
    _EventBase,
)
from langmesh.runtime.values import ToolStatus
from langmesh.protocol.files import ingest_file_part
from langmesh.protocol.metadata import (
    INPUT_RESPONSE_KIND,
    PART_KIND,
    part_payload,
    wrap_part_payload,
)

__all__ = [
    "_all_attachments",
    "_image_attachments",
    "attachment_payload",
    "compose_turn_input",
]


def _input_response_payload(message) -> Optional[dict]:
    """The input-required answer this message carries, or ``None``."""
    for part in message.parts or []:
        root = getattr(part, "root", part)
        payload = part_payload(root.data) if isinstance(root, DataPart) else {}
        if payload.get(PART_KIND) == INPUT_RESPONSE_KIND:
            return dict(payload)
    return None


async def _ingest_incoming_file_parts(message) -> list[dict]:
    """Materialize every inbound file part into the upload store, so a peer's file arrives like a local attachment."""
    attachments: list[dict] = []
    for part in message.parts or []:
        root = getattr(part, "root", part)
        if isinstance(root, FilePart):
            attachment = await ingest_file_part(part, uploads_directory().parent)
            if attachment is not None:
                attachments.append(attachment)
    return attachments


def _structured_data_payloads(message) -> list[dict]:
    """Return the DataPart payloads carried by the user turn."""
    payloads: list[dict] = []
    for part in message.parts or []:
        root = getattr(part, "root", part)
        if isinstance(root, DataPart):
            data = part_payload(root.data)
            payloads.append(dict(data))
    return payloads


def _attachment_warning_event(image_count: int, model_identifier: str) -> WarningEvent:
    """A localized notice that images reached a text-only model as file metadata."""
    return WarningEvent(
        code="image_metadata_only",
        parameters={"count": image_count, "model": model_identifier},
    )


def _text_part(text: str, block_identifier: str) -> Part:
    return Part(
        root=TextPart(
            text=text,
            metadata=content_block_metadata(block_identifier),
        )
    )


def _event_part(event: _EventBase) -> Part:
    """A validated wire-event part, so a misnamed field fails at the emit site rather than in a client."""
    return Part(root=DataPart(data=wrap_part_payload(event.model_dump(mode="json"))))


def _tool_result_part(tool_name: str, tool_call_id: str, result: object, status: str) -> Part:
    """The unified tool-result wire event, whose `display` is the result the interface renders."""
    record = result if isinstance(result, dict) else {}
    return _event_part(
        ToolResultEvent(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status=ToolStatus(status),
            code=record.get("code"),
            display=result,
            metadata=ToolMetadata(tool_name=tool_name, tool_call_id=tool_call_id),
        )
    )
