"""Common metadata contract for local, cloud, and hybrid reasoning engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class EngineCapabilities:
    name: str
    location: str = "local"
    available: bool = False
    supports_tools: bool = False
    supports_vision: bool = False
    privacy: str = "local"
    estimated_latency_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class ReasoningEngine(Protocol):
    async def capabilities(self) -> EngineCapabilities: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
