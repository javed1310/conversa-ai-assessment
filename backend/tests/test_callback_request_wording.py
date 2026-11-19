"""Explicit callback wording shares the authorization policy and saved-lead route."""

import pytest

from app.intent import callback_intent
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change
from tests.test_callback_status import saved_lead


@pytest.mark.parametrize(
    "text",
    [
        "i want to request a call back",
        "I want to request a callback",
        "I'd like to request a call-back, please.",
        "I would like to arrange a counsellor call back",
        "I need to book a callback",
        "Please request a call back",
        "Please call me back",
        "Call back",
        "Please call back",
        "Okay, call back",
    ],
)
def test_explicit_callback_wording_is_consent(text):
    assert callback_intent(text) == "request"


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to request a call back",
        "Do not request a call back",
        "I no longer want to arrange a callback",
        "Cancel my call back",
        "No call backs",
        "Don't call back",
        "Never call back",
    ],
)
def test_callback_wording_keeps_revocation(text):
    assert callback_intent(text) == "revoke"


@pytest.mark.parametrize(
    "text",
    [
        '"I want to request a call back"',
        "Example: I want to request a call back",
        "If I want to request a call back, what happens?",
        "Maybe I want to request a callback",
        "Will I get a call back?",
        "Has my call back been requested?",
        "When can I expect someone to contact me?",
        "Will someone contact me?",
        "What does request a call back mean?",
    ],
)
def test_quote_conditional_and_status_are_not_permission(text):
    assert callback_intent(text) == "none"


@pytest.mark.parametrize("text", ["i want to request a call back", "I want to request a callback"])
async def test_request_uses_existing_lead_without_provider_or_duplicate(agent_env, text):
    env = agent_env
    lead_id = await saved_lead(env)
    requests = len(env.model.requests)
    env.model.fail_state = True
    result = await env.send(text)
    assert len(env.model.requests) == requests
    assert [a.result.tool for a in result.actions] == ["requestCallback"]
    assert result.actions[0].result.status == "success"
    assert result.actions[0].result.data["leadId"] == lead_id
    before = await env.storage.db.leads.find_one({"lead_id": lead_id})
    assert before["callback_request_id"]
    repeated = await env.send(text)
    after = await env.storage.db.leads.find_one({"lead_id": lead_id})
    assert not repeated.actions
    assert after["callback_request_id"] == before["callback_request_id"]
    assert await env.storage.db.leads.count_documents({}) == 1


async def test_mixed_callback_request_does_not_discard_phone_correction(agent_env):
    env = agent_env
    await saved_lead(env)
    before = len(env.model.requests)
    result = await env.send(
        "My phone is 9876543211. Please call me.",
        changes=[change("phone", "9876543211", "9876543211")],
    )
    record = await env.storage.db.sessions.find_one({"session_id": result.session_id})
    assert len(env.model.requests) > before
    assert record["state"]["fields"]["phone"] == "+919876543211"
