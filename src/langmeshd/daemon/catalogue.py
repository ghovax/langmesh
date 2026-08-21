"""Filesystem catalogue assembled by the daemon for a hosted runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from langmesh.base.content.prompts import PackagePromptLoader, PromptTemplates
from langmesh.base.content.instructions import Instruction, instructions_payload
from langmesh.base.content.memories import memories_payload, parse_memory
from langmesh.base.content.skills import enabled_skills, parse_skill, skills_payload
from langmesh.base.persistence.file_cache import parsed_file
from langmesh.base.primitives.serialization import content_address


@dataclass(frozen=True)
class CatalogueRoots:
    agents: tuple[Path, ...] = ()
    skills: tuple[Path, ...] = ()
    memories: tuple[Path, ...] = ()
    instruction_files: tuple[Path, ...] = ()


class AgentLoader(Protocol):
    def load(self, name: str, directories: Sequence[Path]) -> Any: ...

    def route_names(self, directories: Sequence[Path]) -> Sequence[str]: ...


def load_skills(directories: Sequence[Path]) -> list[Any]:
    skills: dict[str, Any] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        candidates = [*sorted(directory.glob("*.md")), *sorted(directory.glob("*/SKILL.md"))]
        for path in candidates:
            content = parsed_file(path, lambda each: each.read_text()) or ""
            skill = parse_skill(content, source=str(path.resolve()))
            skills[skill.identifier] = skill
    return [skills[name] for name in sorted(skills)]


def load_memories(directories: Sequence[Path]) -> list[Any]:
    memories: dict[str, Any] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            content = parsed_file(path, lambda each: each.read_text()) or ""
            memory = parse_memory(content, source=str(path.resolve()))
            memories[memory.name] = memory
    return [memories[name] for name in sorted(memories)]


class FileCatalogue:
    """Reloadable `.agents` values owned by the daemon placement layer."""

    def __init__(
        self,
        roots: CatalogueRoots,
        agent_loader: AgentLoader | None = None,
        fallback_prompts: Optional[Path] = None,
    ) -> None:
        self._roots = roots
        self._agent_loader = agent_loader
        self._fallback_prompts = fallback_prompts

    def agent(self, name: str) -> Any:
        if not self._roots.agents or self._agent_loader is None:
            return None
        try:
            return self._agent_loader.load(name, self._roots.agents)
        except FileNotFoundError:
            return None

    def agents(self) -> Sequence[str]:
        if not self._roots.agents or self._agent_loader is None:
            return []
        return self._agent_loader.route_names(self._roots.agents)

    def skills(self) -> Sequence[Any]:
        return load_skills(self._roots.skills)

    def memories(self) -> Sequence[Any]:
        return load_memories(self._roots.memories)

    def instructions(self) -> list[Instruction]:
        entries: list[Instruction] = []
        seen: set[Path] = set()
        for path in self._roots.instruction_files:
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                continue
            if not resolved.is_file() or resolved in seen:
                continue
            seen.add(resolved)
            entries.append(
                Instruction(
                    source=str(resolved),
                    scope=str(resolved.parent),
                    content=resolved.read_text(errors="ignore").strip(),
                )
            )
        return entries

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        template = self.prompt_override(name)
        if template is not None:
            return PromptTemplates.render(template, variables, name)
        if self._fallback_prompts is None:
            return ""
        return PackagePromptLoader(self._fallback_prompts).load(name, variables)

    def prompt_override(self, name: str) -> Optional[str]:
        for root in reversed(self._roots.agents):
            path = root.parent / "prompts" / f"{name}.md"
            content = parsed_file(path, lambda each: each.read_text())
            if content is not None:
                return content
        return None

    def prompt_revision(self) -> str:
        """Return the content identity of every filesystem value exposed to prompt construction."""
        overrides: dict[str, str] = {}
        for root in self._roots.agents:
            directory = root.parent / "prompts"
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                overrides[path.stem] = parsed_file(path, lambda each: each.read_text()) or ""
        fallback = (
            PackagePromptLoader(self._fallback_prompts).revision()
            if self._fallback_prompts is not None
            else ""
        )
        return content_address(
            {
                "skills": skills_payload(enabled_skills(list(self.skills()))),
                "memories": memories_payload(list(self.memories())),
                "instructions": instructions_payload(self.instructions()),
                "prompts": overrides,
                "fallback": fallback,
            }
        )


__all__ = ["AgentLoader", "CatalogueRoots", "FileCatalogue", "load_memories", "load_skills"]
