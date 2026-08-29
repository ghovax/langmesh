"""The Bash feature: an opt-in shell tool with event-rich execution."""

from __future__ import annotations

from typing import Any

from langmesh.runtime.features import Feature
from langmesh.runtime.plugins.background.capability import BackgroundCapability
from langmesh.runtime.plugins.bash.handlers import handle_bash
from langmesh.runtime.plugins.bash.tools import bash, terminate_bash_call


class Bash(Feature):
    """Run shell commands through the session's confinement and background runner."""

    def attach(self, context, host) -> None:
        self._context = context

    def contribute_tools(self) -> list:
        """Offer Bash only when the attached profile declares it, or before attachment for discovery."""
        context = getattr(self, "_context", None)
        if context is None:
            return [bash]
        declared = getattr(context.agent_configuration, "tools_enabled", None)
        return [bash] if declared is not None and "bash" in declared else []

    def contribute_tool_handlers(self) -> dict[str, Any]:
        """Provide Bash's event-rich handler beside its schema."""
        return {"bash": handle_bash}

    def required_capabilities(self) -> tuple[type, ...]:
        """Require the runner used for both foreground settling and detached commands."""
        return (BackgroundCapability,)

    def terminate_tool_call(self, tool_call_id: str) -> bool:
        """SIGTERM the process group still running for this call."""
        return terminate_bash_call(tool_call_id)


__all__ = ["Bash", "bash"]
