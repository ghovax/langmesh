"""Daemon policy for locating and loading machine configuration files."""

from __future__ import annotations

from collections.abc import Iterable
import json
import os
from pathlib import Path
import sys

from langmesh.base.contracts.mcp_client import MCPConfiguration, MCPServerConfiguration
from langmeshd.commons.configuration import (
    RemoteAgentsConfiguration,
    RemoteAgentServerConfiguration,
)
from langmesh.base.content.observations import OBSERVATIONS_FILENAME


HOME_AGENTS_ROOT = Path("~/.agents")
PROJECT_AGENTS_ROOT = Path(".agents")


def bundled_agents_root() -> Path:
    """Return the daemon's installed or frozen base `.agents` tree."""
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", sys.executable))
        if (bundle_root / ".agents" / "agents").is_dir():
            return bundle_root / ".agents"
    here = Path(__file__).resolve()
    installed = here.parents[1] / "_bundled" / ".agents"
    if (installed / "agents").is_dir():
        return installed
    for candidate in (here.parent, *here.parents):
        if (candidate / ".agents" / "agents").is_dir():
            return candidate / ".agents"
    return here.parents[3] / ".agents"


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        expanded = path.expanduser()
        key = expanded.resolve() if expanded.exists() else expanded.absolute()
        if key not in seen:
            seen.add(key)
            result.append(expanded)
    return result


def home_agents_root() -> Path:
    return HOME_AGENTS_ROOT.expanduser()


def project_agents_root(working_directory: str = "") -> Path:
    base = Path(working_directory).expanduser() if working_directory else Path.cwd()
    return base / PROJECT_AGENTS_ROOT


def agents_roots(working_directory: str = "") -> list[Path]:
    return _dedupe([home_agents_root(), project_agents_root(working_directory)])


def agent_directories(working_directory: str = "") -> list[Path]:
    project_root = project_agents_root(working_directory)
    return _dedupe(
        [
            bundled_agents_root() / "agents",
            home_agents_root() / "agents",
            project_root / "agents",
        ]
    )


def skill_directories(working_directory: str = "") -> list[Path]:
    project_root = project_agents_root(working_directory)
    return _dedupe(
        [
            bundled_agents_root() / "skills",
            home_agents_root() / "skills",
            project_root / "skills",
        ]
    )


def memory_directories(working_directory: str = "") -> list[Path]:
    return _dedupe(
        [home_agents_root() / "memories", project_agents_root(working_directory) / "memories"]
    )


def observation_database(working_directory: str) -> Path:
    return project_agents_root(working_directory) / OBSERVATIONS_FILENAME


def load_mcp_configuration(roots: Iterable[Path]) -> MCPConfiguration:
    """Load and merge MCP declarations from explicit roots."""
    servers: dict[str, MCPServerConfiguration] = {}
    for root in roots:
        path = root / "mcp.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for name, raw in data.get("mcpServers", data.get("servers", {})).items():
            configuration = dict(raw)
            if "type" in configuration and "transport" not in configuration:
                configuration["transport"] = configuration.pop("type")
            servers[name] = MCPServerConfiguration(**configuration)
    return MCPConfiguration(servers=servers)


def mcp_configuration(working_directory: str) -> MCPConfiguration:
    return load_mcp_configuration(agents_roots(working_directory))


def remote_agents_configuration(working_directory: str) -> RemoteAgentsConfiguration:
    agents: dict[str, RemoteAgentServerConfiguration] = {}
    for root in agents_roots(working_directory):
        path = root / "remote-agents.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for name, raw in data.get("agents", {}).items():
            configuration = dict(raw)
            authentication = dict(configuration.get("auth") or {})
            for field in ("token", "client_secret", "client_id"):
                if isinstance(authentication.get(field), str):
                    authentication[field] = os.path.expandvars(authentication[field])
            if authentication:
                configuration["auth"] = authentication
            agents[name] = RemoteAgentServerConfiguration(**configuration)
    return RemoteAgentsConfiguration(agents=agents)


__all__ = [
    "agent_directories",
    "agents_roots",
    "bundled_agents_root",
    "home_agents_root",
    "load_mcp_configuration",
    "mcp_configuration",
    "memory_directories",
    "observation_database",
    "project_agents_root",
    "remote_agents_configuration",
    "skill_directories",
]
