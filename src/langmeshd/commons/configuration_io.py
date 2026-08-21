"""The daemon's typed configuration loading: reading the file, seeding first run, and persisting settings.

The library's Configuration is a pure model; every read and write of the YAML file happens
here, in the daemon.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from langmeshd.commons.paths import configuration_file_path
from langmesh.base.configuration import Configuration
from langmeshd.commons import configuration_file
from langmeshd.commons.configuration_locations import (
    bundled_agents_root,
    home_agents_root,
    mcp_configuration,
    remote_agents_configuration,
)


PACKAGED_CONFIGURATION_PATH = Path(__file__).resolve().parent / "configuration.yaml"


def packaged_configuration_yaml() -> str:
    """The shipped template a first run seeds the configuration file from."""
    return PACKAGED_CONFIGURATION_PATH.read_text()


def seed_home_agents() -> list[str]:
    """Seed ``~/.agents`` with editable copies, filling only what is missing so a person's edits survive."""
    home_root = home_agents_root()
    seeded: list[str] = []
    for kind in ("agents", "skills"):
        source_root = bundled_agents_root() / kind
        if not source_root.is_dir():
            continue
        target_root = home_root / kind
        target_root.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source_root.iterdir()):
            if entry.name.startswith("."):  # skip .DS_Store and other dotfiles
                continue
            target = target_root / entry.name
            if target.exists():
                continue  # a home copy already exists (possibly user-edited) — leave it
            try:
                if entry.is_dir():
                    shutil.copytree(entry, target)
                else:
                    shutil.copy2(entry, target)
                seeded.append(f"{kind}/{entry.name}")
            except OSError:
                # A single unseedable profile must never block startup or the others.
                continue
    return seeded


def load_configuration(*, seed: bool = True) -> Configuration:
    """Read the configuration file, seeding it from the packaged template on first run.

    Applies the MCP and remote-agents dotagents-roots enrichment the library once folded
    into its loader, so every caller gets the same fully-resolved configuration.
    """
    path = configuration_file_path()
    if not path.exists():
        if not seed:
            return Configuration()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(packaged_configuration_yaml())
    data = configuration_file.load()
    configuration = Configuration(**(data or {}))
    configuration.mcp = mcp_configuration("")
    configuration.remote_agents = remote_agents_configuration("")
    return configuration


def save_api_keys(
    *,
    exa_api_key: str | None = None,
    jina_api_key: str | None = None,
    firecrawl_api_key: str | None = None,
    web_fetch_proxy_url: str | None = None,
    permission_mode: str | None = None,
    sandbox: dict | None = None,
    worktree_strategy: str | None = None,
    compaction: dict | None = None,
    user_context_enabled: bool | None = None,
    computer_control_enabled: bool | None = None,
    toolbox_enabled: bool | None = None,
    tuning: dict | None = None,
    provider_keys: dict[str, str] | None = None,
    provider_base_urls: dict[str, str] | None = None,
) -> None:
    """Persist settings into the configuration file, preserving the rest and writing only what was given."""
    path = configuration_file_path()
    data = configuration_file.load() if path.exists() else {}
    if exa_api_key is not None:
        data.setdefault("exa", {})["api_key"] = exa_api_key
    if jina_api_key is not None:
        data.setdefault("jina", {})["api_key"] = jina_api_key
    if firecrawl_api_key is not None:
        data.setdefault("firecrawl", {})["api_key"] = firecrawl_api_key
    if web_fetch_proxy_url is not None:
        data.setdefault("web_fetch", {})["proxy_url"] = web_fetch_proxy_url
    if sandbox is not None:
        data.setdefault("sandbox", {}).update(sandbox)
    if worktree_strategy is not None:
        data.setdefault("workspace", {})["strategy"] = worktree_strategy
    if compaction is not None:
        data.setdefault("compaction", {}).update(compaction)
    if tuning is not None:
        data.setdefault("tuning", {}).update(tuning)
    if user_context_enabled is not None:
        data.setdefault("user_context", {})["enabled"] = user_context_enabled
    if computer_control_enabled is not None:
        data.setdefault("computer_control", {})["enabled"] = computer_control_enabled
    if toolbox_enabled is not None:
        data.setdefault("toolbox", {})["enabled"] = toolbox_enabled
    if provider_keys is not None or provider_base_urls is not None:
        providers_section = data.setdefault("providers", {})
        all_provider_ids = {*(provider_keys or {}), *(provider_base_urls or {})}
        for provider_id in all_provider_ids:
            entry = dict(providers_section.get(provider_id) or {})
            if provider_keys is not None and provider_id in provider_keys:
                entry["api_key"] = provider_keys[provider_id]
            if provider_base_urls is not None and provider_id in provider_base_urls:
                entry["base_url"] = provider_base_urls[provider_id]
            providers_section[provider_id] = entry
    if permission_mode is not None:
        data.setdefault("agent", {})["permission_mode"] = permission_mode
    configuration_file.save(data)


__all__ = ["load_configuration", "packaged_configuration_yaml", "save_api_keys", "seed_home_agents"]
