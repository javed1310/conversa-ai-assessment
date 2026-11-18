"""Adapter contract and registry. Core engine imports no channel implementation."""

from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, TypeAdapter

from app.contracts import (
    Channel,
    ChatResult,
    Content,
    Contract,
    ExecutionContext,
    Identifier,
    NormalizedMessage,
    ProcessingResult,
)


class WebMessage(Contract):
    message_id: Identifier
    content: Content
    timestamp: AwareDatetime


class SessionStart(Contract):
    """Empty JSON request; the client cannot choose server identity or channel."""


class MockMessage(Contract):
    event_id: Identifier = Field(alias="event_id")
    author: Identifier
    text: Content
    timestamp: AwareDatetime


class MockReply(Contract):
    channel: Literal["mock"] = "mock"
    recipient: Identifier
    in_reply_to: Identifier = Field(alias="in_reply_to")
    result: ChatResult | ProcessingResult


class SessionStarted(Contract):
    business_id: Identifier
    session_id: Identifier
    channel: Literal["web"] = "web"
    session_token: str = Field(min_length=40, max_length=100)
    engine_ready: bool


class Delivery(Contract):
    channel: Channel
    recipient_id: Identifier
    payload: ChatResult | ProcessingResult


class ChannelAdapter(Protocol):
    @property
    def channel(self) -> Channel: ...

    def normalize(
        self, payload: dict[str, object], context: ExecutionContext
    ) -> NormalizedMessage: ...

    def encode(
        self, result: ChatResult | ProcessingResult, context: ExecutionContext
    ) -> Delivery: ...


class WebAdapter:
    channel: Channel = "web"

    def normalize(self, payload: dict[str, object], context: ExecutionContext) -> NormalizedMessage:
        incoming = WebMessage.model_validate(payload)
        if context.channel != self.channel:
            raise ValueError("Wrong originating channel")
        return NormalizedMessage(
            **incoming.model_dump(),
            business_id=context.business_id,
            channel=context.channel,
            sender_id=context.sender_id,
            session_id=context.session_id,
        )

    def encode(self, result: ChatResult | ProcessingResult, context: ExecutionContext) -> Delivery:
        if context.channel != self.channel or result.session_id != context.session_id:
            raise ValueError("Reply routing mismatch")
        return Delivery(channel=self.channel, recipient_id=context.sender_id, payload=result)


class MockAdapter:
    channel: Channel = "mock"

    def normalize(self, payload: dict[str, object], context: ExecutionContext) -> NormalizedMessage:
        incoming = MockMessage.model_validate(payload)
        if context.channel != self.channel or incoming.author != context.sender_id:
            raise ValueError("Wrong originating channel or sender")
        return NormalizedMessage(
            message_id=incoming.event_id,
            content=incoming.text,
            timestamp=incoming.timestamp,
            business_id=context.business_id,
            channel=context.channel,
            sender_id=context.sender_id,
            session_id=context.session_id,
        )

    def encode(self, result: ChatResult | ProcessingResult, context: ExecutionContext) -> Delivery:
        if context.channel != self.channel or result.session_id != context.session_id:
            raise ValueError("Reply routing mismatch")
        return Delivery(channel=self.channel, recipient_id=context.sender_id, payload=result)


class AdapterRegistry:
    def __init__(self, adapters: tuple[ChannelAdapter, ...]) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}
        for adapter in adapters:
            TypeAdapter(Channel).validate_python(adapter.channel)
            if adapter.channel in self._adapters:
                raise ValueError("Duplicate channel registration")
            self._adapters[adapter.channel] = adapter

    def get(self, channel: Channel) -> ChannelAdapter:
        if channel not in self._adapters:
            raise ValueError("Channel adapter unavailable")
        return self._adapters[channel]
