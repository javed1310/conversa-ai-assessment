"""Validated domain contracts; wire JSON is camelCase, Python is snake_case."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
]
Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Content = Annotated[Text, Field(max_length=8000)]
Budget = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Channel = Literal["web", "mock"]
Schedule = Literal["weekday", "weekend"]
ToolName = Identifier  # Registered identifiers, not a closed domain-specific enumeration.
CustomerField = Literal["name", "phone", "budget", "schedule", "course_id", "stated_interest"]


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class NormalizedMessage(Contract):
    """Adapter output; claimed identities must still be resolved by the gateway."""

    message_id: Identifier
    business_id: Identifier
    channel: Channel
    sender_id: Identifier
    session_id: Identifier
    content: Content
    timestamp: AwareDatetime

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ExecutionContext(Contract):
    """Constructed only by server ownership checks; never merged with tool arguments."""

    request_id: UUID
    business_id: Identifier
    channel: Channel
    sender_id: Identifier
    session_id: Identifier
    turn_id: Identifier
    lease_token: Identifier | None = None


class SearchCoursesArguments(Contract):
    category: Literal["data-science", "python"]
    budget: Budget
    schedule: Schedule | None = None


class CourseDetailsArguments(Contract):
    course_id: Identifier = Field(alias="courseID")


class CustomerFields(Contract):
    name: Annotated[Text, Field(max_length=200)] | None = None
    phone: str | None = None
    budget: Budget | None = None
    schedule: Schedule | None = None
    course_id: Identifier | None = None
    stated_interest: Annotated[Text, Field(max_length=1000)] | None = None

    @field_validator("phone")
    @classmethod
    def canonical_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        compact = re.sub(r"[\s-]", "", value)
        if compact.startswith("+91"):
            compact = compact[3:]
        if not re.fullmatch(r"[6-9][0-9]{9}", compact):
            raise ValueError("Expected an Indian mobile number, optionally prefixed with +91")
        return "+91" + compact


class CustomerChanges(CustomerFields):
    @model_validator(mode="after")
    def no_implicit_clear(self) -> "CustomerChanges":
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Use clearFields for an explicit clear; omit unchanged fields")
        return self


class StateProposal(Contract):
    """A proposal, not trusted state. M08 verifies evidence against the actual turn."""

    set_fields: CustomerChanges = Field(default_factory=CustomerChanges)
    clear_fields: tuple[CustomerField, ...] = ()

    @model_validator(mode="after")
    def no_conflicting_updates(self) -> "StateProposal":
        if len(set(self.clear_fields)) != len(self.clear_fields):
            raise ValueError("Duplicate clear field")
        if set(self.clear_fields) & self.set_fields.model_fields_set:
            raise ValueError("A field cannot be set and cleared together")
        return self


class CustomerEvidence(Contract):
    source: Literal["customer"] = "customer"
    turn_id: Identifier
    excerpt: Annotated[Text, Field(max_length=1000)]


class CallbackIntent(Contract):
    requested: Literal[True]
    evidence: CustomerEvidence


class DisplayedCourse(Contract):
    course_id: Identifier
    name: Text


class CustomerState(Contract):
    fields: CustomerFields = Field(default_factory=CustomerFields)
    provenance: dict[CustomerField, CustomerEvidence] = Field(default_factory=dict)
    last_displayed_courses: tuple[DisplayedCourse, ...] = ()
    callback_intent: CallbackIntent | None = None
    selection_conflicts: tuple[Literal["budget", "schedule", "full", "missing_course"], ...] = ()

    @model_validator(mode="after")
    def evidence_matches_fields(self) -> "CustomerState":
        populated = {
            field
            for field in CustomerFields.model_fields
            if getattr(self.fields, field) is not None
        }
        if populated != set(self.provenance):
            raise ValueError("Every collected field needs customer evidence; no orphan evidence")
        ids = [course.course_id for course in self.last_displayed_courses]
        if len(ids) != len(set(ids)):
            raise ValueError("Displayed courses must have distinct IDs")
        return self


class LeadInput(CustomerFields):
    name: Annotated[Text, Field(max_length=200)]
    phone: str

    @model_validator(mode="after")
    def qualified_interest(self) -> "LeadInput":
        if self.course_id is None and self.stated_interest is None:
            raise ValueError("A course or stated interest is required")
        return self


class CallbackArguments(Contract):
    lead_id: Identifier = Field(alias="leadID")


class Summary(Contract):
    lead_id: Identifier
    customer_name: Text
    summary: Content
    intent: Text
    qualification_status: Literal["qualified", "incomplete"]
    recommended_action: Text


class AutomationEvent(Contract):
    event_id: Identifier
    type: Literal["lead.created"] = "lead.created"
    status: Literal["pending", "processing", "completed", "failed"] = "pending"
    attempts: Annotated[int, Field(strict=True, ge=0)] = 0
    summary: Summary | None = None
    claim_token: Identifier | None = None
    lease_until: AwareDatetime | None = None
    next_attempt_at: AwareDatetime | None = None
    failure_code: Identifier | None = None
    retryable: bool = False
    manual_retries: Annotated[int, Field(strict=True, ge=0)] = 0


class LeadSnapshot(Contract):
    lead_id: Identifier
    business_id: Identifier
    session_id: Identifier
    created_at: AwareDatetime
    customer: LeadInput
    automation: AutomationEvent
    callback_requested_at: AwareDatetime | None = None
    callback_request_id: Identifier | None = None

    @model_validator(mode="after")
    def summary_belongs_to_lead(self) -> "LeadSnapshot":
        if self.automation.summary and self.automation.summary.lead_id != self.lead_id:
            raise ValueError("Summary must belong to its lead")
        return self


class StateReceipt(Contract):
    stage: Identifier
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class Session(Contract):
    session_id: Identifier
    business_id: Identifier
    channel: Channel
    sender_id: Identifier
    created_at: AwareDatetime
    updated_at: AwareDatetime
    state: CustomerState = Field(default_factory=CustomerState)
    lead_id: Identifier | None = None
    state_revision: Annotated[int, Field(strict=True, ge=0)] = 0
    state_sequence: Annotated[int, Field(strict=True, ge=0)] = 0
    state_turn_id: Identifier | None = None
    state_steps: Annotated[tuple[StateReceipt, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def chronological(self) -> "Session":
        if self.updated_at < self.created_at:
            raise ValueError("Session update cannot precede creation")
        return self


class SourceRef(Contract):
    business_id: Identifier
    knowledge_base_id: Identifier
    document_id: Identifier
    document_version: Annotated[int, Field(strict=True, ge=1)]
    chunk_id: Identifier
    title: Text
    excerpt: Content


class ToolSuccess(Contract):
    status: Literal["success"] = "success"
    call_id: Identifier
    tool: ToolName
    # Concrete result payloads are added with the actual tool implementations in M04/M10.
    data: dict[str, object]


class ToolFailure(Contract):
    status: Literal["failure"] = "failure"
    call_id: Identifier
    tool: Identifier
    code: Literal[
        "INVALID_ARGUMENTS",
        "NOT_ALLOWED",
        "NOT_FOUND",
        "CONFLICT",
        "UNAVAILABLE",
        "INVALID_RESULT",
        "IN_PROGRESS",
        "QUALIFICATION_REQUIRED",
        "INVALID_PROVENANCE",
        "CALLBACK_INTENT_REQUIRED",
        "PREFERENCES_CONFLICT",
        "SAVED_DETAILS_CHANGED",
        "OUTCOME_UNKNOWN",
    ]
    retryable: bool


ToolResult = Annotated[ToolSuccess | ToolFailure, Field(discriminator="status")]


class ActionResult(Contract):
    """Only validated execution results enter this customer-visible trace."""

    result: ToolResult
    execution_id: Identifier
    replayed: bool = False
    handler_invoked: bool = False


class ChatResult(Contract):
    status: Literal["completed"] = "completed"
    message_id: Identifier
    session_id: Identifier
    reply: Content
    actions: tuple[ActionResult, ...] = ()
    sources: tuple[SourceRef, ...] = ()


class ProcessingResult(Contract):
    status: Literal["processing"] = "processing"
    message_id: Identifier
    session_id: Identifier


class ErrorDetail(Contract):
    code: Text
    message: Text
    retryable: bool
    request_id: UUID


class ErrorEnvelope(Contract):
    error: ErrorDetail


class ReadinessResult(Contract):
    status: Literal["ready"] = "ready"
    database: Literal["available"] = "available"
