"""Model-facing attachment composition over caller-supplied values."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from langmesh.base.content.models import find_model
from langmesh.base.primitives.serialization import compact

ATTACHMENTS_KIND = "attachments"
INLINE_IMAGE_MIME_PREFIX = "image/"


@dataclass(frozen=True)
class ComposedAttachments:
    """Model content plus the explicit filesystem authority and warning facts it requires."""

    content: str | list[dict[str, Any]]
    granted_paths: tuple[str, ...]
    omitted_image_count: int = 0


@dataclass(frozen=True)
class Attachment:
    """Attachment metadata and optional inline bytes supplied by the caller."""

    identifier: str
    name: str
    media_type: str = "application/octet-stream"
    data: bytes | None = None
    path: str = ""
    digest: str = ""

    @property
    def size(self) -> int:
        """The byte size available to the composer."""
        return len(self.data) if self.data is not None else 0

    def record(self) -> dict[str, Any]:
        """Return the transport metadata without embedding the bytes twice."""
        return {
            "upload_id": self.identifier,
            "title": self.name,
            "filename": self.name,
            "path": self.path,
            "mime_type": self.media_type,
            "size": self.size,
            "sha256": self.digest,
        }


def attachments_payload(attachments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return the structured payload carried beside user text."""
    return {"kind": ATTACHMENTS_KIND, "attachments": list(attachments)}


def _attachments_with_mime(
    structured_payloads: Sequence[dict[str, Any]], mime_prefix: str = ""
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for payload in structured_payloads:
        if payload.get("kind") != ATTACHMENTS_KIND:
            continue
        for attachment in payload.get("attachments") or ():
            if not isinstance(attachment, dict):
                continue
            if mime_prefix and not str(attachment.get("mime_type", "")).startswith(mime_prefix):
                continue
            attachments.append(attachment)
    return attachments


def attachment_records(structured_payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every attachment record carried by the structured payloads."""
    return _attachments_with_mime(structured_payloads)


def image_attachment_records(
    structured_payloads: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return every image attachment record carried by the structured payloads."""
    return _attachments_with_mime(structured_payloads, INLINE_IMAGE_MIME_PREFIX)


def _model_supports_vision(model_identifier: str) -> bool:
    if not model_identifier:
        return True
    model = find_model(model_identifier)
    return model is None or model.vision


def _image_content_block(
    attachment: dict[str, Any],
    inline_image_bytes: int,
    content: Callable[[dict[str, Any]], bytes | None] | None,
) -> dict | None:
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    # The recorded size answers the budget without reading the file, so an oversized attachment costs no memory just to be dropped.
    size = attachment.get("size")
    if isinstance(size, int) and size > inline_image_bytes:
        return None
    raw = content(attachment) if content is not None else None
    if raw is None:
        return None
    if len(raw) > inline_image_bytes:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}


def compose_attachment_content(
    user_text: str,
    structured_payloads: Sequence[dict[str, Any]],
    model_identifier: str,
    inline_image_bytes: int,
    content: Callable[[dict[str, Any]], bytes | None] | None = None,
) -> tuple[str | list[dict[str, Any]], int]:
    """Compose text, attachment metadata, and eligible image blocks for one provider request."""
    text_payload = compact({"text": user_text, "data_parts": list(structured_payloads)})
    images = image_attachment_records(structured_payloads)
    if not images:
        return text_payload, 0
    if not _model_supports_vision(model_identifier):
        return text_payload, len(images)
    blocks = [
        block
        for image in images
        if (block := _image_content_block(image, inline_image_bytes, content)) is not None
    ]
    # Every image the model does not actually receive is one the caller must be told about: a wrong zero here silently drops the attachment with no warning.
    omitted_image_count = len(images) - len(blocks)
    if not blocks:
        return text_payload, omitted_image_count
    return [{"type": "text", "text": text_payload}, *blocks], omitted_image_count


class AttachmentComposer:
    """Compose attachment values without reading or writing their storage."""

    def compose(
        self,
        message: str,
        attachments: Sequence[Attachment],
        model_identifier: str,
        inline_image_bytes: int,
    ) -> ComposedAttachments:
        records = [attachment.record() for attachment in attachments]
        by_identifier = {attachment.identifier: attachment for attachment in attachments}
        content, omitted_image_count = compose_attachment_content(
            message,
            [attachments_payload(records)],
            model_identifier,
            inline_image_bytes,
            content=lambda record: (
                by_identifier[str(record.get("upload_id") or "")].data
                if str(record.get("upload_id") or "") in by_identifier
                else None
            ),
        )
        return ComposedAttachments(
            content=content,
            granted_paths=tuple(str(record["path"]) for record in records),
            omitted_image_count=omitted_image_count,
        )


__all__ = [
    "Attachment",
    "AttachmentComposer",
    "ComposedAttachments",
]
