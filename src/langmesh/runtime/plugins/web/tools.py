"""The web plugin's own tools: search, fetch and download, defined where the plugin lives."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from langchain.tools import tool

from langmesh.base.content.prompts import PackagePromptLoader
from langmesh.base.primitives.identifiers import new_id
from langmesh.base.primitives.serialization import compact
from langmesh.base.primitives.limits import current_limits
from langmesh.runtime.background import current_background_jobs, current_tool_call_id
from langmesh.runtime.features import BackgroundCapability
from langmesh.runtime.tools import context as tool_context, fetching
from langmesh.runtime.tools.execution import current_tool_services


#: The tools' model-facing descriptions, read from this plugin's own prompts directory.
_DESCRIPTIONS = PackagePromptLoader(Path(__file__).parent / "prompts")


@tool
async def search_web(
    *,
    query: str,
    result_count: int = 5,
    **kwargs: Any,
) -> str:
    """Search the web."""
    client = tool_context.current().exa_client
    if client is None:
        return compact(
            {
                "code": "web_search_error",
                "status": "error",
                "message": "Web search is not configured.",
            }
        )

    # Mint the id up front, so a delivered result can be matched to the search that started it.
    job_id = new_id("search")

    async def run() -> str:
        try:
            results = await asyncio.to_thread(
                client.search,
                query,
                num_results=min(result_count, current_limits().web_search_maximum),
                contents={"text": True},
            )
            entries = []
            for result in results.results:
                entry = {"title": result.title, "url": result.url}
                if result.text:
                    entry["summary"] = result.text
                if result.published_date:
                    entry["published_date"] = result.published_date
                entries.append(entry)
            payload = compact(
                {
                    "code": "web_search_completed",
                    "status": "ok",
                    "job_id": job_id,
                    "query": query,
                    "results": entries,
                }
            )
            return payload
        except Exception as exception:
            payload = compact(
                {
                    "code": "web_search_error",
                    "status": "error",
                    "job_id": job_id,
                    "message": str(exception),
                }
            )
            return payload

    jobs = current_background_jobs()
    jobs.spawn(
        "search_web",
        run(),
        identifier=job_id,
        arguments={
            "query": query,
            "explanation": kwargs.get("explanation", ""),
            "result_count": result_count,
        },
        # A search outliving the turn keeps running, so its result still lands and wakes the agent.
        detached=True,
    )
    # A short inline window, so the common case returns results rather than a pending handle.
    settled = await jobs.settle_inline(job_id, current_limits().web_search_sync_window)
    if settled is not None:
        return settled.result
    # No path or fetch-looking handle in the acknowledgement: the id is the only thing the model needs.
    return compact(
        {
            "code": "web_search_started",
            "status": "running",
            "job_id": job_id,
        }
    )


@tool
async def fetch_url(
    *,
    url: str,
    format: Literal["markdown", "text", "html"] = "markdown",
    timeout: float = 10.0,
    hard_deadline: float = 30,
    background: bool = False,
) -> str:
    """Fetch a page; described in descriptions/fetch_url.md."""
    services = current_tool_services()
    runner = services.features.require(BackgroundCapability).runner
    sync_window = float(timeout or current_limits().slow_tool_sync_window)
    configured = tool_context.current().fetch_timeout_seconds
    hard_deadline = int(hard_deadline or configured or 30)
    job_identifier = runner.spawn(
        "fetch_url",
        fetching.fetch_url(url, format, hard_deadline, services.artifacts),
        tool_call_identifier=current_tool_call_id(),
        detached=background,
    )
    if not background:
        completion = await runner.settle_inline(job_identifier, sync_window)
        if completion is not None:
            return completion.result
    return compact({"code": "fetch_url_started", "status": "running", "job_id": job_identifier})


@tool
async def download(
    *,
    url: str,
    timeout: float = 10.0,
    hard_deadline: float = 120,
    background: bool = False,
) -> str:
    """Download raw bytes into the session's artifact store."""
    services = current_tool_services()
    sync_window = float(timeout or current_limits().slow_tool_sync_window)
    configured = tool_context.current().download_timeout_seconds
    hard_deadline = int(hard_deadline or configured or 120)
    runner = services.features.require(BackgroundCapability).runner
    job_identifier = runner.spawn(
        "download",
        fetching.download(url, services.artifacts, hard_deadline),
        tool_call_identifier=current_tool_call_id(),
        detached=background,
    )
    if not background:
        completion = await runner.settle_inline(job_identifier, sync_window)
        if completion is not None:
            return completion.result
    return compact({"code": "download_started", "status": "running", "job_id": job_identifier})


# The tools' model-facing descriptions are this plugin's own files, applied once at import.
search_web.description = _DESCRIPTIONS.load("search_web", {}).strip() or search_web.description
fetch_url.description = _DESCRIPTIONS.load("fetch_url", {}).strip() or fetch_url.description
download.description = _DESCRIPTIONS.load("download", {}).strip() or download.description


__all__ = ["download", "fetch_url", "search_web"]
