"""The settings domain: applying live credentials, and persisting and reloading the configuration file."""

from __future__ import annotations

from langmeshd.commons.brokers.composio import composio_mcp_servers
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration_io import load_configuration, save_configuration_changes
from langmeshd.commons.paths import configuration_file_path
from langmesh.base.contracts.mcp_client import MCPServerManager
from langmesh.base.primitives.serialization import compact
from typing import Optional
import asyncio
import hashlib
from langmeshd.commons import state


def _persist_app_section(section: str, changes: dict) -> None:
    """Write one of the app's own configuration-file sections, preserving the rest of the file."""
    document = configuration_file.load()
    if section == "composio" and "api_key" in changes:
        from langmesh.base.secrets import COMPOSIO_API_KEY, write_secret

        write_secret(COMPOSIO_API_KEY, str(changes.get("api_key") or ""))
        changes = {**changes, "api_key": ""}
    document.setdefault(section, {}).update(changes)
    invalid = configuration_file.rejects(document)
    if invalid:
        raise ValueError(f"invalid app configuration change: {invalid}")
    configuration_file.save(document)


async def _apply_live_credentials() -> None:
    """Re-provision what the daemon itself owns after a configuration change."""
    assert state.global_configuration is not None
    configuration = state.global_configuration
    state.composio_servers = composio_mcp_servers(state.composio_configuration)
    if state.composio_servers:
        configuration.mcp.servers.update(state.composio_servers)
    else:
        configuration.mcp.servers.pop(state.composio_configuration.server_name, None)
    mcp_servers = configuration.mcp.enabled_servers()
    # Only when the servers changed: reconnecting means new subprocesses and handshakes, and the caller waits.
    fingerprint = _mcp_server_fingerprint(mcp_servers)
    if fingerprint == state.mcp_server_fingerprint and state.mcp_server_manager is not None:
        return
    if state.mcp_server_manager is not None:
        await state.mcp_server_manager.aclose()
    state.mcp_server_manager = MCPServerManager(mcp_servers) if mcp_servers else None
    if state.mcp_server_manager is not None:
        await state.mcp_server_manager.start()
    state.mcp_server_fingerprint = fingerprint


def _mcp_server_fingerprint(servers: dict) -> str:
    """What the MCP server connections are built from, as one comparable string."""
    return compact(
        {
            name: server.model_dump(mode="json") if hasattr(server, "model_dump") else server
            for name, server in sorted(servers.items())
        },
        sort_keys=True,
    )


def _configuration_digest() -> Optional[str]:
    """A content hash of the configuration file, or ``None`` if it is absent."""
    try:
        return hashlib.sha256(configuration_file_path().read_bytes()).hexdigest()
    except OSError:
        return None


async def _persist_configuration(**changes) -> None:
    """Write configuration changes to disk and remember the digest, so the watcher does not read our own save as an edit."""
    await asyncio.to_thread(save_configuration_changes, **changes)
    state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)


async def _reload_configuration_from_disk() -> None:
    """Re-read the configuration file after a manual edit and apply it live."""
    assert state.global_configuration is not None
    fresh = await asyncio.to_thread(load_configuration)
    configuration = state.global_configuration
    # Every section, from the model rather than from a hand-kept list that fell behind the schema.
    for name in type(fresh).model_fields:
        setattr(configuration, name, getattr(fresh, name))
    # The app's own sections, read straight from the file.
    from langmeshd.commons.configuration import ComposioConfiguration, DictationConfiguration

    import yaml as _yaml

    path = configuration_file_path()
    document = {}
    if path.exists():
        document = _yaml.safe_load(path.read_text()) or {}
    state.dictation_configuration = DictationConfiguration.model_validate(
        document.get("dictation") or {}
    )
    state.composio_configuration = ComposioConfiguration.model_validate(
        document.get("composio") or {}
    )
    await _apply_live_credentials()
    state.broadcaster.publish({"type": "settings_changed"})
