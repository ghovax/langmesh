"""Settings routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
import langmesh.base.confinement as _confinement
import langmeshd.commons.toolboxes as _toolbox
import langmesh.base.configuration as _configuration
from models_provider import (
    clear_chatgpt_models_cache,
    clear_cursor_models_cache,
    fetch_chatgpt_models,
    fetch_cursor_models,
)
from langmesh.base.content.models import available_models, list_models, ModelDefinition
from langmesh.base.identity.providers import PROVIDERS
import asyncio
from langmeshd.commons.configuration import AppSettingsUpdateRequest, DictationUpdateRequest
from langmesh.protocol.dtos import (
    AttachmentsUpdateRequest,
    CompactionUpdateRequest,
    ComputerControlUpdateRequest,
    SettingValueRequest,
    ToolboxUpdateRequest,
    SandboxUpdateRequest,
    UserContextUpdateRequest,
)
from models_provider import bind_credential_store, reset_credential_store
from langmeshd.daemon.persistence.credentials import file_credential_store
from langmeshd.commons import state
from langmeshd.commons.services.broadcast import _publish_broadcast
from langmeshd.commons.services.sessions import (
    _normalize_permission_mode,
)
from langmeshd.commons.services.agents import _recent_models
from langmeshd.commons.services.settings import (
    _apply_live_credentials,
    _persist_app_section,
    _persist_configuration,
    _reload_configuration_from_disk,
)

router = APIRouter()


def _email_settings():
    from langmesh.base.configuration.configuration_schema import walk
    from langmeshd.commons.configuration import EmailConfiguration

    return walk(EmailConfiguration, "email")


def _setting_for(path: str):
    """Library settings, then app-owned email.* so the file's mail section is editable here."""
    from langmesh.base.configuration.configuration_schema import setting_for

    found = setting_for(path)
    if found is not None:
        return found
    for setting in _email_settings():
        if setting.path == path:
            return setting
    return None


def _live_additions(
    provider_identifier: str,
    live: dict[str, dict],
    catalog: list[ModelDefinition],
    routable,
    modalities: tuple[str, ...],
) -> list[ModelDefinition]:
    """Catalog entries for live subscription models the static list does not name."""
    known = {
        model.identifier.split("/", 1)[1]
        for model in catalog
        if model.provider == provider_identifier
    }
    return [
        ModelDefinition(
            identifier=f"{provider_identifier}/{model_id}",
            name=meta["name"],
            provider=provider_identifier,
            attachment=len(modalities) > 1,
            vision="image" in modalities,
            input_modalities=modalities,
            context_length=meta["context"],
        )
        for model_id, meta in live.items()
        if model_id not in known and routable(model_id)
    ]


def _merged_sandbox(current, posted: dict):
    """The stored confinement with a posted patch laid over it, validated whole so an unknown key fails rather than vanishes."""
    try:
        return _configuration.SandboxConfiguration.model_validate(
            {**current.model_dump(), **posted}
        )
    except Exception as error:  # noqa: BLE001 — the validator's own message is the useful part
        raise HTTPException(status_code=400, detail=f"invalid sandbox settings: {error}") from error


