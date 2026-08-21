"""The web plugin's event-rich search handler."""

from __future__ import annotations

from typing import Any, AsyncIterator

from langmesh.runtime.background import bind_background_jobs, unbind_background_jobs
from langmesh.runtime.features import BackgroundCapability
from langmesh.runtime.internals import _maybe_json
from langmesh.runtime.tools.execution import ToolExecution
from langmesh.runtime.turn_events import ToolResult


async def handle_search_web(execution: ToolExecution) -> AsyncIterator[Any]:
    services = execution.services
    from langmesh.runtime.plugins.web.tools import search_web as search_web_tool

    background_token = bind_background_jobs(services.features.require(BackgroundCapability).runner)
    try:
        result = await search_web_tool.ainvoke(execution.arguments)
    finally:
        unbind_background_jobs(background_token)
    result_data = _maybe_json(result)
    model_guidance = ""
    if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
        model_guidance = services.prompt_loader.load("web_search_started_note", {})
    yield ToolResult(
        id=execution.call_id,
        name=execution.name,
        result=result_data,
        model_guidance=model_guidance,
    )
