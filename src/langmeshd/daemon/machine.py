"""Reading this machine: the XDG configuration, and the agents on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langmesh.base.configuration import Configuration
from langmeshd.commons.configuration_io import load_configuration  # noqa: F401 — re-exported for callers of machine.load_configuration
from langmeshd.daemon.agent_files import AgentFileLoader


def load_catalogue(configuration: Configuration, directory: str | Path) -> Any:
    """The agents, skills, memories and instructions reachable from `directory`."""
    from langmesh.base.contracts.catalogue import CatalogueRoots, FileCatalogue
    from langmesh.base.contracts.catalogue import _as_paths, packaged_prompts_directory

    local = Path(directory).expanduser().resolve()
    # The daemon discovers skills on disk and passes the paths in; the library never does.
    return FileCatalogue(
        CatalogueRoots(
            agents=_as_paths(configuration.agent_directories_for(str(local))),
            skills=_as_paths(configuration.skill_directories_for(str(local))),
            memories=_as_paths(configuration.memory_directories_for(str(local))),
            prompts=packaged_prompts_directory(),
            project_directory=local,
            include_home_instructions=True,
        ),
        agent_loader=AgentFileLoader(),
    )


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


__all__ = ["load_agent", "load_catalogue", "load_configuration"]