@router.get("/models")
async def list_models_endpoint(refresh: bool = False):
    """The model catalog for the picker: every known model, whether its provider has a credential, and the provider registry."""
    assert state.global_configuration is not None
    # A retry re-fetches the live subscription catalogs rather than serving their TTL'd copies.
    if refresh:
        clear_chatgpt_models_cache()
        clear_cursor_models_cache()
    # Request tasks do not always inherit the daemon's bound store, so bind it for this listing.
    credential_store = file_credential_store()
    bound = bind_credential_store(credential_store)
    try:
        configured_keys = state.global_configuration.configured_provider_keys()
        available_identifiers = {
            model.identifier
            for model in available_models(configured_keys, credential_store=credential_store)
        }
        # The subscription providers list a static superset, so both accounts' live catalogs are fetched at once to grey the rest.
        live_chatgpt, live_cursor = await asyncio.gather(
            fetch_chatgpt_models(),
            fetch_cursor_models(),
        )
    finally:
        reset_credential_store(bound)
    catalog = list_models()
    # Live models the static list has not caught are appended, filtered to what this harness can actually route.
    catalog.extend(
        _live_additions(
            "chatgpt",
            live_chatgpt,
            catalog,
            lambda slug: slug.startswith("gpt-"),
            ("text", "image"),
        )
    )
    # Cursor's agent service takes a text turn, so nothing there claims image input.
    catalog.extend(
        _live_additions(
            "cursor",
            live_cursor,
            catalog,
            lambda _model_id: True,
            ("text",),
        )
    )
    live_by_provider = {"chatgpt": set(live_chatgpt), "cursor": set(live_cursor)}

    def _is_available(model: ModelDefinition) -> bool:
        if (live := live_by_provider.get(model.provider)) is not None:
            return model.identifier.split("/", 1)[1] in live
        return model.identifier in available_identifiers

    models = [
        {
            "id": model.identifier,
            "name": model.name,
            "provider": model.provider,
            "available": _is_available(model),
            "attachment": model.attachment,
            "vision": model.vision,
            "input_modalities": list(model.input_modalities),
            "release_date": model.release_date,
        }
        for model in catalog
    ]
    providers = [
        {
            "id": provider.identifier,
            "name": provider.name,
            "openai_compatible": provider.openai_compatible,
            "credential_id": provider.credential_identifier or provider.identifier,
        }
        for provider in PROVIDERS.values()
    ]
    return {
        "models": models,
        "providers": providers,
    }


@router.get("/models/recent")
async def recent_models():
    """Recently selected models, newest first, so a user can switch back without scrolling the whole catalog."""
    return {"models": await asyncio.to_thread(_recent_models)}


@router.post("/settings/dictation")
async def update_dictation(request: DictationUpdateRequest):
    """Persist and apply the toggle, releasing the worker at once since it holds a model in wired memory."""
    from langmeshd.rest.routes.dictation import _shutdown_transcriber

    assert state.dictation_configuration is not None
    async with state.configuration_lock:
        await asyncio.to_thread(_persist_app_section, "dictation", {"enabled": request.enabled})
        state.dictation_configuration.enabled = request.enabled
        if not request.enabled:
            await asyncio.to_thread(_shutdown_transcriber)
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "enabled": state.dictation_configuration.enabled}


@router.get("/settings")
async def get_settings():
    """The stored API credentials, so the settings dialog can pre-fill them."""
    assert state.global_configuration is not None
    # The machine default, used when a caller has not selected an agent-specific mode.
    permission_mode = _normalize_permission_mode(state.global_configuration.agent.permission_mode)
    return {
        "permission_mode": permission_mode,
        "exa_api_key": state.global_configuration.exa.api_key,
        "composio_api_key": state.composio_configuration.effective_api_key,
        "jina_api_key": state.global_configuration.jina.api_key,
        "firecrawl_api_key": state.global_configuration.firecrawl.api_key,
        "web_fetch_proxy_url": state.global_configuration.web_fetch.proxy_url,
        "sandbox": state.global_configuration.sandbox.model_dump(mode="json"),
        "sandbox_backend": _confinement.probe(),
        "worktree_strategy": state.global_configuration.workspace.strategy,
        "compaction": state.global_configuration.compaction.model_dump(),
        "attachments": state.global_configuration.attachments.model_dump(),
        "user_context_enabled": state.global_configuration.user_context.enabled,
        "computer_control_enabled": state.global_configuration.computer_control.enabled,
        "toolbox_enabled": state.global_configuration.toolbox.enabled,
        # Whether this machine could offer one at all, which the switch being on does not answer.
        "toolbox_available": _toolbox.available(),
        "dictation_enabled": state.dictation_configuration.enabled,
        "providers": {
            identifier: {"api_key": credential.api_key, "base_url": credential.base_url}
            for identifier, credential in state.global_configuration.providers.items()
        },
    }


