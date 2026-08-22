"""Task-local dependencies that must follow a runtime into concurrent work."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from langmesh.base.primitives.limits import Limits, bind_limits, reset_limits


@dataclass(frozen=True)
class RuntimeEnvironment:
    """Optional context-bound limits, credentials, and tracing for one runtime."""

    limits: Limits | None = None
    credentials: Any = None
    tracer: Any = None

    @contextmanager
    def bind(self) -> Iterator[None]:
        """Bind this runtime's dependencies for the current task and restore its caller afterward."""
        with ExitStack() as stack:
            if self.limits is not None:
                token = bind_limits(self.limits)
                stack.callback(reset_limits, token)
            if self.credentials is not None:
                from langmesh.base.identity.credential_store import (
                    bind_credential_store,
                    reset_credential_store,
                )

                token = bind_credential_store(self.credentials)
                stack.callback(reset_credential_store, token)
            if self.tracer is not None:
                from langmesh.base.primitives.telemetry import reset_tracer, set_tracer

                token = set_tracer(self.tracer)
                stack.callback(reset_tracer, token)
            yield


__all__ = ["RuntimeEnvironment"]
