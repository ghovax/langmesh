"""MCP domain: per-request resolution of an agent's servers, and the live reload of mcp.json."""

from __future__ import annotations

from langmesh.base.contracts.mcp_client import MCPServerManager
from langmeshd.commons import state
from langmeshd.commons.configuration_io import load_configuration
from langmeshd.commons.configuration_locations import mcp_configuration


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply it live, dropping cached runtimes so the next turn sees the new set."""
    assert state.global_configuration is not None
    # Serialized with the settings endpoints and the watcher, which all rebuild the shared manager.
    async with state.configuration_lock:
        state.global_configuration.mcp = load_configuration(seed=False).mcp
        # Re-fold the provisioned Composio server in, so a live edit does not drop its tools.
        state.global_configuration.mcp.servers.update(state.composio_servers)
        enabled = state.global_configuration.mcp.enabled_servers()
        if state.mcp_server_manager is None:
            if enabled:
                state.mcp_server_manager = MCPServerManager(enabled)
                await state.mcp_server_manager.start()
        else:
            await state.mcp_server_manager.reconcile(enabled)
        await state.reset_runtimes()


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Grow the shared pool with a folder's own servers. A union: servers are added or updated, never removed."""
    assert state.global_configuration is not None
    if not working_directory:
        return
    # Serialized with every other mutator, so concurrent reconciles never clobber the shared manager.
    async with state.configuration_lock:
        folder_servers = mcp_configuration(working_directory).servers
        new_servers = {
            name: configuration
            for name, configuration in folder_servers.items()
            if state.global_configuration.mcp.servers.get(name) != configuration
        }
        if not new_servers:
            return
        state.global_configuration.mcp.servers.update(new_servers)
        enabled = state.global_configuration.mcp.enabled_servers()
        if state.mcp_server_manager is None:
            if enabled:
                state.mcp_server_manager = MCPServerManager(enabled)
                await state.mcp_server_manager.start()
        else:
            await state.mcp_server_manager.reconcile(enabled)
        await state.reset_runtimes()
