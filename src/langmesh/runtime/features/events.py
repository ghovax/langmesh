"""The typed events the library's plugins exchange on the bus, without knowing each other."""


class MemoryHandoffVerified:
    """The durable handoff reported a fresh picture of the memory registry."""

    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata


class MemoryHandoffFailed:
    """The durable handoff reported the memory registry needs repair."""

    def __init__(self, error: str) -> None:
        self.error = error