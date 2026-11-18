import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.contracts import (
    CallbackArguments,
    CallbackIntent,
    ChatResult,
    CourseDetailsArguments,
    CustomerChanges,
    CustomerEvidence,
    CustomerFields,
    CustomerState,
    DisplayedCourse,
    ExecutionContext,
    LeadInput,
    LeadSnapshot,
    NormalizedMessage,
    SearchCoursesArguments,
    Session,
    StateProposal,
    ToolResult,
)


def message() -> dict[str, Any]:
    return dict(
        messageId="msg-001",
        businessId="training-institute",
        channel="web",
        senderId="visitor-001",
        sessionId="session-001",
        content="Course enquiry",
        timestamp="2026-11-14T12:00:00+05:30",
    )


def test_message_aliases_utc_and_server_context_separation() -> None:
    result = NormalizedMessage.model_validate(message())
    assert result.timestamp.hour == 6 and result.timestamp.minute == 30
    assert result.timestamp.utcoffset().total_seconds() == 0
    wire = result.model_dump(mode="json", by_alias=True)
    assert wire["messageId"] == "msg-001" and "message_id" not in wire
    assert NormalizedMessage.model_validate_json(json.dumps(wire)) == result
    context = ExecutionContext(
        request_id=uuid4(),
        business_id=result.business_id,
        channel="web",
        sender_id=result.sender_id,
        session_id=result.session_id,
        turn_id="turn-001",
    )
    assert context.business_id == result.business_id
    for trusted in ("businessId", "sessionId", "senderId", "requestId", "context"):
        with pytest.raises(ValidationError):
            SearchCoursesArguments.model_validate(
                {"category": "python", "budget": 20000, trusted: "injected"}
            )
    with pytest.raises(ValidationError):
        CallbackArguments.model_validate({"leadID": "lead-001", "businessId": "foreign"})
    assert CourseDetailsArguments.model_validate({"courseID": "DS202"}).course_id == "DS202"


@pytest.mark.parametrize(
    "field,value",
    [
        ("messageId", ""),
        ("businessId", "../foreign"),
        ("sessionId", 123),
        ("content", "  \n"),
        ("content", "x" * 8001),
        ("content", {}),
        ("timestamp", "2026-11-14T12:00:00"),
        ("timestamp", "invalid"),
        ("channel", "whatsapp"),
    ],
)
def test_invalid_normalized_message(field: str, value: object) -> None:
    payload = message()
    payload[field] = value
    with pytest.raises(ValidationError):
        NormalizedMessage.model_validate(payload)


@pytest.mark.parametrize("budget", [True, -1, float("inf"), float("nan"), "20000", {}])
def test_invalid_budgets_fail_without_coercion(budget: object) -> None:
    with pytest.raises(ValidationError):
        SearchCoursesArguments.model_validate({"category": "python", "budget": budget})


def test_lead_qualification_optional_preferences_and_phone_scope() -> None:
    lead = LeadInput(name="  Javed  ", phone="98765-43210", stated_interest="Python learning")
    assert lead.name == "Javed" and lead.phone == "+919876543210"
    assert lead.budget is None and lead.schedule is None and lead.course_id is None
    assert LeadInput(name="नाम", phone="+91 9876543210", course_id="DS202").name == "नाम"
    for change in (
        {"phone": "123"},
        {"name": " "},
        {"phone": True},
        {"course_id": None, "stated_interest": None},
    ):
        with pytest.raises(ValidationError):
            LeadInput.model_validate(
                dict(name="Javed", phone="9876543210", course_id="DS101") | change
            )


def test_state_proposals_avoid_implicit_clears_and_invented_values() -> None:
    assert CustomerState().fields.model_dump() == dict.fromkeys(CustomerFields.model_fields)
    assert not CustomerState().provenance and CustomerState().callback_intent is None
    assert CustomerChanges(budget=20000).model_fields_set == {"budget"}
    assert StateProposal(clear_fields=("budget",)).clear_fields == ("budget",)
    for payload in (
        {"setFields": {"phone": None}},
        {"setFields": {"budget": 20000}, "clearFields": ["budget"]},
        {"clearFields": ["budget", "budget"]},
    ):
        with pytest.raises(ValidationError):
            StateProposal.model_validate(payload)
    evidence = CustomerEvidence(turn_id="turn-001", excerpt="I have a budget of 20000")
    state = CustomerState(
        fields=CustomerFields(budget=20000),
        provenance={"budget": evidence},
        last_displayed_courses=(
            DisplayedCourse(course_id="DS101", name="First"),
            DisplayedCourse(course_id="DS202", name="Second"),
        ),
    )
    assert state.last_displayed_courses[1].course_id == "DS202"
    with pytest.raises(ValidationError):
        CustomerState(fields=CustomerFields(phone="9876543210"))
    with pytest.raises(ValidationError):
        CustomerEvidence.model_validate({"source": "assistant", "turnId": "turn-1", "excerpt": "x"})
    with pytest.raises(ValidationError):
        CallbackIntent.model_validate({"requested": True})


def test_typed_results_and_no_raw_exception_failure_field() -> None:
    adapter = TypeAdapter(ToolResult)
    failure = {
        "status": "failure",
        "callId": "call-1",
        "tool": "createLead",
        "code": "UNAVAILABLE",
        "retryable": True,
    }
    assert adapter.validate_python(failure).status == "failure"
    with pytest.raises(ValidationError):
        adapter.validate_python(failure | {"exception": "private-provider-error"})
    with pytest.raises(ValidationError):
        adapter.validate_python(failure | {"status": "success"})
    result = ChatResult(message_id="msg-1", session_id="session-1", reply="Please try again.")
    assert not result.actions and not result.sources
    assert "discriminator" in adapter.json_schema()


def test_session_rejects_naive_or_backward_time() -> None:
    now = datetime.now(UTC)
    values = dict(
        session_id="session-1",
        business_id="training-institute",
        channel="web",
        sender_id="sender-1",
        created_at=now,
        updated_at=now,
    )
    assert Session.model_validate(values).state == CustomerState()
    with pytest.raises(ValidationError):
        Session.model_validate(values | {"updated_at": "2020-01-01T00:00:00Z"})
    with pytest.raises(ValidationError):
        Session.model_validate(values | {"created_at": "2026-11-14T12:00:00"})


def test_saved_lead_summary_cannot_reference_another_lead() -> None:
    values = dict(
        leadId="lead-1",
        businessId="training-institute",
        sessionId="session-1",
        createdAt="2026-11-14T12:00:00Z",
        customer={"name": "Javed", "phone": "9876543210", "courseId": "DS202"},
        automation={"eventId": "event-1"},
    )
    assert LeadSnapshot.model_validate(values).automation.status == "pending"
    values["automation"] = {
        "eventId": "event-1",
        "summary": {
            "leadId": "other-lead",
            "customerName": "Javed",
            "summary": "Course enquiry",
            "intent": "course enquiry",
            "qualificationStatus": "qualified",
            "recommendedAction": "Follow up",
        },
    }
    with pytest.raises(ValidationError):
        LeadSnapshot.model_validate(values)
