from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from langmesh.base.persistence.schedules import ScheduleError, is_due, next_firing
from langmeshd.commons import state
from langmeshd.commons.database import Base, LocationRecord, WorkspaceRecord
from langmeshd.commons.services import schedules
from langmeshd.daemon import scheduler


@pytest.fixture
def database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    previous_factory = state.session_factory
    state.session_factory = factory
    session = factory()
    session.add(
        WorkspaceRecord(
            id="workspace-1",
            created_at="2026-08-31T08:00:00+00:00",
            updated_at="2026-08-31T08:00:00+00:00",
        )
    )
    session.add(
        LocationRecord(
            id="location-1",
            workspace_id="workspace-1",
            name="project",
            kind="local",
            host_alias="",
            base_directory="/tmp/project",
            created_at="2026-08-31T08:00:01+00:00",
        )
    )
    session.commit()
    session.close()
    try:
        yield
    finally:
        state.session_factory = previous_factory
        engine.dispose()


def schedule_input(**overrides):
    values = {
        "workspace_id": "workspace-1",
        "name": "Daily check",
        "cron": "0 9 * * *",
        "prompt": "Check the project",
        "agent": "reviewer",
        "permission_mode": "automatic",
        "timezone_name": "UTC",
    }
    values.update(overrides)
    return values


def test_schedule_creation_uses_workspace_location(database):
    created = schedules.create(**schedule_input())
    assert created["working_directory"] == "/tmp/project"


def test_schedule_creation_preserves_explicit_directory_and_validates(database):
    created = schedules.create(**schedule_input(working_directory="/tmp/other"))
    assert created["working_directory"] == "/tmp/other"
    with pytest.raises(ScheduleError):
        schedules.create(**schedule_input(cron="not cron"))


def test_due_calculation_is_timezone_aware():
    anchor = datetime(2026, 8, 31, 8, 1, tzinfo=timezone.utc)
    due_at = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    assert next_firing("0 9 * * *", "Europe/Amsterdam", after=anchor) == due_at
    assert not is_due(
        "0 9 * * *",
        "Europe/Amsterdam",
        since=anchor,
        now=datetime(2026, 9, 1, 6, 59, tzinfo=timezone.utc),
    )
    assert is_due(
        "0 9 * * *",
        "Europe/Amsterdam",
        since=anchor,
        now=datetime(2026, 9, 1, 7, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_fire_dispatches_prompt_and_records_success(monkeypatch):
    record = SimpleNamespace(
        id="schedule-1",
        name="Daily check",
        agent="reviewer",
        working_directory="/tmp/project",
        permission_mode="automatic",
        workspace_id="workspace-1",
        prompt="Check the project",
    )
    created_calls = []
    sent_calls = []
    recorded_runs = []

    async def create_session(params):
        created_calls.append(params)
        return {"id": "session-1"}

    async def send_message(params):
        sent_calls.append(params)
        return {"accepted": True}

    def record_run(schedule_id, *, session_id="", error=""):
        recorded_runs.append((schedule_id, session_id, error))

    monkeypatch.setattr(scheduler.schedule_service, "record_run", record_run)
    await scheduler.fire(record, create_session=create_session, send_message=send_message)

    assert created_calls == [{
        "agent": "reviewer",
        "working_directory": "/tmp/project",
        "permission_mode": "automatic",
        "workspace_id": "workspace-1",
        "title": "Daily check",
    }]
    assert sent_calls == [{
        "id": "session-1",
        "parts": [{"kind": "text", "text": "Check the project"}],
    }]
    assert recorded_runs == [("schedule-1", "session-1", "")]


@pytest.mark.asyncio
async def test_fire_records_delivery_failure(monkeypatch):
    record = SimpleNamespace(
        id="schedule-1",
        name="Daily check",
        agent="reviewer",
        working_directory="/tmp/project",
        permission_mode="automatic",
        workspace_id="workspace-1",
        prompt="Check the project",
    )
    recorded_runs = []

    def record_run(schedule_id, *, session_id="", error=""):
        recorded_runs.append((schedule_id, session_id, error))

    async def create_session(_params):
        return {"id": "session-1"}

    async def fail_delivery(_params):
        raise RuntimeError("message rejected")

    monkeypatch.setattr(scheduler.schedule_service, "record_run", record_run)
    await scheduler.fire(record, create_session=create_session, send_message=fail_delivery)
    assert recorded_runs == [("schedule-1", "session-1", "message rejected")]
