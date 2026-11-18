"""Channel-independent engine boundary; actual AI behavior is implemented in M09."""

from typing import Protocol

from app.contracts import ChatResult, ExecutionContext, NormalizedMessage


class ConversationEngine(Protocol):
    @property
    def ready(self) -> bool: ...

    async def respond(
        self, context: ExecutionContext, message: NormalizedMessage
    ) -> ChatResult: ...


class EngineUnavailable(RuntimeError):
    pass


class UnavailableEngine:
    ready = False

    async def respond(self, context: ExecutionContext, message: NormalizedMessage) -> ChatResult:
        raise EngineUnavailable("Conversation engine is not available yet.")
