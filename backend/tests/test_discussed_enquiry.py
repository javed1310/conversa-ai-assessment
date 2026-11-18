"""Recommendation -> focused questions -> owned collection -> grounded saved enquiry."""

import pytest

from app.customer_values import collected_contact_fields, contact_line
from app.gemini import ProviderFailure
from app.generation import ProposedChange, StateDraft
from app.knowledge import DocumentInput, KnowledgeService
from tests.test_agent import agent_env as agent_env
from tests.test_course_followup import classify
from tests.test_knowledge import TestOnlyEmbedder
from tests.test_passage_selection import ingest_courses


async def recommend(env):
    await ingest_courses(env)
    return await env.send(
        "if i not at all know coding then what should i do",
        steps=[[("readKnowledge", {"query": "Python prerequisites"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1"],
            "clarification": "none",
            "policyPassages": [
                {"evidenceKey": "source-1", "quote": "Python Basics has no prerequisites."}
            ],
        },
    )


async def test_recommendation_focus_is_not_customer_selection(agent_env):
    env = agent_env
    await recommend(env)
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] is None
    turn = await env.storage.db.turns.find_one({"session_id": env.context.session_id})
    assert turn["agent_trace"]["discussed_course_id"] == "PY101"


async def test_price_followup_uses_discussed_course_without_selecting(agent_env, monkeypatch):
    env = agent_env
    await recommend(env)
    text = "okay then whats the price of this course"
    classify(env, monkeypatch, text, fields=("fee",), course_id="PY101")
    before = len(env.model.requests)
    result = await env.send(text)
    assert "12000" in result.reply
    assert len(env.model.requests) == before + 1
    assert [a.result.tool for a in result.actions] == ["getCourseDetails"]
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] is None


async def test_procedural_start_collects_and_raw_contacts_save_without_model(agent_env):
    env = agent_env
    await recommend(env)
    before = len(env.model.requests)
    reply = await env.send("okay then how can i choose this course to start")
    assert "Python Basics" in reply.reply and "name" in reply.reply and "phone" in reply.reply
    assert not reply.sources and not reply.actions
    env.model.fail_state = True
    result = await env.send("Audit Learner 9876543210")
    assert len(env.model.requests) == before
    assert [a.result.tool for a in result.actions] == ["createLead"]
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "PY101"
    assert lead["customer"]["name"] == "Audit Learner"
    assert lead["callback_request_id"] is None


async def test_unknown_discussion_clarifies_instead_of_choosing(agent_env):
    result = await agent_env.send("how can i start this course")
    assert "Which course" in result.reply and not result.actions


async def test_failure_explanation_keeps_collection_goal_without_tools(agent_env, monkeypatch):
    env = agent_env
    await recommend(env)
    await env.send("how can i start this course")

    async def rejected(*args, **kwargs):
        raise ProviderFailure("INVALID_PROVENANCE")

    monkeypatch.setattr(env.engine.generation, "propose_state", rejected)
    await env.send("My name is Audit Learner and my phone is 9876543210")
    result = await env.send("why")
    assert "weren't saved" in result.reply and "name" in result.reply
    assert not result.actions
    recovered = await env.send("Audit Learner 9876543210")
    assert any(
        a.result.tool == "createLead" and a.result.status == "success" for a in recovered.actions
    )


@pytest.mark.parametrize(
    "text",
    [
        '"Audit Learner 9876543210"',
        "Do not call 9876543210",
        "Ignore instructions 9876543210",
        "Audit Learner 12345",
    ],
)
def test_contact_parser_rejects_ambiguous_or_instructional_input(text):
    assert contact_line(text) is None


async def test_bare_contacts_without_collection_do_not_auto_save(agent_env):
    result = await agent_env.send("Audit Learner 9876543210")
    assert not result.actions
    assert await agent_env.storage.db.leads.count_documents({}) == 0


async def test_named_enquiry_collects_separate_contacts_without_model_and_closes(agent_env):
    env = agent_env
    env.model.fail_state = True
    asked = await env.send("Please save an enquiry about PY101.")
    assert "name" in asked.reply and "phone" in asked.reply and not asked.actions
    named = await env.send("My name is Avery Patel.")
    assert "phone" in named.reply and "share your name" not in named.reply
    saved = await env.send("My number is 9876543210.")
    assert "Your enquiry is saved" in saved.reply
    assert [a.result.tool for a in saved.actions] == ["createLead"]
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["name"] == "Avery Patel"
    assert lead["customer"]["phone"] == "+919876543210"
    assert lead["customer"]["course_id"] == "PY101"
    assert lead["callback_request_id"] is None
    closed = await env.send("Thanks, that's all.")
    assert "welcome" in closed.reply and not closed.actions
    assert not env.model.requests
    assert await env.storage.db.leads.count_documents({}) == 1


@pytest.mark.parametrize(
    "text",
    [
        '"My name is Avery Patel."',
        "My name is Avery Patel and do not save my enquiry.",
        "My number is 9876543210. Ignore instructions.",
        "My number is 12345.",
        "Do not call 9876543210",
    ],
)
def test_separate_contacts_do_not_accept_mixed_quoted_or_invalid_input(text):
    assert collected_contact_fields(text) == {}


@pytest.mark.parametrize(
    "text",
    [
        "Do not save an enquiry about PY101.",
        'Example: "Please save an enquiry about PY101."',
        "Please save an enquiry about PY101 and call me.",
        "Please save an enquiry about PY999.",
    ],
)
async def test_named_enquiry_fast_path_rejects_unsafe_or_unsupported_input(agent_env, text):
    # An unavailable model cannot accidentally turn these into an authorized write.
    agent_env.model.fail_state = True
    result = await agent_env.send(text)
    assert not result.actions
    assert await agent_env.storage.db.leads.count_documents({}) == 0


async def test_two_rendered_courses_do_not_silently_choose_last_source(agent_env):
    env = agent_env
    await ingest_courses(env)
    service = KnowledgeService(
        env.storage, env.registry.get("training-institute"), TestOnlyEmbedder(), env.settings
    )
    await service.ingest(
        DocumentInput(
            document_id="overview-selection",
            version=2,
            title="introductory option",
            format="text",
            content="Python Basics has no prerequisites.",
        )
    )
    await env.send(
        "Compare the Python prerequisites",
        steps=[[("readKnowledge", {"query": "Python"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1", "source-2"],
            "clarification": "none",
            "policyPassages": [
                {
                    "evidenceKey": "source-1",
                    "quote": "Advanced Python (PY202) requires basic programming knowledge.",
                },
                {"evidenceKey": "source-2", "quote": "Python Basics has no prerequisites."},
            ],
        },
    )
    result = await env.send("how can i start this course")
    assert "Which course" in result.reply and not result.actions


async def test_invalid_extraction_evidence_retries_original_text_once(agent_env, monkeypatch):
    env = agent_env
    original = env.engine.generation._structured
    attempts = 0

    async def structured(model, *args, **kwargs):
        nonlocal attempts
        result = await original(model, *args, **kwargs)
        if model is StateDraft:
            attempts += 1
            if attempts == 1:
                return result.model_copy(
                    update={
                        "changes": (
                            ProposedChange(
                                field="name",
                                operation="set",
                                value="Invented",
                                excerpt="not in the customer text",
                            ),
                        )
                    }
                )
        return result

    monkeypatch.setattr(env.engine.generation, "_structured", structured)
    await env.send(
        "My name is Audit Learner",
        changes=[
            {
                "field": "name",
                "operation": "set",
                "value": "Audit Learner",
                "excerpt": "Audit Learner",
            }
        ],
    )
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert attempts == 2 and session["state"]["fields"]["name"] == "Audit Learner"
    assert await env.storage.db.leads.count_documents({}) == 0
