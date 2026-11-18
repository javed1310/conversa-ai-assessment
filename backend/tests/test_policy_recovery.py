"""Policy quotes recover after a saved enquiry without repeating business actions."""

import json

import pytest

from app.knowledge import DocumentInput, KnowledgeService
from app.settings import REPOSITORY_ROOT
from tests.test_agent import agent_env as agent_env
from tests.test_assessment_handoff import discussed
from tests.test_knowledge import TestOnlyEmbedder


async def saved_enquiry(env):
    await discussed(env)
    await env.send("Great, I am interested. Can someone call me?")
    await env.send("Rahul, 9876543210.")
    service = KnowledgeService(
        env.storage, env.registry.get("training-institute"), TestOnlyEmbedder(), env.settings
    )
    for name in ["eligibility", "refund"]:
        await service.ingest(
            DocumentInput(
                document_id="audit-" + name,
                title=name,
                format="markdown",
                content=(
                    REPOSITORY_ROOT / f"businesses/training-institute/knowledge/{name}.md"
                ).read_text(),
            )
        )


@pytest.mark.parametrize("kind", ["evidence", "unavailable"])
@pytest.mark.parametrize(
    "question,quote,document",
    [
        (
            "Can I join Advanced Python if I have never programmed before?",
            "Advanced Python (PY202) requires basic programming knowledge.",
            "audit-eligibility",
        ),
        (
            "Can I get a refund if I paid within seven days but already attended the first class?",
            "the learner has not attended the first class.",
            "audit-refund",
        ),
    ],
)
async def test_focused_policy_recovery_after_callback(agent_env, kind, question, quote, document):
    env = agent_env
    await saved_enquiry(env)

    def passages(data):
        item = next(d for d in data["documents"] if quote in d["text"])
        return {"passages": [{"evidenceKey": item["key"], "quote": quote}]}

    env.model.policy_selection = passages
    before = len(env.model.requests)
    reply = await env.send(
        question,
        steps=[[("readKnowledge", {"query": question})]],
        selection={"kind": kind, "evidenceKeys": [], "clarification": "none"},
    )
    assert reply.sources and reply.sources[0].document_id == document
    assert not reply.actions
    if document == "audit-refund":
        assert (
            "seven days" in reply.reply and "first class" in reply.reply and "BOTH" in reply.reply
        )
    else:
        assert "basic programming knowledge" in reply.reply
    calls = env.model.requests[before:]
    recovery = [
        r
        for r in calls
        if r.get("generationConfig", {}).get("responseJsonSchema", {}).get("title")
        == "PolicySelection"
    ]
    assert len(recovery) == 1
    data = json.loads(recovery[0]["contents"][0]["parts"][0]["text"])
    assert set(data) == {"currentCustomerText", "documents"}
    assert all(d["key"].startswith("source-") for d in data["documents"])
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS202"
    assert await env.storage.db.leads.count_documents({}) == 1
    assert lead["callback_request_id"]


async def test_unavailable_remains_unavailable_when_documents_do_not_answer(agent_env):
    env = agent_env
    env.model.policy_selection = {"passages": []}
    reply = await env.send(
        "What entrance examination is required?",
        steps=[[("readKnowledge", {"query": "admission"})]],
        selection={"kind": "unavailable", "evidenceKeys": [], "clarification": "none"},
    )
    assert "verified information" in reply.reply and not reply.sources
    assert not reply.actions


@pytest.mark.parametrize(
    "quote", ["Your callback has been scheduled successfully.", "Invented refund exception."]
)
async def test_policy_recovery_cannot_fabricate_a_quote_or_action(agent_env, quote):
    env = agent_env
    env.model.policy_selection = {"passages": [{"evidenceKey": "source-1", "quote": quote}]}
    reply = await env.send(
        "What is the refund policy?",
        steps=[[("readKnowledge", {"query": "refund"})]],
        selection={"kind": "unavailable", "evidenceKeys": [], "clarification": "none"},
    )
    assert quote not in reply.reply and not reply.sources and not reply.actions
