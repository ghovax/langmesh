"""Ingest workspace paths as model-facing text and image blocks."""

from __future__ import annotations

import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes
from PIL import Image, UnidentifiedImageError

from langmesh.base.confinement.confinement import expand
from langmesh.base.content.attachments import image_url_block, model_supports_vision
from langmesh.base.primitives.limits import clip_to_tokens, current_limits
from langmesh.runtime.tools import context as tool_context
from langmesh.runtime.tools.execution import current_tool_services


def _image_mime(raw: bytes) -> str | None:
    """The image MIME type Pillow recognises, or ``None`` when the bytes are not an image."""
    try:
        with Image.open(BytesIO(raw)) as image:
            fmt = image.format
    except (UnidentifiedImageError, OSError):
        return None
    if not fmt:
        return None
    return Image.MIME.get(fmt) or f"image/{fmt.lower()}"


def _decode_text(raw: bytes) -> str | None:
    """Decode ``raw`` as text via charset-normalizer, or ``None`` when it is not text."""
    if not raw:
        return ""
    match = from_bytes(raw).best()
    if match is None:
        return None
    return str(match)


def ingest_paths(paths: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read ``paths`` under the bound confinement. Image blocks are separate from the JSON result."""
    requested = [str(path).strip() for path in paths if str(path).strip()]
    if not requested:
        return {"code": "paths_required"}, []
    services = current_tool_services()
    active = tool_context.current()
    workspace = active.workspace
    vision = model_supports_vision(services.model_identifier)
    budget = current_limits().output_tokens
    inline_budget = max(0, services.inline_image_bytes)
    entries: list[dict[str, Any]] = []
    image_blocks: list[dict[str, Any]] = []
    omitted_images = 0
    for original in requested:
        resolved = expand(original, workspace=workspace)
        if not resolved:
            entries.append({"path": original, "code": "path_unresolved"})
            continue
        location = Path(resolved)
        if not active.sandbox.may_read(resolved, workspace=workspace):
            entries.append({"path": original, "resolved": resolved, "code": "path_not_readable"})
            continue
        if not location.is_file():
            entries.append(
                {
                    "path": original,
                    "resolved": resolved,
                    "code": "path_not_found" if not location.exists() else "path_not_a_file",
                }
            )
            continue
        try:
            size = location.stat().st_size
            raw = location.read_bytes()
        except OSError as error:
            entries.append(
                {
                    "path": original,
                    "resolved": resolved,
                    "code": "path_read_error",
                    "message": str(error),
                }
            )
            continue
        image_mime = _image_mime(raw)
        if image_mime:
            if not vision:
                omitted_images += 1
                entries.append(
                    {
                        "path": original,
                        "resolved": resolved,
                        "code": "image_unsupported_by_model",
                        "mime_type": image_mime,
                        "size": size,
                    }
                )
                continue
            block = image_url_block(image_mime, raw, inline_budget)
            if block is None:
                omitted_images += 1
                entries.append(
                    {
                        "path": original,
                        "resolved": resolved,
                        "code": "image_too_large",
                        "mime_type": image_mime,
                        "size": size,
                    }
                )
                continue
            image_blocks.append(block)
            entries.append(
                {
                    "path": original,
                    "resolved": resolved,
                    "code": "image_ingested",
                    "mime_type": image_mime,
                    "size": size,
                }
            )
            continue
        text = _decode_text(raw)
        if text is None:
            guessed, _encoding = mimetypes.guess_type(location.name)
            entries.append(
                {
                    "path": original,
                    "resolved": resolved,
                    "code": "unread_media",
                    "mime_type": guessed or "application/octet-stream",
                    "size": size,
                }
            )
            continue
        excerpt, truncated = clip_to_tokens(text, budget)
        guessed, _encoding = mimetypes.guess_type(location.name)
        text_mime = guessed if guessed and guessed.startswith("text/") else "text/plain"
        if guessed in {"application/json", "application/javascript"}:
            text_mime = guessed
        entries.append(
            {
                "path": original,
                "resolved": resolved,
                "code": "text_ingested",
                "mime_type": text_mime,
                "size": size,
                "truncated": truncated,
                "content": excerpt,
            }
        )
    return {
        "code": "paths_read",
        "paths": entries,
        "image_count": len(image_blocks),
        "omitted_image_count": omitted_images,
    }, image_blocks
