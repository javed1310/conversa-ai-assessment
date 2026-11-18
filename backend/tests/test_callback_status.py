"""Callback status is a read; only acceptance of an owned offer authorizes a write."""

from dataclasses import replace

import pytest

from app.tools import ToolRegistry
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def saved_lead(env):
    result = await env.send(
        "My name is Audit Customer. My phone is 9876543210. "
        "I want data science. Please save my enquiry.",
        changes=[
            change("name", "Audit Customer", "Audit Customer"),
            change("phone", "9876543210", "9876543210"),
            change("stated_interest", "data science", "data science"),
        ],
        steps=[
            [
                (
                    "createLead",
                    {
                        "name": "Audit Customer",
                        "phone": "9876543210",
                        "statedInterest": "data science",
                    },
                )
            ]
        ],
    )
    return next(a.result.data["leadId"] for a in result.actions if a.result.status == "success")


@pytest.mark.parametrize(
    "text",
    [
        "okay so will i get a call after this or what",
        "Will someone call me?",
        "Has my callback been requested?",
    ],
)
async def test_status_question_never_requests_callback(agent_env, text):
    env = agent_env
    lead = await saved_lead(env)
    before = len(env.model.requests)
    reply = await env.send(text, steps=[[("requestCallback", {"leadID": lead})]])
    assert "saved" in reply.reply and "would you like" in reply.reply.lower()
    assert not reply.actions and len(env.model.requests) == before
    record = await env.storage.db.leads.find_one({"lead_id": lead})
    assert record["callback_request_id"] is None


async def test_yes_to_owned_callback_offer_saves_once(agent_env):
    env = agent_env
    lead = await saved_lead(env)
    await env.send("Will I get a call?")
    result = await env.send("yes")
    assert [a.result.tool for a in result.actions] == ["requestCallback"]
    assert result.actions[0].result.status == "success"
    assert result.actions[0].result.data["leadId"] == lead
    assert "requested" in result.reply
    first = await env.storage.db.leads.find_one({"lead_id": lead})
    await env.send("yes")
    later = await env.storage.db.leads.find_one({"lead_id": lead})
    assert first["callback_request_id"] == later["callback_request_id"]


async def test_decline_does_not_request_callback(agent_env):
    env = agent_env
    lead = await saved_lead(env)
    await env.send("Will I get a call?")
    result = await env.send("no thanks")
    assert not result.actions
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))["callback_request_id"] is None


@pytest.mark.parametrize("answer", ['"yes"', "yes"])
async def test_quote_or_stale_offer_cannot_authorize_callback(agent_env, answer):
    env = agent_env
    lead = await saved_lead(env)
    await env.send("Will I get a call?")
    if answer == "yes":
        await env.send("Hello")
    result = await env.send(answer, steps=[[("requestCallback", {"leadID": lead})]])
    assert not any(a.result.status == "success" for a in result.actions)
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))["callback_request_id"] is None


async def test_missing_enquiry_is_not_a_callback_offer(agent_env):
    env = agent_env
    result = await env.send("Will I get a call?")
    assert "not" in result.reply.lower() and not result.actions
    await env.send("yes")
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_recorded_callback_status_does_not_claim_scheduled_call(agent_env):
    env = agent_env
    await saved_lead(env)
    await env.send("Will I get a call?")
    await env.send("yes")
    result = await env.send("Has my callback been requested?")
    assert "recorded" in result.reply and "not" in result.reply.lower()
    assert not result.actions


async def test_mixed_status_question_does_not_supply_callback_consent(agent_env):
    env = agent_env
    lead = await saved_lead(env)
    result = await env.send(
        "Will someone call me and what is your refund policy?",
        steps=[[("requestCallback", {"leadID": lead})]],
    )
    assert not any(a.result.status == "success" for a in result.actions)
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))["callback_request_id"] is None


async def test_decline_after_failed_callback_clears_old_consent(agent_env):
    env = agent_env
    lead = await saved_lead(env)

    async def failed(*_):
        raise RuntimeError("controlled callback failure")

    registry = ToolRegistry(
        tuple(
            replace(item, handler=failed) if item.name == "requestCallback" else item
            for item in env.engine.tools._definitions.values()
        )
    )
    env.engine.tools = registry
    env.engine.executor.registry = registry
    await env.send("Please call me.")
    await env.send("Will I get a call?")
    await env.send("no thanks")
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["callback_intent"] is None
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))["callback_request_id"] is None
