"""The daemon's shared AGENT.md file I/O: reading and writing profile files with YAML frontmatter.

The library's AgentConfiguration is a pure model; reading and writing the files that carry
one is the daemon's job.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from langmesh.base.configuration.configuration import AgentConfiguration, ToolsConfiguration


class AgentFileLoader:
    """The daemon's on-disk AGENT.md profiles, offered through the library's AgentLoader seam."""

    def load(self, name: str, directories: Sequence[Path]) -> AgentConfiguration:
        return load_agent_configuration(name, directories)

    def route_names(self, directories: Sequence[Path]) -> list[str]:
        return list_agent_route_names(directories)


def from_markdown(path: str | Path) -> AgentConfiguration:
    """One profile from its AGENT.md: front matter for the settings, body for the prompt."""
    path = Path(path)
    with open(path) as file_handle:
        content = file_handle.read()

    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not frontmatter_match:
        raise ValueError(f"No YAML frontmatter found in {path}")

    frontmatter = yaml.safe_load(frontmatter_match.group(1)) or {}
    markdown_body = frontmatter_match.group(2).strip()
    default_identifier = path.parent.name if path.name.upper() == "AGENT.MD" else path.stem
    frontmatter.setdefault("name", default_identifier)
    frontmatter.setdefault("title", frontmatter["name"])

    tools_data = frontmatter.pop("tools", {})
    tools_configuration = (
        ToolsConfiguration(**{name: value for name, value in tools_data.items()})
        if tools_data
        else ToolsConfiguration()
    )

    return AgentConfiguration(
        **frontmatter,
        tools=tools_configuration,
        system_prompt=markdown_body,
    )


def write_agent_markdown(path: str | Path, configuration: AgentConfiguration) -> None:
    """Write a profile back to its `AGENT.md`, the body verbatim so a round trip cannot reword the prompt."""
    path = Path(path)
    body = configuration.system_prompt.strip()
    front = configuration.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
        exclude={"system_prompt"},
    )
    front.setdefault("name", configuration.identifier)
    front["permission_mode"] = configuration.permission_mode
    rendered = yaml.safe_dump(
        front, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{rendered}\n---\n\n{body}\n" if body else f"---\n{rendered}\n---\n")


def _as_directories(directories: str | Path | Iterable[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories).expanduser()]
    return [Path(directory).expanduser() for directory in directories]


def _agent_paths(
    agents_directories: str | Path | Iterable[str | Path], include_aliases: bool = False
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in _as_directories(agents_directories):
        if not directory.is_dir():
            continue
        # `AGENT.md`, in that spelling, exactly as a skill is `SKILL.md`.
        candidates = [
            *sorted(directory.glob("*.md")),
            *sorted(directory.glob("*/AGENT.md")),
        ]
        for path in candidates:
            try:
                configuration = from_markdown(path)
                if not configuration.enabled:
                    continue
                paths[configuration.identifier] = path
                if include_aliases:
                    for alias in configuration.aliases:
                        paths[alias] = path
            except Exception:
                fallback = path.parent.name if path.name.upper() == "AGENT.MD" else path.stem
                paths[fallback] = path
    return paths


def load_agent_configuration(
    name: str, agents_directory: str | Path | Iterable[str | Path]
) -> AgentConfiguration:
    paths = _agent_paths(agents_directory, include_aliases=True)
    path = paths.get(name)
    if path is None:
        searched = ", ".join(str(directory) for directory in _as_directories(agents_directory))
        raise FileNotFoundError(f"Agent configuration not found: {name} (searched: {searched})")
    return from_markdown(path)


def agent_configuration_path(
    name: str, agents_directory: str | Path | Iterable[str | Path]
) -> Path:
    paths = _agent_paths(agents_directory, include_aliases=True)
    path = paths.get(name)
    if path is None:
        searched = ", ".join(str(directory) for directory in _as_directories(agents_directory))
        raise FileNotFoundError(f"Agent configuration not found: {name} (searched: {searched})")
    return path


def list_agent_route_names(agents_directory: str | Path | Iterable[str | Path]) -> list[str]:
    return sorted(_agent_paths(agents_directory, include_aliases=True))


def list_agents(agents_directory: str | Path | Iterable[str | Path]) -> list[dict[str, str]]:
    agents = []
    for name, path in sorted(_agent_paths(agents_directory).items()):
        try:
            configuration = from_markdown(path)
            agents.append(
                {
                    "id": configuration.identifier,
                    "name": configuration.identifier,
                    "title": configuration.display_name,
                    # What the agent is for — surfaced as the subtitle in the UI's agent picker.
                    "description": configuration.description,
                    # The resolved `provider/model`; empty means no runnable model is configured.
                    "model": configuration.model_identifier or "",
                }
            )
        except Exception:
            agents.append({"id": name, "name": name, "title": name, "description": "", "model": ""})
    return agents


__all__ = [
    "AgentFileLoader",
    "agent_configuration_path",
    "from_markdown",
    "list_agent_route_names",
    "list_agents",
    "load_agent_configuration",
    "write_agent_markdown",
]
