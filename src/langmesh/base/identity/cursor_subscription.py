"""What a Cursor subscription is as an account: its endpoints, its headers, and the models a plan serves."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Optional

import httpx

from langmesh.base.identity.cursor_credentials import (
    API_BASE_URL,
    CursorAuthError,
    CursorTokens,
    _account_of,
    _is_display_account,
    save_tokens,
    valid_tokens,
)
from langmesh.base.primitives.limits import current_limits

logger = logging.getLogger(__name__)


RUN_PATH = "/agent.v1.AgentService/RunSSE"
APPEND_PATH = "/aiserver.v1.BidiService/BidiAppend"
# Cursor moves this service between backends, so the hosts are tried in order when a run fails before producing anything.
AGENT_PRIVACY_URL = "https://agent.api5.cursor.sh"
AGENT_OPEN_URL = "https://agentn.api5.cursor.sh"
RUN_HOSTS = (API_BASE_URL, AGENT_PRIVACY_URL, AGENT_OPEN_URL)
# The two model endpoints, each knowing half the answer: which models a plan serves, and how large each window is.
# AgentService/GetUsableModels is what current Cursor CLI clients (oh-my-pi, pi-cursor) call for discovery.
USABLE_MODELS_URL = f"{API_BASE_URL}/agent.v1.AgentService/GetUsableModels"
AVAILABLE_MODELS_URL = f"{API_BASE_URL}/aiserver.v1.AiService/AvailableModels"
GET_ME_URL = f"{API_BASE_URL}/aiserver.v1.DashboardService/GetMe"

# A real Cursor client build, the newest any working client is known to send.
CLIENT_VERSION = "cli-2026.02.13-41ac335"
CLIENT_TYPE = "cli"

# The gRPC status codes worth naming: spent usage, and an unauthenticated call.
STATUS_RESOURCE_EXHAUSTED = 8
STATUS_UNAUTHENTICATED = 16


def machine_time_zone() -> str:
    """The machine's IANA zone name, preferring `TZ` and falling back to the `/etc/localtime` link."""
    if configured := os.environ.get("TZ", "").strip():
        return configured
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return time.tzname[0] if time.tzname else "UTC"


def _checksum(access_token: str) -> str:
    """The checksum the service expects alongside the bearer token, which obfuscates a timestamp rather than signing anything."""
    slot = int(time.time() // 1800) * 1800
    stamp = (slot * 1000) // 1_000_000
    obfuscated = bytearray(stamp.to_bytes(6, "big"))
    previous = 165
    for index in range(len(obfuscated)):
        obfuscated[index] = ((obfuscated[index] ^ previous) + index) & 0xFF
        previous = obfuscated[index]
    prefix = base64.urlsafe_b64encode(bytes(obfuscated)).rstrip(b"=").decode()
    segments = access_token.split(".")
    payload_digest = (
        hashlib.sha256(segments[1].encode()).hexdigest()[:8] if len(segments) > 1 else "00000000"
    )
    token_digest = hashlib.sha256(access_token.encode()).hexdigest()[:8]
    return f"{prefix}{payload_digest}/{token_digest}"


def request_headers(tokens: CursorTokens, request_id: str) -> dict[str, str]:
    """The header set the transport was tested with, since the two transports were exercised with different ones."""
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        # Not Connect's own content type: this service answers 415 to it and expects gRPC-web framing.
        "Content-Type": "application/grpc-web+proto",
        "x-cursor-checksum": _checksum(tokens.access_token),
        "x-cursor-client-version": CLIENT_VERSION,
        "x-cursor-client-type": CLIENT_TYPE,
        "x-cursor-timezone": machine_time_zone(),
        # Privacy mode: do not retain this conversation for training.
        "x-ghost-mode": "true",
        # Without this the service may park a reply in the blob store, and a turn that streams nothing looks failed.
        "x-cursor-streaming": "true",
        "x-request-id": request_id,
    }


