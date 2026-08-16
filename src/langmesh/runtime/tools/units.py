"""The assembled set of built-in `Tool` units: schema, description, and handler in one place.

The registry owns the schemas and real implementations; the handlers own the execution. This
module is where the two meet into the composable units the runtime dispatches — the one place a
built-in tool's name, schema, and handler are joined, and the seam a caller replaces when they
want their own implementation instead.
"""

from __future__ import annotations

from langmesh.runtime.tools.execution import Tool, invoke_supplied
from langmesh.runtime.tools import registry

# Imported here so handlers (which import registry for the schema tools) never see a cycle.
from langmesh.runtime.tools.handlers import HANDLERS


def _builtin_tools() -> dict[str, Tool]:
    """Every built-in schema tool as a unit; a tool with an event-rich handler keeps it, the rest
    are dispatched through the same invoke-the-schema path a caller's tool uses."""
    tools: dict[str, Tool] = {}
    for name in registry.__all_tool_names():
        schema = getattr(registry, name, None)
        if schema is None:
            continue
        description = getattr(schema, "description", "") or ""
        handler = HANDLERS.get(name, invoke_supplied)
        tools[name] = Tool(name=name, schema=schema, description=description, handler=handler)
    return tools


#: Every built-in tool as a self-contained unit, keyed by name. The runtime copies from this map
#: and lets a caller's own tool of the same name replace the entry.
BUILTIN_TOOLS: dict[str, Tool] = _builtin_tools()


__all__ = ["BUILTIN_TOOLS"]