@router.post("/settings")
async def update_settings(request: AppSettingsUpdateRequest):
    """Persist API credentials and apply them live, refreshing the clients and dropping cached runtimes."""
    assert state.global_configuration is not None
    configuration = state.global_configuration
    async with state.configuration_lock:
        await _persist_configuration(
            exa_api_key=request.exa_api_key,
            jina_api_key=request.jina_api_key,
            firecrawl_api_key=request.firecrawl_api_key,
            web_fetch_proxy_url=request.web_fetch_proxy_url,
            sandbox=request.sandbox,
            provider_keys=request.provider_keys,
            provider_base_urls=request.provider_base_urls,
            worktree_strategy=request.worktree_strategy,
            permission_mode=(
                _normalize_permission_mode(request.permission_mode)
                if request.permission_mode is not None
                else None
            ),
        )
        # `agent.permission_mode` in the configuration file, which is where it is read from too.
        if request.permission_mode is not None:
            configuration.agent.permission_mode = _normalize_permission_mode(
                request.permission_mode
            )
        if request.exa_api_key is not None:
            configuration.exa.api_key = request.exa_api_key
        if request.composio_api_key is not None:
            await asyncio.to_thread(
                _persist_app_section, "composio", {"api_key": request.composio_api_key}
            )
            state.composio_configuration.api_key = request.composio_api_key
        if request.jina_api_key is not None:
            configuration.jina.api_key = request.jina_api_key
        if request.firecrawl_api_key is not None:
            configuration.firecrawl.api_key = request.firecrawl_api_key
        if request.web_fetch_proxy_url is not None:
            configuration.web_fetch.proxy_url = request.web_fetch_proxy_url
        if request.sandbox is not None:
            configuration.sandbox = _merged_sandbox(configuration.sandbox, request.sandbox)
        if request.worktree_strategy is not None:
            configuration.workspace.strategy = request.worktree_strategy
        # Rebuild the providers map from the posted values, merging so an unrendered provider keeps its credential.
        merged_providers = {
            identifier: _configuration.ProviderCredential(
                api_key=credential.api_key, base_url=credential.base_url
            )
            for identifier, credential in configuration.providers.items()
        }
        for provider_identifier, api_key in (request.provider_keys or {}).items():
            existing = merged_providers.get(
                provider_identifier
            ) or _configuration.ProviderCredential.model_validate({})
            merged_providers[provider_identifier] = existing.model_copy(update={"api_key": api_key})
        for provider_identifier, base_url in (request.provider_base_urls or {}).items():
            existing = merged_providers.get(
                provider_identifier
            ) or _configuration.ProviderCredential.model_validate({})
            merged_providers[provider_identifier] = existing.model_copy(
                update={"base_url": base_url}
            )
        configuration.providers = merged_providers
        await _apply_live_credentials()
        await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved"}


@router.get("/settings/schema")
async def settings_schema():
    """Every setting there is, with what it holds and what it is set to, as one endpoint for the whole file."""
    from langmeshd.commons import configuration_file
    from langmesh.base.configuration.configuration_schema import (
        KIND_SECTION,
        settings as all_settings,
    )

    document = await asyncio.to_thread(configuration_file.load)
    sections: dict[str, dict] = {}
    for setting in [*all_settings(), *_email_settings()]:
        section = setting.path.split(".")[0]
        if setting.kind == KIND_SECTION and "." not in setting.path:
            sections.setdefault(section, {"path": section, "settings": []})
            continue
        entry = sections.setdefault(section, {"path": section, "settings": []})
        if setting.kind == KIND_SECTION:
            continue
        try:
            value = configuration_file.read(document, setting.path)
            configured = True
        except KeyError:
            value = setting.default
            configured = False
        entry["settings"].append(
            {
                "path": setting.path,
                "kind": setting.kind,
                "choices": list(setting.choices),
                "optional": setting.optional,
                "secret": setting.secret,
                "default": setting.default,
                "value": value,
                # Whether the file says this, as opposed to the code shipping it.
                "configured": configured,
            }
        )
    return {"sections": [section for section in sections.values() if section["settings"]]}


