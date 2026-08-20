"""The caller-supplied tool type accepted by session composition."""

from __future__ import annotations

from typing import TypeAlias

from langchain_core.tools import BaseTool


#: What a caller may hand to ``Session(..., tools=...)`` or ``Session.grant_tool(...)``.
ToolLike: TypeAlias = BaseTool


__all__ = ["ToolLike"]
