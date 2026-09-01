"""Focused lifecycle tests for the hosted GitHub worker."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from langmesh.github.hosted import Processor


class FakeSession:
    def __init__(self) -> None:
        self.interruptions = 0
        self.saves = 0

    def interrupt(self) -> bool:
        self.interruptions += 1
        return True

    async def save(self) -> None:
        self.saves += 1


class FakeStore:
    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    async def release_processing(self, delivery_id: str, error: str) -> bool:
        self.released.append((delivery_id, error))
        return True


def processor(*, grace: float = 1.0) -> Processor:
    value = Processor.__new__(Processor)
    value.settings = SimpleNamespace(
        shutdown_grace_seconds=grace,
        queue_poll_seconds=0.01,
        maximum_delivery_attempts=5,
    )
    value.store = FakeStore()
    value._stop_requested = asyncio.Event()
    value._active_delivery_id = None
    value._active_sessions = {}
    value._lifecycle_metrics = {
        "shutdown_requests": 0,
        "shutdown_completed": 0,
        "shutdown_forced": 0,
        "shutdown_requeues": 0,
        "shutdown_checkpoint_attempts": 0,
        "shutdown_checkpoint_failures": 0,
        "delivery_recoveries": 0,
    }
    value._shutdown_started_at = None
    return value


@pytest.mark.asyncio
async def test_normal_shutdown_quiesces_and_allows_worker_to_exit() -> None:
    value = processor(grace=1.0)

    async def idle_worker() -> None:
        await value._stop_requested.wait()

    worker = asyncio.create_task(idle_worker())
    await value.shutdown(worker)

    assert value.is_stopping
    assert value.metrics_snapshot()["shutdown_requests"] == 1
    assert value.metrics_snapshot()["shutdown_forced"] == 0
    assert value.metrics_snapshot()["shutdown_completed"] == 1


@pytest.mark.asyncio
async def test_forced_shutdown_checkpoints_cache_state_and_requeues_delivery() -> None:
    value = processor(grace=0.1)
    session = FakeSession()
    value._active_delivery_id = "delivery-1"
    value._active_sessions["github:org/repo:issue:1"] = session

    worker = asyncio.create_task(asyncio.sleep(30))
    await value.shutdown(worker)

    assert session.interruptions == 1
    assert session.saves == 1
    assert value.store.released == [
        (
            "delivery-1",
            "service shutdown interrupted delivery; retrying from the durable checkpoint",
        )
    ]
    metrics = value.metrics_snapshot()
    assert metrics["shutdown_forced"] == 1
    assert metrics["shutdown_checkpoint_attempts"] == 1
    assert metrics["shutdown_requeues"] == 1
    assert metrics["shutdown_checkpoint_failures"] == 0


@pytest.mark.asyncio
async def test_release_is_idempotent_when_work_completed_during_shutdown() -> None:
    value = processor()
    value._active_delivery_id = "delivery-2"
    value.store.release_processing = _completed_release

    await value.release_active_delivery()

    assert value.metrics_snapshot()["shutdown_requeues"] == 0


async def _completed_release(_delivery_id: str, _error: str) -> bool:
    return False
