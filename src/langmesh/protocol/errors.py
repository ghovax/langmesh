"""Turn failures classified into something a client can act on, never the provider's raw text."""

from __future__ import annotations

from langmesh.base.content.model_errors import CONTEXT_OVERFLOW_CODES, ContextWindowExceeded


_BAD_REQUEST_ERRORS = frozenset({"BadRequestError"})
_RATE_LIMIT_ERRORS = frozenset({"RateLimitError"})
_AUTHENTICATION_ERRORS = frozenset({"AuthenticationError"})
_UNAVAILABLE_ERRORS = frozenset({"ServiceUnavailableError", "InternalServerError"})
_CONNECTION_ERRORS = frozenset({"APIConnectionError", "Timeout"})
_CONTEXT_WINDOW_ERRORS = frozenset({"ContextWindowExceededError"})


def _provider_error_body(error: object) -> dict:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is None:
        return {}
    try:
        parsed = response.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_error_code(error: object) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    body = _provider_error_body(error)
    nested = body.get("error") if isinstance(body.get("error"), dict) else body
    return str(nested.get("code") or "") if isinstance(nested, dict) else ""


def _provider_status_code(error: object) -> int | None:
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _safe_turn_error(error: object, had_images: bool = False) -> dict[str, object]:
    """Classify a turn-level failure without exposing raw provider or tool text."""
    status_code = _provider_status_code(error)
    provider_code = _provider_error_code(error)
    error_kind = type(error).__name__
    fields: dict[str, object] = {}
    if status_code is not None:
        fields["status"] = status_code

    # Its own failure ahead of every status test, because it is the one the harness caused.
    overflow = error if isinstance(error, ContextWindowExceeded) else None
    if (
        overflow is not None
        or error_kind in _CONTEXT_WINDOW_ERRORS
        or provider_code in CONTEXT_OVERFLOW_CODES
    ):
        window = getattr(overflow, "context_window", 0) or 0
        model = getattr(overflow, "model", "") or ""
        tokens = getattr(overflow, "tokens", None)
        return {
            **fields,
            "code": "context_window_exceeded",
            "parameters": {
                **({"window": window} if window else {}),
                **({"tokens": tokens} if tokens else {}),
                **({"model": model} if model else {}),
            },
        }
    # The provider's code classifies the failure but never reaches the wire event.

    # A rejected image almost always means the agent model is text-only, which is the actionable cause.
    if had_images and (error_kind in _BAD_REQUEST_ERRORS or status_code == 400):
        return {
            **fields,
            "code": "image_unsupported",
        }

    if error_kind in _RATE_LIMIT_ERRORS or status_code == 429:
        return {**fields, "code": "rate_limited"}
    if error_kind in _AUTHENTICATION_ERRORS or status_code in {401, 403}:
        return {**fields, "code": "authentication_failed"}
    if error_kind in _UNAVAILABLE_ERRORS or status_code in {500, 502, 503, 504}:
        return {**fields, "code": "provider_unavailable"}
    if error_kind in _CONNECTION_ERRORS or isinstance(error, TimeoutError) or status_code == 408:
        return {**fields, "code": "connection_failed"}
    if error_kind in _BAD_REQUEST_ERRORS or status_code == 400:
        # The overflow codes are tested against every error, since a streaming provider reports this mid-stream.
        return {**fields, "code": "request_rejected"}
    return {**fields, "code": "turn_failed"}
