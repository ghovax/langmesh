"""File-based skills: a name, a title and a description in front matter, with instructions in the body."""

from __future__ import annotations

import re
from pathlib import PurePath

import yaml
from pydantic import BaseModel

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


class Skill(BaseModel):
    name: str = ""  # stable identifier
    title: str = ""  # human-friendly display title
    description: str = ""
    enabled: bool = True
    body: str = ""
    path: str = ""

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def display_title(self) -> str:
        return self.title or self.name


def parse_skill(content: str, *, source: str = "", default_name: str = "") -> Skill:
    """Parse one caller-supplied skill document into a value."""
    source_path = PurePath(source) if source else PurePath(default_name)
    match = _FRONTMATTER.match(content)
    if match:
        frontmatter = yaml.safe_load(match.group(1)) or {}
        body = match.group(2).strip()
        inferred_name = (
            source_path.parent.name if source_path.name.upper() == "SKILL.MD" else source_path.stem
        )
        default_identifier = default_name or inferred_name
        identifier = str(frontmatter.get("name") or default_identifier)
        title = str(frontmatter.get("title") or identifier)
        description = str(frontmatter.get("description", ""))
        enabled = bool(frontmatter.get("enabled", True))
    else:
        inferred_name = (
            source_path.parent.name if source_path.name.upper() == "SKILL.MD" else source_path.stem
        )
        identifier = default_name or inferred_name
        title = identifier
        description = ""
        enabled = True
        body = content.strip()
    return Skill(
        name=identifier,
        title=title,
        description=description,
        enabled=enabled,
        body=body,
        path=source,
    )


def enabled_skills(skills: list[Skill]) -> list[Skill]:
    """The subset of skills that are enabled — what an agent may actually use."""
    return [skill for skill in skills if skill.enabled]


def skills_for_agent(skills: list[Skill], allowed_names: list[str]) -> list[Skill]:
    """The skills available to an agent: all of them, or the subset its ``skills`` front matter names."""
    if not allowed_names:
        return skills
    wanted = set(allowed_names)
    return [skill for skill in skills if skill.identifier in wanted]


def skills_payload(skills: list[Skill]) -> list[dict]:
    """The structured skills data injected into an agent's system context."""
    return [
        {
            "name": skill.identifier,
            "title": skill.display_title,
            "description": skill.description,
            "path": skill.path,
        }
        for skill in skills
    ]