# Live per-account model discovery, which is authoritative where the static catalog is only an offline superset.
_models_cache: Optional[tuple[float, dict[str, dict[str, Any]]]] = None
_models_cache_lock = asyncio.Lock()

# The floor for a model named without a window, chosen to under-promise rather than over-promise.
UNKNOWN_CONTEXT_WINDOW = 200_000

# What the server itself said a model's window was, learned from a checkpoint and so the most authoritative source.
_observed_context_windows: dict[str, int] = {}


def record_context_window(model_id: str, maximum_tokens: int) -> None:
    """Remember a window the server reported, keeping the largest seen for a model."""
    if maximum_tokens > _observed_context_windows.get(model_id, 0):
        _observed_context_windows[model_id] = maximum_tokens


def observed_context_window(model_id: str) -> int:
    """The largest window the server reported for a model, or 0, as an accessor rather than a shared dictionary."""
    return _observed_context_windows.get(model_id, 0)


# Cursor's model ids carry the reasoning effort, which is why this provider ignores the effort setting.
_EFFORT_SUFFIX = re.compile(r"-(high|medium|low|max|xhigh)$")


def _display_name(entry: dict[str, Any], model_id: str) -> str:
    """A model's label, with its effort named, since Cursor gives every effort variant the same display name."""
    name = model_id
    for key in ("displayName", "displayNameShort", "displayModelId"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    effort = _EFFORT_SUFFIX.search(model_id)
    if effort and effort.group(1) not in name.lower():
        return f"{name} ({effort.group(1)})"
    return name


def _token_limit(value: Any) -> int:
    """A parameter value as a token count, reading the three spellings its interface uses."""
    text_value = str(value or "").strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km])?", text_value)
    if match is None:
        return 0
    scale = {"k": 1_000, "m": 1_000_000}.get(match.group(2) or "", 1)
    return round(float(match.group(1)) * scale)


