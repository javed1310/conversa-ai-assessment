"""Registered membership reads complete without redundant provider calls."""

from uuid import uuid4

import pytest

from app.gateway import GatewayFailure
from app.gemini import ProviderFailure
from tests.test_agent import agent_env as agent_env


async def fitness_context(env):
    session = await env.gateway.start_web_session("fitness-studio")
    return await env.gateway.web_context(
        "fitness-studio", session.session_id, session.session_token, uuid4()
    )


@pytest.mark.parametrize(
    "text",
    [
        "What membership options are available?",
        "Show me membership plans",
        "How much do memberships cost?",
    ],
)
async def test_checked_memberships_skip_later_model_requests(agent_env, monkeypatch, text):
    env = agent_env
    context = await fitness_context(env)

    async def unexpected(*args, **kwargs):
        raise AssertionError("A complete membership read made a redundant provider call")

    monkeypatch.setattr(env.engine.provider, "continue_with", unexpected)
    monkeypatch.setattr(env.engine.generation, "select_reply", unexpected)
    before = len(env.model.requests)
    result = await env.send(text, target=context, steps=[[("getMembershipDetails", {})]])
    assert len(env.model.requests) == before + 1
    assert "INR" in result.reply and "months" in result.reply
    assert "assessment" not in result.reply.lower()
    assert "knowledge base" not in result.reply.lower()
    assert "integration" not in result.reply.lower()
    assert "for 1 month." in result.reply
    assert [a.result.tool for a in result.actions] == ["getMembershipDetails"]
    assert result.actions[0].result.status == "success"
    original = env.payloads[result.message_id]
    replay = await env.gateway.handle(context, original)
    assert replay.payload == result
    assert len(env.model.requests) == before + 1
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_empty_membership_catalog_is_a_valid_checked_result(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.storage.db.memberships.delete_many({"business_id": "fitness-studio"})
    result = await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    assert result.reply == "No matching membership was found."
    assert result.actions[0].result.status == "success"


async def test_rate_limit_before_membership_lookup_still_reports_failure(agent_env, monkeypatch):
    context = await fitness_context(agent_env)

    async def limited(*args, **kwargs):
        raise ProviderFailure("RATE_LIMIT", True)

    monkeypatch.setattr(agent_env.engine.provider, "start_context", limited)
    with pytest.raises(GatewayFailure):
        await agent_env.send("What membership options are available?", target=context)
    assert await agent_env.storage.db.tool_executions.count_documents({}) == 0


async def test_memberships_plus_policy_still_use_orchestration(agent_env, monkeypatch):
    context = await fitness_context(agent_env)

    async def limited(*args, **kwargs):
        raise ProviderFailure("RATE_LIMIT", True)

    monkeypatch.setattr(agent_env.engine.provider, "continue_with", limited)
    with pytest.raises(GatewayFailure):
        await agent_env.send(
            "What membership options are available and what is the refund policy?",
            target=context,
            steps=[[("getMembershipDetails", {})]],
        )


async def test_irrelevant_document_read_does_not_hide_complete_membership_receipt(
    agent_env, monkeypatch
):
    env = agent_env
    context = await fitness_context(env)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Verified membership listing did not need further model calls")

    monkeypatch.setattr(env.engine.provider, "continue_with", unexpected)
    result = await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("readKnowledge", {"query": "trial"}), ("getMembershipDetails", {})]],
    )
    assert "INR" in result.reply
    assert not result.sources
    assert [a.result.tool for a in result.actions] == ["getMembershipDetails"]
