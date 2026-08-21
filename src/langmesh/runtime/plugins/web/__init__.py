"""The web plugin: searching the web, fetching pages, and downloading files.

The library core never names the web: the search, fetch and download tools and their
event-rich handlers are this plugin's, contributed through the feature seam when the host
composes it. A bare library embedding has no web surface at all.
"""

from __future__ import annotations

from typing import Any

from langmesh.runtime.features import BackgroundCapability, Feature, PluginContext
from langmesh.runtime.plugins.web.tools import download, fetch_url, search_web


class Web(Feature):
    """Searching, fetching and downloading over the web."""

    def attach(self, context: PluginContext, host=None) -> None:
        self._context = context

    def contribute_tools(self) -> list:
        """The web tools, for a profile that declared any of them.

        The schema map the host builds for name resolution has no context yet, so an
        unattached plugin still offers its tools."""
        context = getattr(self, "_context", None)
        if context is None:
            return [search_web, fetch_url, download]
        declared = getattr(context, "agent_configuration", None)
        enabled = getattr(declared, "tools_enabled", None) or []
        return [tool for tool in (search_web, fetch_url, download) if tool.name in enabled]

    def contribute_tool_handlers(self) -> dict[str, Any]:
        """Provide the event-rich handlers beside their schemas."""
        from langmesh.runtime.plugins.web.handlers import handle_search_web

        return {
            "search_web": handle_search_web,
        }

    def required_capabilities(self) -> tuple[type, ...]:
        """Require the runner that owns slow searches, fetches, and downloads."""
        return (BackgroundCapability,)


__all__ = ["Web"]
