"""The provider sign-in surface: one generic route per provider, keyed by name.

A provider is a resource, not a route: `GET /auth/{provider}` reads the sign-in state, `POST
`/auth/{provider}/start`` begins the OAuth flow, `DELETE /auth/{provider}` signs out. Adding a
provider means adding an entry to the table, never a route.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from langmesh.base.identity.credentials import ChatGPTLoginFlow, clear_tokens, load_tokens
from langmesh.base.identity.cursor_credentials import CursorLoginFlow
from langmesh.base.identity.cursor_credentials import clear_tokens as cursor_clear_tokens
from langmesh.base.identity.cursor_credentials import load_tokens as cursor_load_tokens
from langmesh.base.identity import cursor_subscription
from langmesh.base.identity.subscription import (
    clear_subscription_models_cache,
    clear_usage_snapshot,
    get_usage_snapshot,
)
from langmeshd.commons import state
from langmeshd.commons.services.broadcast import _publish_broadcast
from langmeshd.commons.services.workspaces import _reset_all_runtimes

router = APIRouter()


@dataclass(frozen=True)
class _ProviderAuth:
    """One subscription provider's sign-in: its flow, credential store, and state slot."""

    flow_kind: type
    load: Callable[[], Any]
    clear: Callable[[], None]
    in_flight: str
    clear_caches: Callable[[], None]
    account: Callable[[Any], str] = staticmethod(lambda _tokens: "")
    clear_resumptions: Callable[[], None] = staticmethod(lambda: None)

    async def status(self) -> dict:
        tokens = await asyncio.to_thread(self.load)
        return {
            "signed_in": tokens is not None,
            "account": self.account(tokens) if tokens is not None else "",
            "usage": get_usage_snapshot()
            if tokens is not None and self.flow_kind is ChatGPTLoginFlow
            else None,
        }

    async def start(self) -> dict:
        pending = getattr(state, self.in_flight, None)
        if pending is not None:
            await pending.close()
        flow = self.flow_kind()
        try:
            await flow.start()
        except OSError as error:
            raise HTTPException(
                status_code=409, detail=f"Could not start sign-in ({error})."
            ) from error
        setattr(state, self.in_flight, flow)

        async def _await_completion() -> None:
            try:
                await flow.wait()
                self.clear_caches()
                await _reset_all_runtimes()
                _publish_broadcast({"type": "settings_changed"})
            except Exception:  # noqa: BLE001 — timeout/denial just leaves us signed out
                pass
            finally:
                if getattr(state, self.in_flight, None) is flow:
                    setattr(state, self.in_flight, None)

        task = asyncio.create_task(_await_completion(), name=f"langmesh:auth:{self.in_flight}")
        state._auth_tasks.add(task)
        task.add_done_callback(state._auth_tasks.discard)
        return {"authorize_url": flow.authorize_url}

    async def signout(self) -> dict:
        pending = getattr(state, self.in_flight, None)
        if pending is not None:
            await pending.close()
            setattr(state, self.in_flight, None)
        await asyncio.to_thread(self.clear)
        self.clear_caches()
        self.clear_resumptions()
        await _reset_all_runtimes()
        _publish_broadcast({"type": "settings_changed"})
        return {"ok": True}


def _chatgpt_account(tokens: Any) -> str:
    return tokens.email or ""


def _chatgpt_caches() -> None:
    clear_subscription_models_cache()
    clear_usage_snapshot()


def _cursor_resumptions() -> None:
    from langmesh.runtime.models.cursor import clear_resumptions

    clear_resumptions()


_PROVIDERS = {
    "chatgpt": _ProviderAuth(
        flow_kind=ChatGPTLoginFlow,
        load=load_tokens,
        clear=clear_tokens,
        in_flight="chatgpt_login_flow",
        clear_caches=_chatgpt_caches,
        account=_chatgpt_account,
    ),
    "cursor": _ProviderAuth(
        flow_kind=CursorLoginFlow,
        load=cursor_load_tokens,
        clear=cursor_clear_tokens,
        in_flight="cursor_login_flow",
        clear_caches=cursor_subscription.clear_subscription_models_cache,
        account=lambda tokens: tokens.account or "",
        clear_resumptions=_cursor_resumptions,
    ),
}


def _provider(provider: str) -> _ProviderAuth:
    entry = _PROVIDERS.get(provider)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No provider named {provider!r}.")
    return entry


@router.get("/auth/{provider}")
async def auth_status(provider: str):
    """Whether a subscription provider is signed in, and for which account."""
    return await _provider(provider).status()


@router.post("/auth/{provider}/start")
async def auth_start(provider: str):
    """Begin a provider's OAuth sign-in and return the authorize URL for the client to open."""
    return await _provider(provider).start()


@router.delete("/auth/{provider}")
async def auth_signout(provider: str):
    """Sign out a provider: clear the stored tokens and reset runtimes so it re-locks immediately."""
    return await _provider(provider).signout()
