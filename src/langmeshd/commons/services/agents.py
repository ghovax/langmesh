"""Agent domain: card building, configuration resolution, sidecar writes, and model history."""

from __future__ import annotations
from langmesh.protocol.dtos import (
    AgentBashConfigurationResponse,
    AgentConfigurationResponse,
    AgentConfigurationUpdateRequest,
)

from datetime import datetime, timezone
from langmesh.protocol.card import build_agent_card
from langmeshd.commons.agent_files import (
    agent_configuration_path,
    list_agent_route_names,
    list_agents,
    load_agent_configuration,
)
from langmesh.base.content.models import find_model, split_model_identifier
from langmesh.base.content.skills import skills_for_agent
from langmeshd.daemon.catalogue import load_skills
from langmeshd.commons.configuration_locations import agent_directories, skill_directories
from pathlib import Path
import langmesh.base.configuration as _configuration
from langmeshd.commons import state
from langmeshd.commons.database import ModelHistoryRecord


def _catalogue_base_url() -> str:
    """The address a profile's card names: the daemon, since nothing listens for a profile until a session exists."""
    return f"http://127.0.0.1:{state.daemon_port}"


def _path_scope(path_value: str, home_root: Path) -> str:
    """Whether a discovered file is global or the selected folder's own."""
    try:
        return "global" if Path(path_value).resolve().is_relative_to(home_root) else "project"
    except Exception:
        return "global"


def _record_model_selection(model_identifier: str) -> None:
    """Record a model selection, upserting by id, with a readable label for a typed model id."""
    if not model_identifier or state.session_factory is None:
        return
    definition = find_model(model_identifier)
    split = split_model_identifier(model_identifier)
    if definition is None and split is None:
        return
    if split is not None:
        provider, suffix = split
    else:
        assert definition is not None
        provider, suffix = definition.provider, definition.identifier.split("/", 1)[1]
    label = (
        definition.name
        if definition is not None
        else suffix.replace("/", " / ").replace("-", " ").replace("_", " ").title()
    )
    database_session = state.session_factory()
    try:
        record = database_session.get(ModelHistoryRecord, model_identifier)
        selected_at = datetime.now(timezone.utc).isoformat()
        if record is None:
            database_session.add(
                ModelHistoryRecord(
                    model_id=model_identifier,
                    name=label,
                    provider=provider,
                    selected_at=selected_at,
                )
            )
        else:
            record.name = label
            record.provider = provider
            record.selected_at = selected_at
        database_session.commit()
    except Exception:
        database_session.rollback()
    finally:
        database_session.close()


def _recent_models(limit: int = 8) -> list[dict[str, str]]:
    """Recently selected models, newest first."""
    if state.session_factory is None:
        return []
    database_session = state.session_factory()
    try:
        rows = (
            database_session.query(ModelHistoryRecord)
            .order_by(ModelHistoryRecord.selected_at.desc())
            .limit(limit)
            .all()
        )
        return [{"id": row.model_id, "name": row.name, "provider": row.provider} for row in rows]
    finally:
        database_session.close()


def _card_for(agent_name: str, working_directory: str = ""):
    """Build an agent's card from its configuration and the skills scoped to the given working directory."""
    assert state.application_configuration is not None
    configuration = load_agent_configuration(agent_name, agent_directories())
    skill_roots = skill_directories(working_directory)
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    return configuration, build_agent_card(
        configuration,
        agent_skills,
        _catalogue_base_url(),
    )


def _agent_directories_for_request(working_directory: str) -> list[Path]:
    assert state.application_configuration is not None
    return agent_directories(working_directory)


#: Profiles as last read from disk, emptied by the same watcher that rebuilds the cards.
_resolved_profiles: dict[
    tuple[str, tuple[str, ...]], tuple[Path, _configuration.AgentConfiguration]
] = {}


#: Which profiles exist, per set of directories, held on the same terms as the parsed ones.
_available_agents: dict[tuple[str, ...], list[str]] = {}


def forget_resolved_profiles() -> None:
    """Drop the parsed profiles and the listings, because the files behind them changed."""
    _resolved_profiles.clear()
    _available_agents.clear()


def available_agent_names(directories) -> list[str]:
    """Which profiles these directories offer, read from disk once and held until the watcher says otherwise."""
    key = tuple(str(directory) for directory in directories)
    names = _available_agents.get(key)
    if names is None:
        names = [entry["id"] for entry in list_agents(directories)]
        _available_agents[key] = names
    return names


def _agent_configuration_for_request(
    agent_name: str, working_directory: str
) -> tuple[Path, _configuration.AgentConfiguration]:
    """A profile parsed from disk once and held until the watcher says the files moved."""
    directories = _agent_directories_for_request(working_directory)
    key = (agent_name, tuple(str(directory) for directory in directories))
    held = _resolved_profiles.get(key)
    if held is None:
        held = (
            agent_configuration_path(agent_name, directories),
            load_agent_configuration(agent_name, directories),
        )
        _resolved_profiles[key] = held
    return held


def _agent_configuration_payload(
    agent_name: str, working_directory: str
) -> AgentConfigurationResponse:
    path, configuration = _agent_configuration_for_request(agent_name, working_directory)
    return AgentConfigurationResponse(
        id=configuration.identifier,
        name=configuration.name,
        title=configuration.display_name,
        model=configuration.model or "",
        provider=configuration.provider or "",
        reasoning_effort=configuration.reasoning_effort,
        permission_mode=configuration.permission_mode,
        tools_enabled=configuration.tools_enabled,
        bash=AgentBashConfigurationResponse(
            background_allowed=configuration.tools.bash.background_allowed,
            permissions=dict(configuration.tools.bash.permissions),
        ),
        path=str(path),
    )


def _apply_agent_configuration_update(
    configuration: _configuration.AgentConfiguration, request: AgentConfigurationUpdateRequest
) -> _configuration.AgentConfiguration:
    """The profile with this request applied, as a value. Edits land on the `AgentConfiguration` itself."""
    updated = configuration.model_copy(deep=True)
    if request.model is not None:
        updated.model = request.model or None
    if request.provider is not None:
        updated.provider = request.provider or None
    if request.reasoning_effort is not None:
        updated.reasoning_effort = request.reasoning_effort
    if request.permission_mode is not None:
        updated.permission_mode = request.permission_mode
    if request.tools_enabled is not None:
        updated.tools_enabled = list(request.tools_enabled)
    if request.bash is not None:
        if request.bash.background_allowed is not None:
            updated.tools.bash.background_allowed = request.bash.background_allowed
        if request.bash.permissions is not None:
            updated.tools.bash.permissions = _normalized_permissions(request.bash.permissions)
    return updated


def _normalized_permissions(permissions: dict[str, str]) -> dict[str, str]:
    """Rules with their decisions lowercased and anything unnamed dropped."""
    return {
        str(pattern): str(decision).lower()
        for pattern, decision in (permissions or {}).items()
        if str(pattern).strip()
    }


def _reload_agent_cards() -> None:
    """Recompile the catalogue of profile cards, which is a different thing from the sessions themselves."""
    assert state.application_configuration is not None
    forget_resolved_profiles()
    catalogue = {}
    for agent_name in list_agent_route_names(agent_directories()):
        try:
            _configuration, card = _card_for(agent_name)
        except Exception:  # noqa: BLE001 — one unreadable profile must not empty the catalogue
            continue
        catalogue[agent_name] = card
    state.agent_cards = catalogue
