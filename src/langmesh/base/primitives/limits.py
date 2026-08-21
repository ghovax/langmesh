"""The plain limits every tool's size, count and timing budget is read from.

No scaling families and no context-window math: a ``Limits`` value holds fixed numbers,
built once from the configuration the daemon loads. Call sites read the current value
through ``current_limits()``; the tokenizer-backed text helpers and the surface-settling
poll live here too.
"""

from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar, cast


@dataclass(frozen=True)
class Limits:
    """Every budget, as a plain value. Each field is configurable; these are the shipped defaults.

    Token and item budgets are powers of two, rounded up from a natural number, because the
    context window they derive from is itself a power of two.
    """

    # Text budgets, in tokens (powers of two)
    output_tokens: int = 16_384
    fetch_tokens: int = 32_768
    upstream_error_detail_tokens: int = 256

    # Listing budgets, in item counts (powers of two)
    web_search_maximum: int = 16
    web_exchanges: int = 256
    web_websockets: int = 32
    web_websocket_frames: int = 256

    # Timeouts, in seconds
    action_timeout: float = 5.0
    navigation_timeout: float = 20.0
    snapshot_timeout: float = 10.0
    browser_authorization: float = 90.0
    drag_timeout: float = 8.0
    read_text_timeout: float = 10.0
    frame_resolve_timeout: float = 2.0
    sigterm_grace: float = 3.0
    bash_sync_window: float = 60.0
    slow_tool_sync_window: float = 10.0
    web_search_sync_window: float = 10.0
    accessibility_messaging: float = 2.0
    control_script: float = 120.0
    surface_guard_margin: float = 30.0
    open_url: float = 5.0
    model_silence_give_up: float = 180.0

    # Autonomous continuation budgets, in turns
    task_continuation_turns: int = 16

    # Surface settling, in seconds and reads
    settle_poll_seconds: float = 0.05
    settle_give_up_seconds: float = 1.5
    settle_stable_reads: int = 2

    # Screen input pacing and retrieval (fixed shapes and pixel sizes; sizes are powers of two)
    type_chunk_size: int = 32
    type_chunk_interval: float = 0.005
    drag_steps: int = 16
    drag_step_interval: float = 0.01
    click_interval: float = 0.01
    focus_settle: float = 0.03
    scroll_amount_pixels: int = 512
    accessibility_walk_budget: float = 3.0
    accessibility_ready_probe: float = 0.4
    accessibility_prewarm_interval: float = 0.4
    accessibility_ready_backoff: float = 0.2
    find_rephrasing_similarity: float = 0.45
    find_near_weight: float = 0.5
    find_anchor_margin: float = 0.02
    find_candidates: int = 8
    find_one_margin: float = 0.20
    find_many_ceiling: int = 64
    find_relevance_floor: float = 0.25

    # Other process and identity budgets
    session_title_attempts: int = 4
    permission_reviewer_attempts: int = 4
    model_catalogue_ttl: float = 60.0
    credential_refresh_leeway: float = 300.0
    oauth_poll_interval: float = 1.0
    oauth_poll_ceiling: float = 10.0
    oauth_poll_give_up: float = 300.0
    subscription_resume_ttl: float = 1_800.0
    file_url_ttl: float = 600.0
    mcp_connect: float = 20.0
    card_resolve: float = 20.0


#: The process default, with an optional task-local override for concurrent embedded sessions.
_default = Limits()
_bound: contextvars.ContextVar[Limits | None] = contextvars.ContextVar(
    "langmesh_limits", default=None
)


def set_limits(limits: Limits) -> None:
    """Adopt the process-wide limits the host configuration asks for."""
    global _default
    _default = limits


def bind_limits(limits: Limits) -> contextvars.Token[Limits | None]:
    """Override limits in the current context until the returned token is reset."""
    return _bound.set(limits)


def reset_limits(token: contextvars.Token[Limits | None]) -> None:
    """Restore the limits binding represented by ``token``."""
    _bound.reset(token)


def current_limits() -> Limits:
    """The limits in force, defaulting to the shipped values when none were loaded."""
    return _bound.get() or _default


def limits_from_configuration(policy: object) -> Limits:
    """The limits a configuration section asks for, with plain overrides by field name."""
    configured = getattr(policy, "limits", None)
    values = (
        {key: value for key, value in configured.items() if hasattr(Limits, key)}
        if isinstance(configured, dict)
        else {}
    )
    return Limits(**cast(dict[str, Any], values))


# Tokenizer-backed text budgeting, because a fixed characters-per-token ratio is wrong in both directions.
_ENCODING_NAME = "o200k_base"  # the current-generation general tokenizer; a good cross-model proxy

_encoding = None


def _bundled_vocabulary() -> None:
    """Point tiktoken at a frozen build's bundled vocabulary before it is imported, since the cache directory is read at fetch time."""
    import sys

    if not getattr(sys, "frozen", False) or "TIKTOKEN_CACHE_DIR" in os.environ:
        return
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "langmesh" / "tokenizer"
    if bundled.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(bundled)


def _get_encoding():
    """The encoding every budget here is measured with, raising rather than guessing when it cannot be loaded."""
    global _encoding
    if _encoding is None:
        _bundled_vocabulary()
        import tiktoken

        _encoding = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoding


def count_tokens(text: str) -> int:
    """How many tokens `text` is, by the same encoding `clip_to_tokens` cuts on."""
    return len(_get_encoding().encode(text, disallowed_special=()))


def clip_to_tokens(text: str, budget: int) -> tuple[str, bool]:
    """Clip `text` to at most `budget` tokens on a real token boundary, reporting whether it was truncated."""
    budget = max(1, budget)
    encoding = _get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= budget:
        return text, False
    return encoding.decode(tokens[:budget]), True


_Reading = TypeVar("_Reading")


def settle(
    read: Callable[[], _Reading],
    *,
    interval: float | None = None,
    ceiling: float | None = None,
    stable_reads: int | None = None,
) -> _Reading:
    """Poll a surface until it stops changing, returning once it reads the same value repeatedly or the ceiling elapses."""
    limits = current_limits()
    step = max(0.001, interval if interval is not None else limits.settle_poll_seconds)
    limit = max(0.0, ceiling if ceiling is not None else limits.settle_give_up_seconds)
    needed = max(1, stable_reads if stable_reads is not None else limits.settle_stable_reads)
    deadline = time.monotonic() + limit
    latest = read()
    repeats = 1
    while time.monotonic() < deadline:
        time.sleep(step)
        current = read()
        repeats = repeats + 1 if current == latest else 1
        latest = current
        if repeats >= needed:
            break
    return latest


__all__ = [
    "Limits",
    "bind_limits",
    "clip_to_tokens",
    "count_tokens",
    "current_limits",
    "limits_from_configuration",
    "reset_limits",
    "set_limits",
    "settle",
]
