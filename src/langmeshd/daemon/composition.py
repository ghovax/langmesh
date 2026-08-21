"""What the daemon owns on behalf of everyone, and the watchers that keep it current."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from langmeshd.commons.paths import configuration_file_path
from langmeshd.commons.configuration_io import seed_home_agents
from langmeshd.commons.agent_files import list_agent_route_names
from langmeshd.commons import toolboxes
from langmeshd.commons.paths import data_directory
from langmeshd.commons.configuration_locations import (
    agent_directories,
    agents_roots,
    skill_directories,
)
from langmeshd.daemon.persistence.file_leases import FileLeaseManager
from langmeshd.daemon.persistence.worktrees import SessionWorktreeManager
from langmeshd.daemon import state
from langmeshd.commons import state as commons_state
from langmeshd.commons.services.agents import _reload_agent_cards
from langmeshd.commons.services.broadcast import _notify_filesystem_lease_state
from langmeshd.commons.services.workspaces import _ensure_default_project
from langmeshd.commons.services.settings import (
    _configuration_digest,
    _mcp_server_fingerprint,
    _reload_configuration_from_disk,
)

logger = logging.getLogger(__name__)


async def open_shared_resources() -> None:
    """Build what the daemon holds for everyone, in dependency order."""
    from langmeshd.commons.brokers.composio import composio_mcp_servers
    from langmesh.base.contracts.mcp_client import MCPServerManager
    from langmeshd.commons.brokers.remote_agents import _remote_agent_dataclasses
    from langmeshd.daemon.persistence.push_store import (
        PersistentPushNotificationConfigurationStore,
        PinnedPushNotificationSender,
    )
    from langmeshd.daemon.attachments import FileUrlSigner
    from langmeshd.daemon.persistence.secrets import ensure_private_value
    from langmeshd.commons.brokers.terminals import TerminalSessionManager

    assert commons_state.global_configuration is not None
    configuration = commons_state.global_configuration

    commons_state.main_loop = asyncio.get_running_loop()
    commons_state.file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    commons_state.worktree_manager = SessionWorktreeManager()
    commons_state.terminal_manager = TerminalSessionManager()

    # Seed the home layer with editable copies of the shipped agents and skills, non-destructively.
    seeded = await asyncio.to_thread(seed_home_agents)
    if seeded:
        logger.info("seeded home agents and skills: %s", ", ".join(seeded))
    # Seed the digest with the file as just loaded, so a bootstrap write is not mistaken for a manual edit.
    commons_state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)

    # There is no landing page, so the app always opens into a project: guarantee one exists.
    await asyncio.to_thread(_ensure_default_project)

    # Toolboxes outlive a daemon that was killed, so ones belonging to gone sessions are swept.
    live_sessions = (
        [record.id for record in state.registry.live()] if state.registry is not None else []
    )
    swept = await asyncio.to_thread(toolboxes.sweep, live_sessions)
    if swept:
        logger.info("swept %d toolbox(es) belonging to sessions that are gone", len(swept))

    # Composio's hosted endpoint is folded into the ordinary server set rather than being a second path.
    commons_state.composio_servers = composio_mcp_servers(commons_state.composio_configuration)
    configuration.mcp.servers.update(commons_state.composio_servers)
    mcp_servers = configuration.mcp.enabled_servers()
    commons_state.mcp_server_manager = MCPServerManager(mcp_servers) if mcp_servers else None
    # Recorded here too, or the first write after every boot reconnects everything to learn what it already knows.
    commons_state.mcp_server_fingerprint = _mcp_server_fingerprint(mcp_servers)
    if commons_state.mcp_server_manager is not None:
        # Connected in the background, so a slow or hung server never delays the daemon's boot.
        state._mcp_start_task = asyncio.create_task(commons_state.mcp_server_manager.start())

    signing_root = data_directory()
    commons_state.file_url_signer = FileUrlSigner(
        ensure_private_value(signing_root / "a2a_file_secret", lambda: os.urandom(32)),
        f"http://127.0.0.1:{commons_state.daemon_port}",
        allowed_root=signing_root / "uploads",
    )

    commons_state.push_configuration_store = PersistentPushNotificationConfigurationStore(
        commons_state.async_engine
    )
    await commons_state.push_configuration_store.initialize()
    import httpx

    state._push_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    commons_state.push_sender = PinnedPushNotificationSender(
        state._push_client,
        commons_state.push_configuration_store,
        allow_private=commons_state.push_configuration_store.allow_private_webhooks,
    )

    # Outbound peers, whose card resolution runs in the background so an unreachable one never holds up boot.
    remote_configurations = _remote_agent_dataclasses()
    if remote_configurations:
        from langmesh.protocol.client import RemoteAgentManager

        commons_state.remote_agent_manager = RemoteAgentManager(remote_configurations)
        state._remote_start_task = asyncio.create_task(commons_state.remote_agent_manager.start())

    _reload_agent_cards()
    from langmeshd.daemon import api, scheduler

    state._watchers = [
        asyncio.create_task(_watch_catalogue()),
        asyncio.create_task(_watch_configuration()),
        asyncio.create_task(_watch_ssh_hosts()),
        # Recurring prompts, alongside the watchers because they are the same kind of long-lived task.
        asyncio.create_task(
            scheduler.run(
                create_session=api._session_create,
                send_message=api._session_send,
            )
        ),
    ]


async def close_shared_resources() -> None:
    """Release everything the opener built, ordered and individually guarded."""
    for name in ("chatgpt_login_flow", "cursor_login_flow"):
        flow = getattr(commons_state, name, None)
        if flow is not None:
            with contextlib.suppress(Exception):
                await flow.close()
            setattr(commons_state, name, None)
    for task in [
        *getattr(state, "_watchers", []),
        *getattr(commons_state, "_auth_tasks", set()),
        state.__dict__.get("_mcp_start_task"),
        state.__dict__.get("_remote_start_task"),
    ]:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    commons_state._auth_tasks.clear()
    if commons_state.terminal_manager is not None:
        with contextlib.suppress(Exception):
            await commons_state.terminal_manager.close_all()
    if commons_state.mcp_server_manager is not None:
        with contextlib.suppress(Exception):
            await commons_state.mcp_server_manager.aclose()
    for client in (state.__dict__.get("_push_client"), state.proxy_client):
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def _watched_agent_paths() -> list[str]:
    """Every directory whose contents define what agents and skills exist, watched recursively."""
    assert commons_state.global_configuration is not None
    candidates = [
        *agents_roots(),
        *agent_directories(),
        *skill_directories(),
    ]
    watched: list[str] = []
    seen: set[Path] = set()
    for directory in candidates:
        if not directory.is_dir():
            continue
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        watched.append(str(resolved))
    return watched


async def _watch_catalogue() -> None:
    """Pick up agents, skills, MCP servers, and remote peers as they change on disk."""
    from watchfiles import awatch

    from langmeshd.commons.brokers.mcp_servers import _reload_mcp
    from langmeshd.commons.brokers.remote_agents import _reload_remote_agents

    watched = _watched_agent_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched, stop_event=commons_state.shutting_down):
            paths = [str(path) for _change, path in changes]
            if any(path.endswith("mcp.json") for path in paths):
                await _reload_mcp()
            if any(path.endswith("remote-agents.json") for path in paths):
                await _reload_remote_agents()
            _reload_agent_cards()
            state.broadcaster.publish({"type": "agents_changed"})
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — a watcher that dies must not take the daemon with it
        logger.exception("the agent and skill watcher stopped")


async def _watch_configuration() -> None:
    """Mirror hand edits of the configuration file into the running daemon and its clients."""
    from watchfiles import awatch

    from langmeshd.commons.services.settings import _configuration_digest as digest_of

    path = configuration_file_path()
    try:
        async for _changes in awatch(
            str(path.parent),
            recursive=False,
            watch_filter=lambda _change, changed: Path(changed).name == path.name,
            stop_event=commons_state.shutting_down,
        ):
            # Serialised against interface-driven saves, with the digest re-checked inside the lock.
            async with commons_state.configuration_lock:
                digest = await asyncio.to_thread(digest_of)
                if digest is not None and digest == commons_state.last_written_configuration_digest:
                    continue
                commons_state.last_written_configuration_digest = digest
                await _reload_configuration_from_disk()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("the configuration watcher stopped")


async def _watch_ssh_hosts() -> None:
    """Broadcast when the SSH host registry changes, filtered to the configuration file alone."""
    from watchfiles import awatch

    ssh_configuration = await asyncio.to_thread(_available_ssh_configuration)
    if ssh_configuration is None:
        return
    try:
        async for _changes in awatch(
            str(ssh_configuration.parent),
            recursive=False,
            watch_filter=lambda _change, changed: Path(changed).name == "config",
            stop_event=commons_state.shutting_down,
        ):
            state.broadcaster.publish({"type": "hosts_changed"})
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("the SSH host watcher stopped")


def _available_ssh_configuration() -> Path | None:
    path = Path("~/.ssh/config").expanduser()
    return path if path.parent.exists() else None


def known_agent_names() -> list[str]:
    """Every agent profile a session could be created with, from the configured roots."""
    assert commons_state.global_configuration is not None
    return list_agent_route_names(agent_directories())


__all__ = [
    "close_shared_resources",
    "known_agent_names",
    "open_shared_resources",
]
