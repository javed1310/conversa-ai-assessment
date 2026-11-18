"""A late provider outage must not discard completed, verified catalog searches."""

import pytest

from app.gateway import GatewayFailure
from app.gemini import ProviderFailure
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


@pytest.mark.parametrize("stage", ["continue_with", "select_reply"])
@pytest.mark.parametrize("code", ["TIMEOUT", "UNAVAILABLE", "RATE_LIMIT"])
async def test_verified_search_skips_unnecessary_late_provider_calls(
    agent_env, monkeypatch, stage, code
):
    env = agent_env

    async def outage(*args, **kwargs):
        raise AssertionError("Completed simple search made an unnecessary provider call")

    target = env.engine.provider if stage == "continue_with" else env.engine.generation
    monkeypatch.setattr(target, stage, outage)
    result = await env.send(
        "I want data science courses with a maximum budget of 30000 INR.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    assert "DS101" in result.reply and "DS202" in result.reply
    assert "25000" in result.reply and "28000" in result.reply
    assert result.actions[0].result.status == "success"
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["status"] == "completed"
    assert turn["agent_trace"]["failure_codes"] == []
    payload = env.payloads[result.message_id]
    replay = await env.gateway.handle(env.context, payload)
    assert replay.payload == result
    assert await env.storage.db.tool_executions.count_documents({}) == 1
    monkeypatch.undo()
    second = await env.send("second one", steps=[[("getCourseDetails", {"courseID": "DS202"})]])
    assert "Applied Data Analytics" in second.reply and "weekends" in second.reply
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_outage_before_search_still_fails_without_inventing_results(agent_env, monkeypatch):
    async def outage(*args, **kwargs):
        raise ProviderFailure("TIMEOUT", True)

    monkeypatch.setattr(agent_env.engine.provider, "start_context", outage)
    with pytest.raises(GatewayFailure):
        await agent_env.send("Find data science courses under 30000")
    assert await agent_env.storage.db.tool_executions.count_documents({}) == 0


async def test_mixed_policy_question_does_not_claim_search_is_a_complete_answer(
    agent_env, monkeypatch
):
    async def outage(*args, **kwargs):
        raise ProviderFailure("TIMEOUT", True)

    monkeypatch.setattr(agent_env.engine.provider, "continue_with", outage)
    with pytest.raises(GatewayFailure):
        await agent_env.send(
            "Find data science courses under 30000 and explain the refund policy",
            changes=[change("budget", "30000", "30000")],
            steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
        )


async def test_successful_empty_search_is_still_a_verified_result(agent_env, monkeypatch):
    async def outage(*args, **kwargs):
        raise ProviderFailure("TIMEOUT", True)

    monkeypatch.setattr(agent_env.engine.provider, "continue_with", outage)
    result = await agent_env.send(
        "Find data science courses under 20000",
        changes=[change("budget", "20000", "20000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 20000})]],
    )
    assert "No courses match" in result.reply
    assert "DS101" not in result.reply and "DS202" not in result.reply
    assert result.actions[0].result.status == "success"
    assert result.actions[0].result.data["courses"] == []
