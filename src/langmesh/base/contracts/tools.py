"""Caller-supplied tools, described to the model by message rather than by schema binding.

The provider cache prefix is the system prompt, the tool schemas, then every turn. Binding a
new tool into the schema mid-session moves the tools segment and bursts the cache, so a tool
granted to a session is instead described by an appended conversation message: the model reads
what it is and how to call it, the harness dispatches the call by name, and the schema segment
never changes — at creation or at any later moment. A grant is therefore always append-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class ToolGrant:
    """One tool granted to a session.

    The tool becomes dispatchable in the session, and its description (with the argument
    schema) is appended to the conversation as a message, so the model learns to call it
    without the bound tool schema changing. Grants may be given at session creation or
    appended at any later moment; both are append-only, so the provider cache prefix holds.
    """

    tool: BaseTool


#: What a caller may hand to ``Session(..., tools=...)`` or ``Session.grant_tool(...)``.
ToolLike: TypeAlias = BaseTool | ToolGrant


def as_tool_grants(tools) -> list[ToolGrant]:
    """Normalize a mixed sequence of bare tools and grants to grants only."""
    return [tool if isinstance(tool, ToolGrant) else ToolGrant(tool=tool) for tool in tools]


__all__ = ["ToolGrant", "ToolLike", "as_tool_grants"]
