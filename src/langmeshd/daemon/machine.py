"""Reading this machine: the XDG configuration, and the agents on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from langmesh.base.configuration import Configuration
from langmesh.base.contracts.catalogue import AgentLoader, CatalogueRoots, FileCatalogue
from langmesh.base.contracts.catalogue import packaged_prompts_directory
from langmeshd.commons.configuration_io import load_configuration  # noqa: F401 — re-exported for callers of machine.load_configuration
from langmeshd.daemon.agent_files import AgentFileLoader

# Instruction files a project may carry, in preference order, with the first match winning.
PROJECT_INSTRUCTION_NAMES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md")


def _as_paths(directories: Iterable[str | Path] | str | Path | None) -> tuple[Path, ...]:
    """Normalize a directory list a configuration method returns into `~`-expanded paths."""
    if directories is None:
        return ()
    if isinstance(directories, (str, Path)):
        return (Path(directories).expanduser(),)
    return tuple(Path(directory).expanduser() for directory in directories)


# Well-known user-wide instruction files, read only for a machine catalogue.
def home_instruction_paths() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".config" / "opencode" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
        home / ".agents" / "AGENTS.md",
        home / "AGENTS.md",
    )


def _project_instruction(project_directory: Optional[Path]) -> Optional[Path]:
    """The nearest instruction file at or above the project directory, stopping at the home directory."""
    if project_directory is None:
        return None
    try:
        current = project_directory.expanduser().resolve()
        home = Path.home().resolve()
    except OSError:
        return None
    while True:
        for name in PROJECT_INSTRUCTION_NAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == current.parent or current == home:
            return None
        current = current.parent


def _instruction_files_for(project_directory: Optional[Path]) -> tuple[Path, ...]:
    """The machine's instruction files: the home-wide ones first, then the project's own."""
    files = list(home_instruction_paths())
    project = _project_instruction(project_directory)
    if project is not None:
        files.append(project)
    return tuple(files)


def machine_catalogue(
    configuration: Configuration,
    working_directory: str = "",
    agent_loader: AgentLoader | None = None,
) -> FileCatalogue:
    """The catalogue a person's machine has, including the home roots.

    The daemon discovers skills and instruction files on disk and passes the paths in;
    the library never does.
    """
    return FileCatalogue(
        CatalogueRoots(
            agents=_as_paths(configuration.agent_directories_for(working_directory)),
            skills=_as_paths(configuration.skill_directories_for(working_directory)),
            memories=_as_paths(configuration.memory_directories_for(working_directory)),
            prompts=packaged_prompts_directory(),
            instruction_files=_instruction_files_for(
                Path(working_directory).expanduser() if working_directory else None
            ),
        ),
        agent_loader=agent_loader or AgentFileLoader(),
    )


def load_catalogue(configuration: Configuration, directory: str | Path) -> Any:
    """The agents, skills, memories and instructions reachable from `directory`."""
    local = Path(directory).expanduser().resolve()
    return machine_catalogue(configuration, str(local))


def load_agent(
    name: str, directory: str | Path, *, configuration: Configuration | None = None
) -> Any:
    """One named agent profile from this machine, raising with what is available when the name is unknown."""
    resolved = configuration if configuration is not None else load_configuration(seed=False)
    catalogue = load_catalogue(resolved, directory)
    profile = catalogue.agent(name)
    if profile is None:
        available = ", ".join(catalogue.agents()) or "none"
        raise LookupError(f"No agent profile named {name!r}. This machine offers: {available}.")
    return profile


__all__ = [
    "load_agent",
    "load_catalogue",
    "load_configuration",
    "machine_catalogue",
]
