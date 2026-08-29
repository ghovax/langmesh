"""Asking a model until it returns structured data, with one shared shape.

Two places need the same answer: the goal reviewer and the compaction summarizer each drive a
hidden session until it submits its one verdict tool; the permission reviewer and the session
titler each ask a single call for a structured tool call. Both shapes live here once, so no
caller reimplements the loop. A driven session receives explicit attempt and time budgets, so a
malformed or stalled model response cannot hold the parent session forever.
"""

from __future__ import annotations

import asyncio
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
    retry_reminder: Callable[[Any], Any | None] | None = None,
) -> Any | None:
    """The structured answer one model call must return, retried up to ``attempts`` times.

    Each attempt asks the bound model once. A response that lacks ``tool_name``, that
    ``select`` (default: the first such call) rejects, that does not validate against
    ``schema``, or that ``accept`` rejects is one failed attempt. ``retry_reminder``
    may return a message appended before the next attempt, so the model learns why its
    last answer was rejected. Returns the validated value of the first accepted call,
    or ``None`` when the attempts run out.
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
                if retry_reminder is not None:
                    reminder = retry_reminder(response)
                    if reminder is not None:
                        request = [*request, reminder]
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
    run_turn: Callable[[str], Awaitable[bool]],
    attempts: int,
    timeout_seconds: float,
    submitted: Callable[[], Any],
    require_submission: Callable[[], None],
    missing_instruction: Callable[[], str],
    aborted: Callable[[], bool] = lambda: False,
    initial_instruction: str = "",
    on_empty: Callable[[int], Any] | None = None,
    on_success: Callable[[Any], Any] | None = None,
) -> Any | None:
    """Drive a hidden session until it submits its one verdict tool or its budget ends.

    Each attempt streams the session's turn with the current instruction, which starts at
    ``initial_instruction`` and is replaced by ``missing_instruction`` after each empty turn.
    A turn that ends without a submission is one empty attempt: ``require_submission``
    constrains the session down to its verdict tool, and it is asked again until ``attempts``
    or ``timeout_seconds`` ends the operation. A cancelled, aborted, or timed-out turn returns
    ``None`` immediately. ``on_empty`` receives the attempt number. The callbacks may be
    synchronous or awaitable.
    """
    if attempts < 1:
        raise ValueError("verdict attempts must be positive")
    if timeout_seconds <= 0:
        raise ValueError("verdict timeout must be positive")
    instruction = initial_instruction
    for attempt in range(1, attempts + 1):
        if aborted():
            return None
        try:
            async with asyncio.timeout(timeout_seconds):
                ran = await _maybe_await(run_turn(instruction))
        except TimeoutError:
            logger.warning(
                "structured verdict timed out (attempt %d of %d after %.1fs)",
                attempt,
                attempts,
                timeout_seconds,
            )
            return None
        if not ran:
            return None
        verdict = submitted()
        if verdict is not None:
            if on_success is not None:
                await _maybe_await(on_success(verdict))
            return verdict
        if on_empty is not None:
            await _maybe_await(on_empty(attempt))
        if attempt == attempts:
            logger.warning("structured verdict exhausted %d attempts", attempts)
            return None
        require_submission()
        instruction = missing_instruction()
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["collect_structured_call", "drive_verdict_session"]
