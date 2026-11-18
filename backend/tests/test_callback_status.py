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
        "will get a callback",
        "now what should i do",
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


@pytest.mark.parametrize("text", ["okay", "OK!", "thanks", "okay, thanks", "got it", "yes"])
async def test_acknowledgement_after_callback_does_not_replay_write(agent_env, text):
    env = agent_env
    lead = await saved_lead(env)
    await env.send("Will I get a call?")
    await env.send("yes")
    before = await env.storage.db.leads.find_one({"lead_id": lead})
    requests = len(env.model.requests)
    result = await env.send(text, steps=[[("requestCallback", {"leadID": lead})]])
    assert not result.actions and not result.sources
    assert "question" in result.reply.lower()
    assert "reference" not in result.reply.lower()
    assert len(env.model.requests) == requests
    after = await env.storage.db.leads.find_one({"lead_id": lead})
    assert before == after


async def test_callback_acknowledgement_does_not_swallow_status_question(agent_env):
    env = agent_env
    await saved_lead(env)
    await env.send("Will I get a call?")
    await env.send("yes")
    result = await env.send("okay, will I get a callback?")
    assert "recorded" in result.reply
    assert not result.actions


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


async def test_next_steps_then_fragmented_callback_question_only_writes_after_yes(agent_env):
    env = agent_env
    lead = await saved_lead(env)
    for text in ["now what should i do", "will get a callback"]:
        before = len(env.model.requests)
        result = await env.send(text)
        assert "callback hasn't been requested" in result.reply
        assert "Would you like" in result.reply
        assert not result.actions and len(env.model.requests) == before
        assert await env.storage.db.leads.count_documents({}) == 1
        assert (await env.storage.db.leads.find_one({"lead_id": lead}))[
            "callback_request_id"
        ] is None
    result = await env.send("yes")
    assert [a.result.tool for a in result.actions] == ["requestCallback"]
    assert result.actions[0].result.status == "success"
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))[
        "callback_request_id"
    ] is not None
    first = await env.storage.db.leads.find_one({"lead_id": lead})
    status = await env.send("will get a callback")
    assert "recorded" in status.reply and "not" in status.reply
    later = await env.storage.db.leads.find_one({"lead_id": lead})
    assert first["callback_request_id"] == later["callback_request_id"]
    assert await env.storage.db.leads.count_documents({}) == 1


@pytest.mark.parametrize(
    "text,intent",
    [
        ("Alright, where do we go from here? What happens now?", "next_steps"),
        ("When can I expect someone to contact me?", "callback_status"),
    ],
)
async def test_semantic_post_enquiry_questions_only_offer_callback(
    agent_env, monkeypatch, text, intent
):
    from app.generation import PostEnquirySelection

    env = agent_env
    lead = await saved_lead(env)
    original = env.engine.generation._structured

    async def structured(model, system, data, **kwargs):
        if model is PostEnquirySelection:
            return PostEnquirySelection(intent=intent, excerpt=text)
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", structured)
    result = await env.send(text)
    assert "Would you like me to request" in result.reply
    assert not result.actions
    assert (await env.storage.db.leads.find_one({"lead_id": lead}))["callback_request_id"] is None
