"""Model-facing composition for paths an application attaches to a turn."""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

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


def attachment_from_path(path: Path | str) -> dict[str, Any]:
    """Describe a regular local file in place without copying it."""
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(f"{resolved} is not a regular file.")
    name = resolved.name
    return {
        "upload_id": f"ref-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{os.urandom(4).hex()}",
        "title": name,
        "filename": name,
        "path": str(resolved),
        "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "size": resolved.stat().st_size,
        "sha256": "",
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


def _image_content_block(attachment: dict[str, Any], inline_image_bytes: int) -> dict | None:
    path = str(attachment.get("path") or "")
    if not path:
        return None
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    # The recorded size answers the budget without reading the file, so an oversized attachment costs no memory just to be dropped.
    size = attachment.get("size")
    if isinstance(size, int) and size > inline_image_bytes:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
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
        if (block := _image_content_block(image, inline_image_bytes)) is not None
    ]
    # Every image the model does not actually receive is one the caller must be told about: a wrong zero here silently drops the attachment with no warning.
    omitted_image_count = len(images) - len(blocks)
    if not blocks:
        return text_payload, omitted_image_count
    return [{"type": "text", "text": text_payload}, *blocks], omitted_image_count


class PathAttachments:
    """Compose local file paths with metadata and bounded vision-model image blocks."""

    def compose(
        self,
        message: str,
        attachments: Sequence[Path],
        model_identifier: str,
        inline_image_bytes: int,
    ) -> ComposedAttachments:
        records = [attachment_from_path(path) for path in attachments]
        content, omitted_image_count = compose_attachment_content(
            message,
            [attachments_payload(records)],
            model_identifier,
            inline_image_bytes,
        )
        return ComposedAttachments(
            content=content,
            granted_paths=tuple(str(record["path"]) for record in records),
            omitted_image_count=omitted_image_count,
        )


__all__ = [
    "ComposedAttachments",
    "PathAttachments",
]
