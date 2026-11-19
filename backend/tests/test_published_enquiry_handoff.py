"""Published live failures: clear enquiry handoffs must survive provider outages."""

import pytest

from app.dialogue import starts_discussed_enquiry
from tests.test_agent import agent_env as agent_env
from tests.test_direct_enquiry import select_first


@pytest.mark.parametrize(
    "text",
    [
        "i want to enquire about it",
        "okay then i want to enquiry about it",
        "i want toenquire about it",
        "I'd like to make an enquiry about this course.",
        "Can I enquire about it?",
        "Please register my interest in this course.",
    ],
)
async def test_published_handoff_and_fragmented_contacts_need_no_provider(agent_env, text):
    env = agent_env
    await select_first(env)
    before = len(env.model.requests)
    env.model.fail_state = True
    response = await env.send(text)
    assert "name" in response.reply and "phone" in response.reply
    assert not response.actions and not response.sources
    response = await env.send("My name is Evaluation Learner")
    assert "phone" in response.reply and "share your name" not in response.reply
    response = await env.send("9876543210")
    assert "Your enquiry is saved" in response.reply
    assert [a.result.tool for a in response.actions] == ["createLead"]
    assert len(env.model.requests) == before
    lead = await env.storage.db.leads.find_one({"session_id": response.session_id})
    assert lead["customer"]["course_id"] == "DS101"
    assert lead["customer"]["name"] == "Evaluation Learner"
    assert lead.get("callback_request_id") is None
    assert await env.storage.db.leads.count_documents({}) == 1


@pytest.mark.parametrize(
    "text",
    [
        "I do not want to enquire about it",
        "If I want to enquire about it, what happens?",
        'Say "I want to enquire about it"',
        "I want to enquire about another course",
        "I want to enquire about Python Basics",
        "I want to enquire about it and request a callback",
        "I want to enquire about it, but my budget is 20k",
        "I want to enquire about it. My name is Rahul",
        "I want to enquire about it after checking its refund policy",
        "Please register my interest in this course and call me",
    ],
)
def test_literal_handoff_excludes_mixed_quoted_conditional_and_foreign_choices(text):
    assert not starts_discussed_enquiry(text)


async def test_unresolved_enquiry_asks_for_course_without_provider_or_write(agent_env):
    env = agent_env
    env.model.fail_state = True
    response = await env.send("I want to enquire about it")
    assert "Which course" in response.reply
    assert not response.actions and not env.model.requests
    assert await env.storage.db.leads.count_documents({}) == 0
