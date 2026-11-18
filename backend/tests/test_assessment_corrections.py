"""Regressions from actual shorthand extraction and accepted reordered drafts."""

import json

import pytest

from app.budgets import budget_is_supported, normalize_budget
from app.contracts import ActionResult, ChatResult, ToolSuccess
from app.customer_values import supports_value
from app.generation import GenerationService
from app.reliability_cli import search_result
from app.settings import REPOSITORY_ROOT
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change
from tests.test_generation import client_for, structured


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20k", 20000),
        ("20 K", 20000),
        ("20 thousand", 20000),
        ("0.2 lakh", 20000),
        ("20,000", 20000),
        ("\u20b920,000", 20000),
        ("INR 20,000", 20000),
        ("Rs. 20000", 20000),
        ("20000 INR", 20000),
        ("20.5k", 20500),
        ("0", 0),
    ],
)
def test_one_amount_grammar_for_proposal_and_customer_proof(raw, expected):
    assert normalize_budget(raw) == expected
    assert supports_value("budget", expected, f"My budget is {raw}.")


@pytest.mark.parametrize(
    "raw", ["nan", "inf", "-20k", "+20k", "12,34", "20m", "20k20", "1.2.3", "twenty thousand"]
)
def test_invalid_amount_cannot_be_salvaged(raw):
    with pytest.raises(ValueError):
        normalize_budget(raw)
    assert not budget_is_supported(20, raw)


@pytest.mark.parametrize(
    "value,text", [(20, "20k"), (20000, "30k"), (1234, "12,34"), (20, "abc20k"), (20, "-20k")]
)
def test_proof_does_not_drop_units_prefixes_or_malformed_groups(value, text):
    assert not budget_is_supported(value, text)


@pytest.mark.parametrize("raw", ["20k", "\u20b920,000", "0.2 lakh"])
async def test_actual_raw_model_forms_are_normalized_before_provenance(raw):
    client = client_for(
        structured(
            {"changes": [{"field": "budget", "operation": "set", "value": raw, "excerpt": raw}]}
        )
    )
    async with client:
        result = await GenerationService(client).propose_state(
            "budget-turn", f"Actually, my budget is {raw}"
        )
    assert result.proposal.set_fields.budget == 20000


async def test_30k_to_raw_20k_updates_the_next_actual_search(agent_env):
    env = agent_env
    await env.send(
        "I want data science courses with a maximum budget of 30000 INR.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("Actually, my budget is 20k", changes=[change("budget", "20k", "20k")])
    state = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert state["state"]["fields"]["budget"] == 20000
    result = await env.send(
        "Which data science courses fit that budget now?",
        steps=[[("searchCourses", {"category": "data-science", "budget": 20000})]],
    )
    actual = search_result(result)
    assert actual is not None and not actual.courses
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_reversed_draft_cannot_change_visible_order_or_second_course(agent_env, monkeypatch):
    env = agent_env
    reviews = []

    async def unexpected_review(*args, **kwargs):
        reviews.append(True)
        raise AssertionError("Ordered search rows must not reach conversational review")

    monkeypatch.setattr(env.engine.generation, "review_reply", unexpected_review)
    selection = {
        "kind": "evidence",
        "evidenceKeys": ["tool-1"],
        "clarification": "none",
        "courseUse": "overview",
        "conversationalDraft": {
            "sentences": [
                {
                    "text": (
                        "Applied Data Analytics (DS202) costs INR 28000, "
                        "runs on weekends, and is available."
                    ),
                    "evidenceKeys": ["tool-1"],
                },
                {
                    "text": (
                        "Data Science Foundations (DS101) costs INR 25000, "
                        "runs on weekdays, and is available."
                    ),
                    "evidenceKeys": ["tool-1"],
                },
            ]
        },
    }
    result = await env.send(
        "I want data science courses under INR 30000.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
        selection=selection,
    )
    assert "DS101" in result.reply, result
    assert result.reply.index("DS101") < result.reply.index("DS202") and not reviews
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert [c["course_id"] for c in session["state"]["last_displayed_courses"]] == [
        "DS101",
        "DS202",
    ]
    next_reply = await env.send("second one")
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] == "DS202"
    assert (
        "Applied Data Analytics" in next_reply.reply
        and "Data Science Foundations" not in next_reply.reply
    )


def receipt(courses, text):
    return ChatResult(
        message_id="search",
        session_id="session",
        reply=text,
        actions=(
            ActionResult(
                execution_id="execution",
                result=ToolSuccess(call_id="call", tool="searchCourses", data={"courses": courses}),
            ),
        ),
    )


def test_live_gate_uses_receipts_when_reply_omits_ids_or_changes_wording():
    courses = json.loads(
        (REPOSITORY_ROOT / "businesses/training-institute/data/courses.json").read_text()
    )[:2]
    result = search_result(
        receipt(
            courses,
            "We have Data Science Foundations and Applied Data Analytics within your budget.",
        )
    )
    assert result is not None and {c.course_id for c in result.courses} == {"DS101", "DS202"}
    empty = search_result(
        receipt([], "There aren't any options within your revised spending limit.")
    )
    assert empty is not None and not empty.courses
    assert search_result(ChatResult(message_id="x", session_id="s", reply="DS101 DS202")) is None


async def test_acknowledgement_requires_a_current_committed_budget(agent_env):
    env = agent_env
    acknowledgement = {
        "kind": "evidence",
        "evidenceKeys": [],
        "clarification": "none",
        "dialogueAct": "acknowledge_preference",
    }
    reply = await env.send(
        "Actually, my budget is 20k",
        changes=[change("budget", "20k", "20k")],
        selection=acknowledgement,
    )
    assert (
        "your budget is now INR 20000" in reply.reply and "verified information" not in reply.reply
    )
    later = await env.send("Tell me about instructors", selection=acknowledgement)
    assert "your budget is now" not in later.reply


async def test_unsupported_question_is_not_replaced_by_budget_acknowledgement(agent_env):
    reply = await agent_env.send(
        "My budget is 20k. Are jobs guaranteed?",
        changes=[change("budget", "20k", "20k")],
        selection={
            "kind": "unavailable",
            "evidenceKeys": [],
            "clarification": "none",
            "dialogueAct": "acknowledge_preference",
        },
    )
    assert "your budget is now" not in reply.reply and "verified information" in reply.reply


async def test_optional_acknowledgement_cue_is_not_required_for_saved_budget(agent_env):
    reply = await agent_env.send(
        "Actually, my budget is 20k",
        changes=[change("budget", "20k", "20k")],
        selection={"kind": "evidence", "evidenceKeys": [], "clarification": "none"},
    )
    assert (
        "your budget is now INR 20000" in reply.reply and "verified information" not in reply.reply
    )


async def test_mixed_question_cannot_use_state_only_acknowledgement_fallback(agent_env):
    reply = await agent_env.send(
        "My budget is 20k. Are jobs guaranteed?",
        changes=[change("budget", "20k", "20k")],
        selection={"kind": "evidence", "evidenceKeys": [], "clarification": "none"},
    )
    assert "your budget is now" not in reply.reply and "verified information" in reply.reply
