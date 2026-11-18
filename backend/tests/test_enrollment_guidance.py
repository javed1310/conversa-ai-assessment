"""Enrollment process questions offer a supported next step without authorizing writes."""

import pytest

from app.dialogue import asks_enrollment_next_step
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


@pytest.mark.parametrize(
    "text",
    [
        "if i want to enroll what should be the next step",
        "If I would like to join this course, what should be my next step?",
        "What is the next step to enroll?",
        "What are the next steps for enrollment?",
    ],
)
async def test_guidance_remembers_course_and_asks_before_collecting(agent_env, text):
    env = agent_env
    await env.send(
        "data science under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("second one", steps=[[("getCourseDetails", {"courseID": "DS202"})]])
    before = len(env.model.requests)
    result = await env.send(text)
    assert "Applied Data Analytics" in result.reply
    assert "Would you like to make an enquiry" in result.reply
    assert "doesn't enroll you or reserve a seat" in result.reply
    assert len(env.model.requests) == before
    assert not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["agent_trace"]["next_step"] == "offer_enquiry"
    accepted = await env.send("yes")
    assert "name" in accepted.reply and "phone" in accepted.reply
    saved = await env.send("Rahul 9876543210")
    assert "Your enquiry is saved" in saved.reply
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS202"
    assert lead.get("callback_request_id") is None


async def test_guidance_without_selected_course_asks_which_course(agent_env):
    result = await agent_env.send("if i want to enroll what should be the next step")
    assert "Which course" in result.reply
    assert not result.actions
    assert await agent_env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to enroll what should be the next step",
        "if i want to enroll what should be the next step and is a refund guaranteed",
        "if i want to enroll what should be the next step in Python Basics",
        "if i want to enroll what should be the next step my phone is 9876543210",
        'Say "if i want to enroll what should be the next step"',
        "if i want to enroll what should be the next step and call me",
    ],
)
def test_mixed_quoted_or_different_course_questions_keep_normal_routing(text):
    assert not asks_enrollment_next_step(text)


async def test_process_question_does_not_save_lead_when_contacts_are_already_known(agent_env):
    env = agent_env
    await env.send(
        "I choose DS202. My name is Rahul and phone is 9876543210.",
        changes=[
            change("course_id", "DS202", "DS202"),
            change("name", "Rahul", "Rahul"),
            change("phone", "9876543210", "9876543210"),
        ],
    )
    result = await env.send("if i want to enroll what should be the next step")
    assert "Would you like to make an enquiry" in result.reply
    assert not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "text",
    [
        "okay perfect , then if i want to enroll what should be process",
        "Great, how would I go about enrolling?",
        "Could you explain the joining procedure for me?",
        "What happens next if I decide to join?",
    ],
)
async def test_semantic_process_intent_handles_natural_followups(agent_env, monkeypatch, text):
    from app.generation import EnrollmentProcessSelection

    env = agent_env
    await env.send("I choose DS202", changes=[change("course_id", "DS202", "DS202")])
    original = env.engine.generation._structured

    async def structured(model, system, data, **kwargs):
        if model is EnrollmentProcessSelection:
            assert data["customerText"] == text
            return EnrollmentProcessSelection(process_question=True, excerpt=text)
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", structured)
    result = await env.send(text)
    assert "Applied Data Analytics" in result.reply
    assert "Would you like to make an enquiry" in result.reply
    assert "assessment" not in result.reply.lower()
    assert "prototype" not in result.reply.lower()
    assert not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0
    accepted = await env.send("yes")
    assert "name" in accepted.reply and "phone" in accepted.reply


@pytest.mark.parametrize("accepted,excerpt", [(False, None), (True, "different turn")])
async def test_semantic_process_cue_needs_positive_current_turn_verdict(
    agent_env, monkeypatch, accepted, excerpt
):
    from app.generation import EnrollmentProcessSelection

    env = agent_env
    text = "Could you explain the joining procedure?"
    original = env.engine.generation._structured

    async def structured(model, system, data, **kwargs):
        if model is EnrollmentProcessSelection:
            return EnrollmentProcessSelection(process_question=accepted, excerpt=excerpt or text)
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", structured)
    result = await env.send(text)
    assert "Which course would you like to take the next step" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0
