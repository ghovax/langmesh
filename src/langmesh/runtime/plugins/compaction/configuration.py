"""Configuration owned by the compaction plugin."""

from pydantic import Field, field_validator

from langmesh.base.configuration.configuration import Section


class CompactionConfiguration(Section):
    """Thresholds and reserve ratios used only by conversation compaction."""

    automatic: bool = True
    reclaim_at_fraction: float = 0.85
    output_reserve_fraction: float = 0.1
    recent_working_set_fraction: float = 0.15
    maximum_context_tokens: int = Field(default=0, ge=0)
    summary_attempts: int = Field(default=3, ge=1)
    summary_timeout_seconds: float = Field(default=180.0, gt=0)

    @field_validator(
        "reclaim_at_fraction",
        "output_reserve_fraction",
        "recent_working_set_fraction",
    )
    @classmethod
    def valid_fraction(cls, value: float) -> float:
        if not 0 < value < 1:
            raise ValueError("compaction fractions must be greater than 0 and less than 1")
        return value


__all__ = ["CompactionConfiguration"]
