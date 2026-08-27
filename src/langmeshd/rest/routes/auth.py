"""The provider sign-in surface: one generic route per provider, keyed by name.

A provider is a resource, not a route: `GET /auth/{provider}` reads the sign-in state, `POST
`/auth/{provider}/start`` begins the OAuth flow, `DELETE /auth/{provider}` signs out. Adding a
provider means adding an entry to the table, never a route.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, HTTPException

from models_provider import (
    ProviderAuthentication,
    clear_chatgpt_models_cache,
    clear_cursor_models_cache,
    clear_usage_snapshot,
    display_cursor_account,
    get_usage_snapshot,
    provider_auth_profile,
)

from langmesh.base.identity.providers import get_provider_definition, provider_env_vars
from langmeshd.commons import state
from langmeshd.commons.services.broadcast import _publish_broadcast
from langmeshd.commons.services.workspaces import _reset_all_runtimes
from langmeshd.daemon.persistence.credentials import file_credential_store

router = APIRouter()


@dataclass(frozen=True)
class _ProviderAuth:
    """One provider's authentication edge: presentation, persistence, and cache invalidation."""

    provider_identifier: str
    in_flight: str
    clear_caches: Callable[[], None]

    @property
    def store(self):
        return file_credential_store()

    def authentication(self) -> ProviderAuthentication:
        definition = get_provider_definition(self.provider_identifier)
        if definition is None:
            raise HTTPException(
                status_code=404,
                detail=f"No provider named {self.provider_identifier!r}.",
            )
        profile = provider_auth_profile(
            self.provider_identifier,
            environment_variables=provider_env_vars(self.provider_identifier),
            default_base_url=definition.default_base_url,
            headers=definition.default_headers,
            anonymous_api_key=definition.anonymous_api_key,
            method="oauth" if self.provider_identifier in {"chatgpt", "cursor"} else "api_key",
            credential_identifier=definition.credential_identifier or self.provider_identifier,
        )
        configured_keys = (
            state.global_configuration.configured_provider_keys()
            if state.global_configuration is not None
            else {}
        )
        return ProviderAuthentication(
            {self.provider_identifier: profile},
            api_keys=configured_keys,
            store=self.store,
        )

    async def status(self) -> dict:
        authentication = self.authentication()
        current = await asyncio.to_thread(authentication.status, self.provider_identifier)
        account = current.account
        if self.provider_identifier == "cursor" and current.signed_in:
            tokens = await asyncio.to_thread(authentication.token, self.provider_identifier)
            account = await display_cursor_account(tokens)
        return {
            "signed_in": current.signed_in,
            "expired": current.expired,
            "method": current.method,
            "source": current.source,
            "account": account,
            "usage": get_usage_snapshot()
            if current.signed_in and self.provider_identifier == "chatgpt"
            else None,
        }

    async def start(self) -> dict:
        pending = getattr(state, self.in_flight, None)
        if pending is not None:
            await pending.close()
        try:
            flow = self.authentication().flow(self.provider_identifier)
        except Exception as error:  # noqa: BLE001 — the client needs a body, not a dropped CORS 500
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            await flow.start()
        except OSError as error:
            raise HTTPException(
                status_code=409, detail=f"Could not start sign-in ({error})."
            ) from error
        except Exception as error:  # noqa: BLE001 — the client needs a body, not a dropped CORS 500
            raise HTTPException(
                status_code=500, detail=f"Could not start sign-in ({error})."
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
        await asyncio.to_thread(self.authentication().sign_out, self.provider_identifier)
        self.clear_caches()
        await _reset_all_runtimes()
        _publish_broadcast({"type": "settings_changed"})
        return {"ok": True}


def _chatgpt_caches() -> None:
    clear_chatgpt_models_cache()
    clear_usage_snapshot()


_PROVIDERS = {
    "chatgpt": _ProviderAuth(
        provider_identifier="chatgpt",
        in_flight="chatgpt_login_flow",
        clear_caches=_chatgpt_caches,
    ),
    "cursor": _ProviderAuth(
        provider_identifier="cursor",
        in_flight="cursor_login_flow",
        clear_caches=clear_cursor_models_cache,
    ),
}


def _provider(provider: str) -> _ProviderAuth:
    identifier = provider.strip().lower()
    entry = _PROVIDERS.get(identifier)
    if entry is not None:
        return entry
    if get_provider_definition(identifier) is None:
        raise HTTPException(status_code=404, detail=f"No provider named {provider!r}.")
    return _ProviderAuth(
        provider_identifier=identifier,
        in_flight=f"{identifier}_login_flow",
        clear_caches=lambda: None,
    )


@router.get("/auth/{provider}")
async def auth_status(provider: str):
    """Return the authentication state for a model provider."""
    return await _provider(provider).status()


@router.post("/auth/{provider}/start")
async def auth_start(provider: str):
    """Begin a provider's registered sign-in flow and return its URL."""
    return await _provider(provider).start()


@router.delete("/auth/{provider}")
async def auth_signout(provider: str):
    """Clear a provider credential and reset runtimes so it re-locks immediately."""
    return await _provider(provider).signout()
