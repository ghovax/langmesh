"""Model-facing composition for paths an application attaches to a turn."""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from langmesh.base.models import find_model
from langmesh.base.serialization import compact

ATTACHMENTS_KIND = "attachments"
INLINE_IMAGE_MIME_PREFIX = "image/"


@dataclass(frozen=True)
class AttachmentInput:
    """A composed provider input plus the access and warning facts it produced."""

    value: object
    paths: tuple[str, ...]
    images_not_inlined: int = 0


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


def attachment_payload(attachments: Sequence[dict[str, Any]]) -> dict[str, Any]:
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


def all_attachments(structured_payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every attachment record carried by the structured payloads."""
    return _attachments_with_mime(structured_payloads)


def image_attachments(structured_payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
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
    # The recorded size answers the budget without reading the file, so an oversized
    # attachment costs no memory just to be dropped.
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


def compose_turn_input(
    user_text: str,
    structured_payloads: Sequence[dict[str, Any]],
    model_identifier: str,
    inline_image_bytes: int,
) -> tuple[object, int]:
    """Compose text, attachment metadata, and eligible image blocks for one provider request."""
    text_payload = compact({"text": user_text, "data_parts": list(structured_payloads)})
    images = image_attachments(structured_payloads)
    if not images:
        return text_payload, 0
    if not _model_supports_vision(model_identifier):
        return text_payload, len(images)
    blocks = [
        block
        for image in images
        if (block := _image_content_block(image, inline_image_bytes)) is not None
    ]
    # Every image the model does not actually receive is one the caller must be told
    # about: a wrong zero here silently drops the attachment with no warning.
    images_not_inlined = len(images) - len(blocks)
    if not blocks:
        return text_payload, images_not_inlined
    return [{"type": "text", "text": text_payload}, *blocks], images_not_inlined


class PathAttachments:
    """Compose local file paths with metadata and bounded vision-model image blocks."""

    def compose(
        self,
        message: str,
        attachments: Sequence[Path],
        model_identifier: str,
        inline_image_bytes: int,
    ) -> AttachmentInput:
        records = [attachment_from_path(path) for path in attachments]
        value, images_not_inlined = compose_turn_input(
            message,
            [attachment_payload(records)],
            model_identifier,
            inline_image_bytes,
        )
        return AttachmentInput(
            value=value,
            paths=tuple(str(record["path"]) for record in records),
            images_not_inlined=images_not_inlined,
        )


__all__ = [
    "AttachmentInput",
    "PathAttachments",
    "all_attachments",
    "attachment_from_path",
    "attachment_payload",
    "compose_turn_input",
    "image_attachments",
]
