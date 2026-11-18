"""Direct enquiry intent can start collection without a previous assistant offer."""

import pytest

from app.generation import EnquiryRequest, StateDraft
from app.intent import callback_request
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def select_first(env):
    await env.send(
        "data science under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("first one", steps=[[("getCourseDetails", {"courseID": "DS101"})]])


def classify(env, monkeypatch, *, course_id="DS101", excerpt=None):
    original = env.engine.generation._structured

    async def structured(model, system, data, **kwargs):
        result = await original(model, system, data, **kwargs)
        if model is StateDraft:
            return result.model_copy(
                update={
                    "enquiry_request": EnquiryRequest(
                        course_id=course_id, excerpt=excerpt or data["customerText"]
                    )
                }
            )
        return result

    monkeypatch.setattr(env.engine.generation, "_structured", structured)


@pytest.mark.parametrize(
    "text",
    [
        "okay then i want to enquiry about it",
        "I'd like to make an enquiry about this course.",
        "Can I enquire about it?",
        "Please register my interest in this course.",
    ],
)
async def test_direct_request_collects_then_saves_without_offer(agent_env, monkeypatch, text):
    env = agent_env
    await select_first(env)
    classify(env, monkeypatch)
    before = len(env.model.requests)
    result = await env.send(text)
    assert len(env.model.requests) == before + 1
    assert "name" in result.reply and "mobile number" in result.reply
    assert "25000" not in result.reply and "weekdays" not in result.reply
    assert not result.actions
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["agent_trace"]["next_step"] == "collect_enquiry"
    assert turn["agent_trace"]["dialogue_course_id"] == "DS101"
    assert await env.storage.db.leads.count_documents({}) == 0
    monkeypatch.undo()
    named = await env.send("My name is Rahul.", changes=[change("name", "Rahul", "Rahul")])
    assert "mobile number" in named.reply and "share your name" not in named.reply
    saved = await env.send(
        "My phone is 9876543210.", changes=[change("phone", "9876543210", "9876543210")]
    )
    assert "Your enquiry is saved" in saved.reply
    assert await env.storage.db.leads.count_documents({}) == 1
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS101"
    assert lead.get("callback_request_id") is None


async def test_direct_request_asks_only_for_missing_phone(agent_env, monkeypatch):
    env = agent_env
    await select_first(env)
    classify(env, monkeypatch)
    result = await env.send(
        "My name is Rahul. I want to enquire about it.",
        changes=[change("name", "Rahul", "Rahul")],
    )
    assert "mobile number" in result.reply and "share your name" not in result.reply
    assert not result.actions


@pytest.mark.parametrize(
    "text",
    [
        "I do not want to enquire about it",
        "Don't save my enquiry",
        "I never requested an enquiry",
        "What is its refund policy before I enquire?",
        "I want to enquire about Python Basics",
        "I want to enquire and request a callback",
    ],
)
async def test_wrong_model_cue_does_not_skip_refusal_or_mixed_intent(agent_env, monkeypatch, text):
    env = agent_env
    await select_first(env)
    classify(env, monkeypatch)
    before = len(env.model.requests)
    result = await env.send(text)
    if callback_request(text):
        assert len(env.model.requests) == before + 1
        assert "phone" in result.reply and "name" in result.reply and not result.actions
    else:
        assert len(env.model.requests) > before + 1
    assert "To save your enquiry" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize("course_id,excerpt", [("DS202", None), ("DS101", "another turn")])
async def test_unbound_request_cue_is_not_authority(agent_env, monkeypatch, course_id, excerpt):
    env = agent_env
    await select_first(env)
    classify(env, monkeypatch, course_id=course_id, excerpt=excerpt)
    result = await env.send("I want to enquire about it")
    assert "To save your enquiry" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_request_without_selected_course_keeps_normal_planning(agent_env, monkeypatch):
    env = agent_env
    classify(env, monkeypatch)
    result = await env.send("I want to enquire about it")
    assert "To save your enquiry" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_direct_request_with_qualified_contacts_saves_only_owned_course(
    agent_env, monkeypatch
):
    env = agent_env
    await select_first(env)
    classify(env, monkeypatch)
    result = await env.send(
        "I want to enquire about it. My name is Rahul, phone 9876543210.",
        changes=[change("name", "Rahul", "Rahul"), change("phone", "9876543210", "9876543210")],
    )
    assert "Your enquiry is saved" in result.reply
    assert [a.result.tool for a in result.actions] == ["createLead"]
    assert await env.storage.db.leads.count_documents({}) == 1
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS101"
    assert lead.get("callback_request_id") is None
