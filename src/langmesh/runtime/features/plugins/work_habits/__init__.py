"""The work-habits plugin: the user's habitual shell commands, injected when the host enables it.

The library core never mines the user's shell history: that is a personal-environment feature
the daemon opts into. This plugin reads the history and contributes the habitual commands to
the turn's model-facing context, so a bare library embedding stays free of it.
"""

from __future__ import annotations

from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.prompt_environment import _shell_command_usage


class WorkHabits(Feature):
    """The user's habitual commands, as a nested histogram, contributed to the machine context."""

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host

    def compose_context(self, context: dict) -> None:
        """The habitual commands as the model sees them, under ``work_habits``."""
        context["work_habits"] = _shell_command_usage()


__all__ = ["WorkHabits"]
