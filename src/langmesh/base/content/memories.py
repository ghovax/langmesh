"""Durable notes an agent keeps about a project, loaded from its memories directory."""

from __future__ import annotations

import re
from pathlib import PurePath

import yaml
from pydantic import BaseModel, Field


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Memory(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    importance: str = ""
    tags: list[str] = Field(default_factory=list)
    body: str = ""
    path: str = ""


def parse_memory(content: str, *, source: str = "", default_name: str = "") -> Memory:
    """Parse one caller-supplied memory document into a value."""
    source_path = PurePath(source) if source else PurePath(default_name)
    inferred_name = default_name or source_path.stem
    match = _FRONTMATTER.match(content)
    if not match:
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        return Memory(
            name=inferred_name,
            title=inferred_name,
            description=first_line[:240],
            body=content.strip(),
            path=source,
        )
    frontmatter = yaml.safe_load(match.group(1)) or {}
    raw_tags = frontmatter.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
    else:
        tags = [str(tag) for tag in raw_tags]
    return Memory(
        name=str(frontmatter.get("name") or inferred_name),
        title=str(frontmatter.get("title") or inferred_name),
        description=str(frontmatter.get("description") or ""),
        importance=str(frontmatter.get("importance", "")),
        tags=tags,
        body=match.group(2).strip(),
        path=source,
    )


def memories_payload(memories: list[Memory]) -> list[dict]:
    return [
        {
            "name": memory.name,
            "title": memory.title,
            "description": memory.description,
            "importance": memory.importance,
            "tags": memory.tags,
            "path": memory.path,
            "read_hint": "Read this path with bash if the description indicates it is relevant.",
        }
        for memory in memories
    ]
