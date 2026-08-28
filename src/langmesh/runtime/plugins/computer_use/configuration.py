"""Configuration owned by the computer-use plugin."""

from pydantic import Field

from langmesh.base.configuration.configuration import Section


class RetrievalConfiguration(Section):
    """Screen ranking models and lexical gates."""

    multilingual_rank_model: str = "minishlab/M2V_multilingual_output"
    english_rank_model: str = "minishlab/potion-base-32M"
    lexical_gate_short_words: int = Field(default=3, ge=0)
    lexical_gate_long_words: int = Field(default=7, ge=1)


class ComputerControlConfiguration(Section):
    """Whether screen control is available and how it ranks targets."""

    enabled: bool = False
    retrieval: RetrievalConfiguration = Field(default_factory=RetrievalConfiguration)


__all__ = ["ComputerControlConfiguration", "RetrievalConfiguration"]
