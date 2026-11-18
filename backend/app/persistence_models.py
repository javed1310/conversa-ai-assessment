"""Validated persisted records; private Mongo _id and counters stay out of API models."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from app.contracts import (
    Budget,
    CallbackIntent,
    Channel,
    ChatResult,
    Content,
    Contract,
    CustomerState,
    Identifier,
    LeadSnapshot,
    Schedule,
    Summary,
    Text,
)
from app.dialogue import NextStep


class Course(Contract):
    business_id: Identifier
    course_id: Identifier
    name: Text
    category: Literal["data-science", "python"]
    fee: Budget
    currency: Literal["INR"] = "INR"
    schedule: Schedule
    availability: Literal["available", "full"]


class Membership(Contract):
    business_id: Identifier
    membership_id: Identifier
    name: Text
    fee: Budget
    currency: Literal["INR"] = "INR"
    duration_months: Annotated[int, Field(strict=True, ge=1)]
    description: Text
    fictional: Literal[True] = True


class StateCheckpoint(Contract):
    stage: Identifier
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: Annotated[int, Field(strict=True, ge=0)]
    request_json: str = Field(max_length=16384)
    after: CustomerState


class AgentTrace(Contract):
    discussed_course_id: Identifier | None = None
    next_step: NextStep = "none"
    dialogue_course_id: Identifier | None = None
    operations: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    outcome: Literal[
        "greeting",
        "clarify",
        "evidence",
        "unavailable",
        "provider_failure",
        "state_rejected",
        "social",
    ]
    failure_codes: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()


class Turn(Contract):
    turn_id: Identifier
    business_id: Identifier
    channel: Channel
    sender_id: Identifier
    session_id: Identifier
    message_id: Identifier
    sequence: Annotated[int, Field(strict=True, ge=1)]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_text: Content
    input_timestamp: AwareDatetime
    created_at: AwareDatetime
    status: Literal["processing", "completed", "failed"] = "processing"
    output: ChatResult | None = None
    prepared_output: ChatResult | None = None
    completed_at: AwareDatetime | None = None
    processing_token: Identifier | None = None
    state_checkpoints: Annotated[tuple[StateCheckpoint, ...], Field(max_length=8)] = ()
    agent_trace: AgentTrace | None = None

    @model_validator(mode="after")
    def output_matches_identity(self) -> "Turn":
        if self.prepared_output is not None and (
            self.prepared_output.message_id != self.message_id
            or self.prepared_output.session_id != self.session_id
            or (self.output is not None and self.output != self.prepared_output)
        ):
            raise ValueError("Prepared output must belong to this turn and terminal result")
        if self.status == "completed" and (self.output is None or self.completed_at is None):
            raise ValueError("Completed turn needs output and completion time")
        if self.output is not None and (
            self.status != "completed"
            or self.output.message_id != self.message_id
            or self.output.session_id != self.session_id
        ):
            raise ValueError("Output must belong to its completed turn")
        return self


class HistoryEntry(Contract):
    turn_id: Identifier
    role: Literal["customer", "assistant"]
    content: Content


class MockNotification(Contract):
    notification_id: Identifier
    event_id: Identifier
    lead_id: Identifier
    destination: Literal["simulated-crm"] = "simulated-crm"
    created_at: AwareDatetime
    payload: Summary


class LeadRecord(LeadSnapshot):
    # At most 25 turns / 50 entries: never embed unbounded conversation history.
    automation_input: Annotated[tuple[HistoryEntry, ...], Field(max_length=50)]
    notification: MockNotification | None = None
    automation_callback_intent: CallbackIntent | None = None

    @model_validator(mode="after")
    def notification_matches_event(self) -> "LeadRecord":
        if self.notification and (
            self.notification.lead_id != self.lead_id
            or self.notification.event_id != self.automation.event_id
            or self.notification.payload != self.automation.summary
        ):
            raise ValueError("Notification must belong to the saved summary and event")
        return self
