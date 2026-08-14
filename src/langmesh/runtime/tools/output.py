"""A tool's machine result and optional model guidance, kept separate until message assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """Data for the tool-role message plus guidance for a later instruction-role message."""

    result: Any
    model_guidance: str = ""
