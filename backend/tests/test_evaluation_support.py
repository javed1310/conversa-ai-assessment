"""Evaluation privacy, transparent scoring and non-interfering observations."""

import importlib.util
import json
from pathlib import Path

import pytest

from app.contracts import LeadInput, SearchCoursesArguments


@pytest.fixture
def support():
    path = Path(__file__).resolve().parents[2] / "scripts/evaluation_support.py"
    spec = importlib.util.spec_from_file_location("evaluation_support_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_excludes_contacts_results_secrets_and_unknown_field_names(support):
    secret = "sensitive-customer-value-do-not-retain"
    event = support.safe_operation(
        "createLead",
        {"name": secret, "phone": "9876543210", secret: secret},
        LeadInput,
        {"createLead"},
        {"status": "failure", "code": secret, "data": {"secret": secret}},
    )
    encoded = json.dumps(event)
    assert secret not in encoded and "9876543210" not in encoded
    assert event["schemaValid"] is False and "unrecognized_field" in event["argumentFields"]


def test_unregistered_operation_name_is_not_retained(support):
    event = support.safe_operation("secret-in-tool-name", {}, None, set(), {})
    assert event["operation"] == "unregistered_operation"
    assert "secret-in-tool-name" not in json.dumps(event)


async def test_observer_preserves_result_identity_and_exactly_one_execution(support):
    result = {"status": "success", "data": {"courses": []}}
    calls = []

    async def operation(value):
        calls.append(value)
        return result

    events = []
    actual = await support.observe_call(
        operation,
        (42,),
        "searchCourses",
        {"category": "python"},
        SearchCoursesArguments,
        {"searchCourses"},
        events,
    )
    assert actual is result and calls == [42]
    assert events[0]["schemaValid"] is True


async def test_observer_preserves_exception_without_recording_its_message(support):
    error = RuntimeError("private exception content")

    async def operation():
        raise error

    events = []
    with pytest.raises(RuntimeError) as raised:
        await support.observe_call(
            operation,
            (),
            "searchCourses",
            {"category": "python"},
            SearchCoursesArguments,
            {"searchCourses"},
            events,
        )
    assert raised.value is error and "private exception content" not in json.dumps(events)


def test_citation_and_answer_are_scored_separately(support):
    step = {
        "answerable": True,
        "route": "knowledge",
        "sources": ["eligibility"],
        "patterns": ["basic programming"],
    }
    body = {
        "status": "completed",
        "reply": "It requires basic programming.",
        "actions": [],
        "sources": [{"businessId": "training-institute", "documentId": "refund"}],
    }
    checks = support.score_step(step, body, {}, None, "training-institute")
    assert checks["required_answer_facts"] is True
    assert checks["required_source"] is False and checks["no_unexpected_citation"] is False


def test_foreign_source_and_false_refusal_fail_independently(support):
    step = {"answerable": True, "route": "knowledge", "sources": ["eligibility"], "patterns": []}
    body = {
        "status": "completed",
        "reply": "I don't have that information.",
        "actions": [],
        "sources": [{"businessId": "fitness-studio", "documentId": "eligibility"}],
    }
    checks = support.score_step(step, body, {}, None, "training-institute")
    assert not checks["source_business_isolation"] and not checks["no_false_refusal"]


def test_explicit_undocumented_age_is_an_honest_refusal(support):
    step = {"answerable": False, "route": "unknown", "sources": ["eligibility"]}
    body = {
        "status": "completed",
        "reply": "This document does not establish age limits.",
        "sources": [{"businessId": "training-institute", "documentId": "eligibility"}],
    }
    assert support.score_step(step, body, {}, None, "training-institute")["honest_refusal"]


def test_retrieval_metric_does_not_claim_answer_accuracy(support):
    result = support.retrieval_metrics(
        [{"expectedDocuments": ["refund"], "topKHit": True, "admittedHit": False}]
    )
    assert result["topKRecall"] == 1 and result["thresholdAdmittedRecall"] == 0
    assert "accuracy" not in result


def test_heldout_and_development_splits_do_not_overlap():
    root = Path(__file__).resolve().parents[2] / "evaluation"
    heldout = json.loads((root / "heldout_cases.json").read_text())
    development = json.loads((root / "development_cases.json").read_text())
    questions = {step["question"] for case in heldout["cases"] for step in case["steps"]}
    assert not questions.intersection(case["question"] for case in development["cases"])
    identifiers = [case["id"] for case in heldout["cases"]]
    assert len(identifiers) == len(set(identifiers))
