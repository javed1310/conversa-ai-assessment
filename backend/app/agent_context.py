"""Per-turn work only; domain hints preserve the existing persisted state format."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.contracts import ActionResult, ChatResult, CustomerState, SourceRef
from app.dialogue import NextStep
from app.generation import CourseQuestion, EnquiryRequest, SocialReply

if TYPE_CHECKING:
    from app.behavior import BusinessBehavior


@dataclass
class Evidence:
    key: str
    text: str
    source: SourceRef | None = None
    action: ActionResult | None = None


@dataclass
class TurnWork:
    behavior: BusinessBehavior | None = None
    discussed_course_id: str | None = None
    social_reply: SocialReply | None = None
    course_question: CourseQuestion | None = None
    enquiry_request: EnquiryRequest | None = None
    next_step: NextStep = "none"
    dialogue_course_id: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    actions: list[ActionResult] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    response_evidence: dict[str, str] = field(default_factory=dict)
    response_locked: bool = False
    choice_unresolved: bool = False
    resolved_reference: str | None = None
    phone_rejected: bool = False


@dataclass
class PreparedTurn:
    state: CustomerState
    output: ChatResult | None = None
    outcome: str = "evidence"
