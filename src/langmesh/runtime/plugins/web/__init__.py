"""The web plugin: searching the web, fetching pages, and downloading files.

The library core never names the web: the search, fetch and download tools and their
event-rich handlers are this plugin's, contributed through the feature seam when the host
composes it. A bare library embedding has no web surface at all.
"""

from __future__ import annotations

from langmesh.runtime.features import Feature, PluginContext
from langmesh.runtime.plugins.web.tools import download_file, fetch_url, search_web


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
            return [search_web, fetch_url, download_file]
        declared = getattr(context, "agent_configuration", None)
        enabled = getattr(declared, "tools_enabled", None) or []
        return [
            tool
            for tool in (search_web, fetch_url, download_file)
            if tool.name in enabled
        ]

    def invoke(self, name: str, *args, **kwargs):
        """The capability the runtime asks for: a tool's event-rich handler, by tool name."""
        if name != "tool_handler" or not args:
            return None
        tool_name = args[0]
        if tool_name == "search_web":
            from langmesh.runtime.plugins.web.handlers import handle_search_web

            return handle_search_web
        if tool_name == "download_file":
            from langmesh.runtime.plugins.web.handlers import handle_download_file

            return handle_download_file
        return None


__all__ = ["Web"]
