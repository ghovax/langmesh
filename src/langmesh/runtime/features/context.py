"""The public context a feature is given to live.

A caller's plugin is constructed with whatever ports it declares and installed with this context
plus the hooks it implements; it never receives the runtime. The context carries only identity,
configuration, the plugin's own templates, and the bus.
"""

from __future__ import annotations

from typing import Callable

from langmesh.base.configuration import AgentConfiguration, Configuration, PromptLoader
from langmesh.base.contracts.ports import CatalogueLike
from langmesh.runtime.features.bus import PluginBus


class PluginContext:
    """What a feature is given to live: its identity, its configuration, its templates, and the bus."""

    def __init__(
        self,
        *,
        session_id: str,
        parent_session: str,
        working_directory: str,
        project_directory: str,
        agent_configuration: AgentConfiguration,
        global_configuration: Configuration,
        catalogue: CatalogueLike,
        prompts: Callable[[str], PromptLoader],
        bus: PluginBus,
    ) -> None:
        self.session_id = session_id
        self.parent_session = parent_session
        self.working_directory = working_directory
        self.project_directory = project_directory
        self.agent_configuration = agent_configuration
        self.global_configuration = global_configuration
        self.catalogue = catalogue
        self.prompts = prompts
        self.bus = bus


__all__ = ["PluginContext"]