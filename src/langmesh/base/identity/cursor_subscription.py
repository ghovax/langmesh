"""Compatibility surface for models-provider's Cursor account adapter."""

from models_provider import (
    cached_cursor_models,
    clear_cursor_models_cache,
    fetch_cursor_models,
    machine_time_zone,
    observed_context_window,
    record_context_window,
    request_cursor_headers,
)
from models_provider.auth import CursorTokens
from models_provider.subscriptions import (
    APPEND_PATH,
    AVAILABLE_MODELS_URL,
    CLIENT_TYPE,
    CLIENT_VERSION,
    RUN_HOSTS,
    RUN_PATH,
    STATUS_RESOURCE_EXHAUSTED,
    STATUS_UNAUTHENTICATED,
    UNKNOWN_CONTEXT_WINDOW,
)

API_BASE_URL = "https://api2.cursor.sh"
clear_subscription_models_cache = clear_cursor_models_cache
fetch_subscription_models = fetch_cursor_models
cached_subscription_models = cached_cursor_models
request_headers = request_cursor_headers


async def display_account(tokens: CursorTokens) -> str:
    return tokens.account


__all__ = [
    "API_BASE_URL", "APPEND_PATH", "AVAILABLE_MODELS_URL", "CLIENT_TYPE", "CLIENT_VERSION",
    "RUN_HOSTS", "RUN_PATH", "STATUS_RESOURCE_EXHAUSTED", "STATUS_UNAUTHENTICATED",
    "UNKNOWN_CONTEXT_WINDOW", "cached_subscription_models", "clear_subscription_models_cache",
    "display_account", "fetch_subscription_models", "machine_time_zone", "observed_context_window",
    "record_context_window", "request_headers",
]
