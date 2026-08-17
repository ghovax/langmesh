"""Asking a model until it returns structured data, with one shared shape and an attempts cap.

Two places need the same answer: the goal reviewer and the compaction summarizer each drive a
hidden session until it submits its one verdict tool; the permission reviewer and the session
titler each ask a single call for a structured tool call. Both shapes, and their attempts caps,
live here once, so no caller reimplements the loop.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from langmesh.runtime.cache_trace import cache_lane

logger = logging.getLogger(__name__)


async def collect_structured_call(
    model: Any,
    request: list,
    *,
    tool_name: str,
    schema: Any,
    attempts: int,
    cache_lane_name: str,
    reason: str,
    select: Callable[[Any], Any] | None = None,
    accept: Callable[[Any], bool] | None = None,
    on_success: Callable[[Any, Any, int], None] | None = None,
) -> Any | None:
    """The structured answer one model call must return, retried up to ``attempts`` times.

    Each attempt asks the bound model once. A response that lacks ``tool_name``, that
    ``select`` (default: the first such call) rejects, that does not validate against
    ``schema``, or that ``accept`` rejects is one failed attempt. Returns the validated
    value of the first accepted call, or ``None`` when the attempts run out.
    """
    select = select or (lambda response: _first_arguments(response, tool_name))
    with cache_lane(cache_lane_name):
        for attempt in range(1, attempts + 1):
            try:
                response = await model.ainvoke(request)
            except Exception as error:  # noqa: BLE001 — one dropped call is not an answer
                logger.warning("%s failed (attempt %d of %d): %s", reason, attempt, attempts, error)
                continue
            arguments = select(response)
            validated = None
            if arguments is not None:
                try:
                    validated = schema.model_validate(arguments)
                except Exception:  # noqa: BLE001 — a malformed answer is a failed attempt
                    validated = None
            if validated is None or (accept is not None and not accept(validated)):
                logger.warning(
                    "%s returned no usable answer (attempt %d of %d)", reason, attempt, attempts
                )
                continue
            if on_success is not None:
                on_success(validated, response, attempt)
            return validated
    return None


def _first_arguments(response: Any, tool_name: str) -> Any:
    """The arguments of the first call of ``tool_name``, or ``None``."""
    for call in getattr(response, "tool_calls", None) or []:
        if call.get("name") == tool_name:
            return call.get("args")
    return None


async def drive_verdict_session(
    *,
    attempts: int,
    reason: str,
    run_turn: Callable[[str], Awaitable[bool]],
    submitted: Callable[[], Any],
    require_submission: Callable[[], None],
    missing_instruction: Callable[[], str],
    aborted: Callable[[], bool] = lambda: False,
    initial_instruction: str = "",
    on_empty: Callable[[int, int], Any] | None = None,
    on_exhausted: Callable[[], Any] | None = None,
    on_success: Callable[[Any], Any] | None = None,
) -> Any | None:
    """Drive a hidden session until it submits its one verdict tool, capped at ``attempts``.

    Each attempt streams the session's turn with the current instruction, which starts at
    ``initial_instruction`` and is replaced by ``missing_instruction`` after each empty turn.
    A turn that ends without a submission is one failed attempt: ``require_submission``
    constrains the session down to its verdict tool. A cancelled or aborted turn returns
    ``None`` immediately; so does exhausting the attempts, after ``on_exhausted`` has a
    chance to return the terminal value. The callbacks may be synchronous or awaitable.
    """
    instruction = initial_instruction
    for attempt in range(1, attempts + 1):
        if aborted():
            return None
        if not await _maybe_await(run_turn(instruction)):
            return None
        verdict = submitted()
        if verdict is not None:
            if on_success is not None:
                await _maybe_await(on_success(verdict))
            return verdict
        if on_empty is not None:
            await _maybe_await(on_empty(attempt, attempts))
        if attempt >= attempts:
            break
        require_submission()
        instruction = missing_instruction()
    if on_exhausted is not None:
        return await _maybe_await(on_exhausted())
    logger.warning("%s gave up after %d attempts", reason, attempts)
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["collect_structured_call", "drive_verdict_session"]
