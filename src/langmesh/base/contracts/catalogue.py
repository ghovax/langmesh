"""In-memory sources for the prompt material supplied to a runtime."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from langmesh.base.content.prompts import PackagePromptLoader, PromptTemplates
from langmesh.base.content.instructions import Instruction, as_instructions
from langmesh.base.content.instructions import instructions_payload
from langmesh.base.content.memories import memories_payload
from langmesh.base.content.skills import enabled_skills, skills_payload
from langmesh.base.primitives.serialization import content_address


class Catalogue:
    """Everything supplied as values and nothing discovered from the host filesystem."""

    def __init__(
        self,
        agents: Optional[Mapping[str, Any]] = None,
        skills: Optional[Iterable[Any]] = None,
        memories: Optional[Iterable[Any]] = None,
        instructions: str | Iterable[Instruction] | None = None,
        prompts: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._agents = dict(agents or {})
        self._skills = list(skills or ())
        self._memories = list(memories or ())
        self._instructions = as_instructions(instructions)
        self._prompts = dict(prompts or {})
        self._fallback_prompts = packaged_prompts_directory()

    def agent(self, name: str) -> Any:
        return self._agents.get(name)

    def agents(self) -> Sequence[str]:
        return sorted(self._agents)

    def skills(self) -> Sequence[Any]:
        return list(self._skills)

    def memories(self) -> Sequence[Any]:
        return list(self._memories)

    def instructions(self) -> list[Instruction]:
        return list(self._instructions)

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        template = self._prompts.get(name)
        if template is not None:
            return PromptTemplates.render(template, variables, name)
        return PackagePromptLoader(self._fallback_prompts).load(name, variables)

    def prompt_override(self, name: str) -> Optional[str]:
        """Return the caller-supplied template named ``name`` when present."""
        return self._prompts.get(name)

    def prompt_revision(self) -> str:
        """Return the content identity of every value that can shape a rendered prompt."""
        return content_address(
            {
                "skills": skills_payload(enabled_skills(list(self._skills))),
                "memories": memories_payload(list(self._memories)),
                "instructions": instructions_payload(self._instructions),
                "prompts": self._prompts,
                "fallback": PackagePromptLoader(self._fallback_prompts).revision(),
            }
        )


def project_catalogue() -> Catalogue:
    """Return the storage-neutral catalogue used by a bare embedded session."""
    return Catalogue()


def packaged_prompts_directory() -> Path:
    """Return the package directory containing the shared runtime prompts."""
    return Path(__file__).resolve().parents[2] / "runtime" / "prompts"


__all__ = ["Catalogue", "packaged_prompts_directory", "project_catalogue"]
