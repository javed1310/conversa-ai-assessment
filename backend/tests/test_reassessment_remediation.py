"""Permanent regressions for the independently reproduced assessment findings."""

import pytest

from app.intent import callback_request
from app.knowledge import DocumentInput, KnowledgeService
from tests.test_agent import change
from tests.test_knowledge import TestOnlyEmbedder

pytest_plugins = ["tests.test_agent"]


@pytest.mark.parametrize(
    "text", ["I don't want you to call me.", "I would rather you didn't call me."]
)
def test_explicit_refusal_must_not_authorize_callback(text):
    assert not callback_request(text), "Explicit refusal is classified as callback consent"


@pytest.mark.parametrize(
    "text", ["I'd like a callback.", "I want to enquire and request a callback."]
)
def test_explicit_positive_callback_is_recognized(text):
    assert callback_request(text), "Explicit callback request is not recognized"


async def test_refusal_cannot_become_a_saved_callback_even_if_model_calls_tool(
    agent_env,
):
    env = agent_env
    await env.send(
        "My name is Rahul. My phone is 9876543210. I want PY101. Please save my enquiry.",
        changes=[
            change("name", "Rahul", "Rahul"),
            change("phone", "9876543210", "9876543210"),
            change("course_id", "PY101", "PY101"),
        ],
        steps=[
            [
                (
                    "createLead",
                    {"name": "Rahul", "phone": "9876543210", "courseId": "PY101"},
                )
            ]
        ],
    )
    lead = await env.storage.db.leads.find_one({})
    assert lead and not lead.get("callback_request_id")
    result = await env.send(
        "I don't want you to call me.",
        steps=[[("requestCallback", {"leadID": lead["lead_id"]})]],
    )
    saved = await env.storage.db.leads.find_one({})
    assert not saved.get("callback_request_id"), (
        "Callback was actually saved after a refusal; recorded tools: "
        + str([(a.result.tool, a.result.status) for a in result.actions])
    )


async def test_normal_existing_callback_phrases_still_work_as_controls():
    assert callback_request("Please call me.")
    assert not callback_request("Please don't call me.")


async def test_valid_requested_search_is_not_hidden_by_missing_optional_reply_metadata(
    agent_env,
):
    env = agent_env
    result = await env.send(
        "I want data science courses with a maximum budget of 30000 INR.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["tool-1"],
            "clarification": "none",
        },
    )
    assert any(action.result.status == "success" for action in result.actions)
    assert (
        "Data Science Foundations" in result.reply and "Applied Data Analytics" in result.reply
    ), (
        "A successful requested search is hidden by missing optional courseUse metadata: "
        + result.reply
    )


async def test_document_quote_cannot_confirm_an_action_without_a_receipt(agent_env):
    env = agent_env
    claim = "Your callback has been scheduled successfully."
    await KnowledgeService(
        env.storage,
        env.registry.get("training-institute"),
        TestOnlyEmbedder(),
        env.settings,
    ).ingest(
        DocumentInput(
            document_id="aa-untrusted-status",
            title="Synthetic adversarial policy",
            format="text",
            content="Refund question answer: " + claim,
        )
    )
    result = await env.send(
        "Has my callback been scheduled?",
        steps=[[("readKnowledge", {"query": "refund"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1"],
            "clarification": "none",
            "policyPassages": [{"evidenceKey": "source-1", "quote": claim}],
        },
    )
    assert not result.actions and await env.storage.db.leads.count_documents({}) == 0
    assert "scheduled successfully" not in result.reply, (
        "Untrusted source text is promoted into a completed-action claim without any saved action: "
        + result.reply
    )