async def _connect_json(url: str, body: dict, tokens: CursorTokens) -> dict:
    """One unary call over Connect's JSON encoding, which these two model endpoints accept."""
    headers = {
        **request_headers(tokens, str(uuid.uuid4())),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "connect-protocol-version": "1",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    return payload if isinstance(payload, dict) else {}


@dataclass
class _Variant:
    """How the backend routes one model variant: its server name, its max-mode flag, and the parameters that pick it."""

    server_model: str
    maximum_mode: bool
    parameters: tuple[tuple[str, str], ...]
    context: int


async def _fetch_variants(tokens: CursorTokens) -> dict[str, _Variant]:
    """Every model variant the account can reach, which is the only place both the window and the selection are stated."""
    payload = await _connect_json(
        AVAILABLE_MODELS_URL,
        {
            "isNightly": False,
            "excludeMaxNamedModels": True,
            "additionalModelNames": [],
            "useModelParameters": True,
            "useReactModelPicker": True,
        },
        tokens,
    )
    variants: dict[str, _Variant] = {}

    def remember(key: str, variant: _Variant) -> None:
        if not key:
            return
        existing = variants.get(key)
        if existing is None:
            variants[key] = variant
        elif variant.context and existing.context and variant.context < existing.context:
            variants[key] = variant

    for entry in payload.get("models") or []:
        if not isinstance(entry, dict) or not (base_name := entry.get("name")):
            continue
        server_model = str(entry.get("serverModelName") or base_name)
        for raw_variant in entry.get("variants") or []:
            if not isinstance(raw_variant, dict):
                continue
            values = {
                str(parameter.get("id")): str(parameter.get("value"))
                for parameter in raw_variant.get("parameterValues") or []
                if isinstance(parameter, dict) and parameter.get("id") is not None
            }
            variant = _Variant(
                server_model=server_model,
                maximum_mode=raw_variant.get("isMaxMode") is True,
                parameters=tuple(sorted(values.items())),
                context=_token_limit(values.get("context")),
            )
            remember(str(base_name), variant)
    return variants


def _variant_for(model_id: str, variants: dict[str, _Variant]) -> Optional[_Variant]:
    """The variant for a run id: an exact match on a slug, else the longest base name the id starts with."""
    if (exact := variants.get(model_id)) is not None:
        return exact
    candidates = [name for name in variants if model_id.startswith(name)]
    return variants[max(candidates, key=len)] if candidates else None


def _entries_from_usable(listing: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in listing.get("models") or [] if isinstance(entry, dict)]


def _entries_from_variants(variants: dict[str, _Variant]) -> list[dict[str, Any]]:
    """AvailableModels names, used when GetUsableModels is empty or refuses JSON."""
    return [{"modelId": name, "displayName": name} for name in variants]


def _result_from_entries(
    entries: list[dict[str, Any]], variants: dict[str, _Variant]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        model_id = entry.get("modelId") or entry.get("displayModelId")
        if not model_id:
            continue
        variant = _variant_for(str(model_id), variants)
        result[str(model_id)] = {
            "name": _display_name(entry, str(model_id)),
            "context": variant.context if variant else 0,
            "variant": None
            if variant is None
            else {
                "server_model": variant.server_model,
                "maximum_mode": variant.maximum_mode,
                "parameters": variant.parameters,
            },
        }
    return result


async def fetch_subscription_models() -> dict[str, dict[str, Any]]:
    """The account's live model list, from the two endpoints that each know half of it."""
    global _models_cache
    ttl = current_limits().model_catalogue_ttl
    if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
        return _models_cache[1]
    async with _models_cache_lock:
        if _models_cache is not None and time.monotonic() - _models_cache[0] < ttl:
            return _models_cache[1]
        result: dict[str, dict[str, Any]] = {}
        try:
            tokens = await valid_tokens()
            entries: list[dict[str, Any]] = []
            try:
                listing = await _connect_json(
                    USABLE_MODELS_URL, {"customModelIds": []}, tokens
                )
                entries = _entries_from_usable(listing)
            except (httpx.HTTPError, ValueError, TypeError) as error:
                logger.warning("Cursor GetUsableModels failed: %s", error)
            try:
                variants = await _fetch_variants(tokens)
            except (httpx.HTTPError, ValueError, TypeError) as error:
                logger.warning("Cursor AvailableModels failed: %s", error)
                variants = {}
            if not entries:
                entries = _entries_from_variants(variants)
            result = _result_from_entries(entries, variants)
        except CursorAuthError as error:
            logger.info("Cursor models unavailable: %s", error)
            result = {}
        _models_cache = (time.monotonic(), result)
        return result


def _email_from_profile(payload: dict[str, Any]) -> str:
    """An address from Dashboard GetMe, walking the few shapes that endpoint has used."""
    for key in ("email", "userEmail", "cachedEmail"):
        value = payload.get(key)
        if isinstance(value, str) and _is_display_account(value):
            return value.strip()
    user = payload.get("user")
    if isinstance(user, dict):
        value = user.get("email")
        if isinstance(value, str) and _is_display_account(value):
            return value.strip()
    return ""


async def display_account(tokens: CursorTokens) -> str:
    """A label fit for the sign-in row, preferring an email over an Auth0 subject."""
    current = tokens.account if _is_display_account(tokens.account) else _account_of(
        tokens.access_token
    )
    if current:
        if current != tokens.account:
            await asyncio.to_thread(save_tokens, replace(tokens, account=current))
        return current
    try:
        profile = await _connect_json(GET_ME_URL, {}, tokens)
        email = _email_from_profile(profile)
    except (httpx.HTTPError, ValueError, TypeError) as error:
        logger.info("Cursor GetMe failed: %s", error)
        return ""
    if email:
        await asyncio.to_thread(save_tokens, replace(tokens, account=email))
    return email


def cached_subscription_models() -> dict[str, dict[str, Any]]:
    """The last live list fetched, without a network round-trip, for sync callers wanting the freshest known value."""
    return _models_cache[1] if _models_cache is not None else {}


def clear_subscription_models_cache() -> None:
    """Drop the cached list so a fresh sign-in or sign-out shows immediately rather than waiting out the TTL."""
    global _models_cache
    _models_cache = None
