"""Compatibility surface for models-provider's ChatGPT account adapter."""

from models_provider import (
    cached_chatgpt_models,
    capture_usage_headers,
    clear_chatgpt_models_cache,
    clear_usage_snapshot,
    fetch_chatgpt_models,
    get_usage_snapshot,
    request_chatgpt_headers,
    set_usage_snapshot,
)
from models_provider.subscriptions import MODELS_URL, ORIGINATOR, RESPONSES_URL

CLIENT_VERSION = "0.144.4"
USER_AGENT = ""
cached_subscription_models = cached_chatgpt_models
clear_subscription_models_cache = clear_chatgpt_models_cache
fetch_subscription_models = fetch_chatgpt_models
request_headers = request_chatgpt_headers

__all__ = [
    "CLIENT_VERSION", "MODELS_URL", "ORIGINATOR", "RESPONSES_URL", "USER_AGENT",
    "cached_subscription_models", "capture_usage_headers", "clear_subscription_models_cache",
    "clear_usage_snapshot", "fetch_subscription_models", "get_usage_snapshot", "request_headers",
    "set_usage_snapshot",
]
