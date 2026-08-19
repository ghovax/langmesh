"""The locations plugin: an opt-in set of execution environments, local or over SSH.

Without this plugin a session knows only its own local folder. With it, the workspace has
named locations; a remote one is reached through one durable multiplexed SSH connection per
host, and bash is forwarded there directly. The plugin owns the whole concept — the location
table, the URI naming, and the executor map — and answers the runtime's one generic question:
where does a call with a named target actually run. It contributes no tools of its own: bash
stays a single tool, and simply receives the answer.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from langmesh.runtime.features import Feature, PluginContext, PluginHost
from langmesh.runtime.plugins.locations.resolver import (
    LocationAddress,
    executor_for,
    location_uri_for,
)


class ToolLocationError(ValueError):
    """A call named a location that is missing or unknown."""

#: The extra argument this plugin contributes to the bash tool: which location runs the command.
LOCATION_FIELD = (
    str,
    Field(
        "",
        description="The workspace location to run in, by name or URI, from the locations in your context. Empty runs on the local folder.",
    ),
)


class Locations(Feature):
    """The workspace's named execution environments and the durable SSH connections behind them."""

    def __init__(self) -> None:
        self._locations: dict[str, dict[str, Any]] = {}
        self._locations_by_name: dict[str, dict[str, Any]] = {}

    def attach(self, context: PluginContext, host: PluginHost) -> None:
        self._context = context
        self._host = host
        bundle = getattr(host, "services", None) or {}
        self.set_locations(bundle.get("locations"))

    def set_locations(self, locations: list[dict[str, Any]] | None) -> None:
        """Adopt the workspace's environments as they are now, so one added later reaches an existing session."""
        self._locations = {}
        self._locations_by_name = {}
        for entry in locations or []:
            name = str(entry.get("name") or "location")
            kind = str(entry.get("kind") or "local")
            base_directory = str(entry.get("base_directory") or "")
            host_alias = str(entry.get("host_alias") or "")
            address = LocationAddress(
                kind=kind, base_directory=base_directory, host_alias=host_alias
            )
            try:
                uri = str(entry.get("uri") or location_uri_for(address))
            except Exception:
                uri = f"{kind}:{base_directory}"
            executor = entry.get("executor") or executor_for(address)
            resolved = {
                "uri": uri,
                "name": name,
                "kind": kind,
                "base_directory": base_directory,
                "executor": executor,
            }
            self._locations[uri] = resolved
            self._locations_by_name[name] = resolved

    def contribute_tools(self) -> list:
        """This plugin adds no tools of its own; it extends bash's schema with ``location``."""
        return []

    def contribute_schema_fields(self, tool_name: str) -> dict:
        """The ``location`` selector this plugin contributes to the bash tool."""
        if tool_name != "bash":
            return {}
        return {"location": LOCATION_FIELD}

    def invoke(self, name: str, *args, **kwargs):
        """The capabilities the runtime asks for: resolving a call's execution target, by name."""
        if name == "resolve_execution":
            tool_name = args[0] if args else ""
            if tool_name != "bash":
                return None
            arguments = args[1] if len(args) > 1 else {}
            location_value = str(arguments.get("location") or "")
            if not location_value:
                return None
            resolved = self._resolve(location_value)
            if resolved is None:
                raise ToolLocationError(
                    f"The location {location_value!r} is unknown. "
                    f"Available: {', '.join(sorted(self._locations_by_name)) or 'the local folder'}"
                )
            return (resolved["executor"], resolved["base_directory"])
        if name == "set_locations":
            self.set_locations(args[0] if args else None)
            return True
        return None

    def compose_context(self, context: dict) -> None:
        """The locations as the model sees them: the name to pass, and enough to choose the right one."""
        context["locations"] = [
            {
                "location": entry["uri"],
                "name": entry["name"],
                "kind": entry["kind"],
                "base_directory": entry["base_directory"],
                "writable": entry["kind"] == "remote"
                or bool(getattr(self._host, "writes_anywhere", False)),
            }
            for entry in self._locations.values()
        ]

    def _resolve(self, location_value: str) -> dict[str, Any] | None:
        """The location named by URI or name, or ``None`` when unknown."""
        return self._locations.get(location_value) or self._locations_by_name.get(
            location_value
        )


__all__ = ["Locations"]