@router.post("/settings/value")
async def update_setting(request: SettingValueRequest):
    """Set one setting by its path, validated first, because the daemon reads this file at every start."""
    from langmeshd.commons import configuration_file

    if _setting_for(request.path) is None:
        raise HTTPException(status_code=404, detail=f"No setting named {request.path!r}.")
    async with state.configuration_lock:
        document = await asyncio.to_thread(configuration_file.load)
        entry = _setting_for(request.path)
        if request.value is None and entry is not None and not entry.optional:
            # Nothing, for a setting that cannot hold nothing, means put it back rather than write a null.
            configuration_file.remove(document, request.path)
        else:
            configuration_file.write(document, request.path, request.value)
        invalid = configuration_file.rejects(document)
        if invalid:
            raise HTTPException(status_code=400, detail=invalid)
        await asyncio.to_thread(configuration_file.save, document)
        # Read back, applied live, and every session told to rebuild, done once for every setting.
        await _reload_configuration_from_disk()
        await state.reset_runtimes()
    return {"status": "saved", "path": request.path}


@router.delete("/settings/value")
async def reset_setting(path: str):
    """Put one setting back to what the code ships by removing it, so it follows the default from here on."""
    from langmeshd.commons import configuration_file

    if _setting_for(path) is None:
        raise HTTPException(status_code=404, detail=f"No setting named {path!r}.")
    async with state.configuration_lock:
        document = await asyncio.to_thread(configuration_file.load)
        removed = configuration_file.remove(document, path)
        if removed:
            invalid = configuration_file.rejects(document)
            if invalid:
                raise HTTPException(status_code=400, detail=invalid)
            await asyncio.to_thread(configuration_file.save, document)
            await _reload_configuration_from_disk()
            await state.reset_runtimes()
    return {"status": "reset", "path": path, "removed": removed}


@router.post("/settings/sandbox")
async def update_sandbox(request: SandboxUpdateRequest):
    """Persist and apply confinement on its own; only sessions created afterwards get the change."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        state.global_configuration.sandbox = _merged_sandbox(
            state.global_configuration.sandbox, request.sandbox
        )
        await _persist_configuration(sandbox=request.sandbox)
    _publish_broadcast({"type": "settings_changed"})
    return {
        "status": "saved",
        "sandbox": state.global_configuration.sandbox.model_dump(mode="json"),
        "sandbox_backend": _confinement.probe(),
    }


@router.post("/settings/user-context")
async def update_user_context(request: UserContextUpdateRequest):
    """Persist and apply the user-context toggle, dropping cached runtimes since the snapshot is built into the prompt."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(user_context_enabled=request.enabled)
        state.global_configuration.user_context.enabled = request.enabled
        await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {
        "status": "saved",
        "user_context_enabled": state.global_configuration.user_context.enabled,
    }


@router.post("/settings/computer-control")
async def update_computer_control(request: ComputerControlUpdateRequest):
    """Persist and apply the computer-use toggle, dropping cached runtimes since their tool set is fixed at construction."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(computer_control_enabled=request.enabled)
        state.global_configuration.computer_control.enabled = request.enabled
        await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {
        "status": "saved",
        "computer_control_enabled": state.global_configuration.computer_control.enabled,
    }


@router.post("/settings/toolbox")
async def update_toolbox(request: ToolboxUpdateRequest):
    """Persist and apply whether sessions may install tools, dropping cached runtimes so the change reaches the next turn."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(toolbox_enabled=request.enabled)
        state.global_configuration.toolbox.enabled = request.enabled
        await state.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "toolbox_enabled": state.global_configuration.toolbox.enabled}


@router.post("/settings/attachments")
async def update_attachments(request: AttachmentsUpdateRequest):
    """Persist and apply the attachment limits, which each turn reads live and so needs no runtime reset."""
    assert state.global_configuration is not None
    changes = request.model_dump(exclude_none=True)
    if changes:
        async with state.configuration_lock:
            await _persist_configuration(attachments=changes)
            state.global_configuration.attachments = (
                state.global_configuration.attachments.model_copy(update=changes)
            )
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "attachments": state.global_configuration.attachments.model_dump()}


@router.post("/settings/compaction")
async def update_compaction(request: CompactionUpdateRequest):
    """Persist and apply the compaction settings, which the runtime reads live and so needs no runtime reset."""
    assert state.global_configuration is not None
    changes = request.model_dump(exclude_none=True)
    if changes:
        async with state.configuration_lock:
            await _persist_configuration(compaction=changes)
            state.global_configuration.compaction = (
                state.global_configuration.compaction.model_copy(update=changes)
            )
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "compaction": state.global_configuration.compaction.model_dump()}
