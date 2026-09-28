from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable


class AgentError(RuntimeError):
    pass


@dataclass
class AgentRequest:
    session_id: str
    message: str
    timeout: float = 7200
    model: str = ""


Emit = Callable[[str, object], None]
Ask = Callable[[list[dict]], Awaitable[dict[str, list[str]]]]
SaveSession = Callable[[str], None]


class AgentBackend(ABC):
    """One turn per instance; durable native session IDs survive process restarts.

    Emit token/thinking/activity events, request explicit answers via ask, and
    save the native ID before submitting work. Never retry a submitted turn.
    """

    @abstractmethod
    async def run(self, request: AgentRequest, native_id: str | None,
                  emit: Emit, ask: Ask, save_session: SaveSession) -> None: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def probe(self) -> dict: ...
