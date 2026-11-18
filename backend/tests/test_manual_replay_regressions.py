"""Exact historical dialogue failures and the safety boundaries around their fixes."""

import pytest

from app.generation import CourseQuestion, EnquiryRequest, ProposedChange, StateDraft
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def select(env):
    await env.send(
        "Find data science courses under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("second one")


@pytest.mark.parametrize("text", ["okay perfect", "okay, great", "thanks, understood"])
async def test_social_closing_before_enquiry_needs_no_model_or_tools(agent_env, text):
    env = agent_env
    await select(env)
    before = len(env.model.requests)
    result = await env.send(text, steps=[[("getCourseDetails", {"courseID": "DS202"})]])
    assert not result.actions and not result.sources
    assert "question" in result.reply.lower() or "next" in result.reply.lower()
    assert len(env.model.requests) == before


async def test_validated_preference_is_acknowledged_without_another_lookup(agent_env):
    env = agent_env
    await select(env)
    result = await env.send(
        "I prefer weekends.",
        changes=[change("schedule", "weekend", "weekends")],
        selection={
            "kind": "unavailable",
            "courseUse": "none",
            "evidenceKeys": [],
            "clarification": "none",
        },
    )
    assert "weekend" in result.reply.lower() and "enquire" in result.reply.lower()
    assert "28000" not in result.reply and not result.actions
    result = await env.send("yes")
    assert "name" in result.reply and "phone" in result.reply


async def test_selected_ordinal_preference_cannot_be_hidden_by_reply_plan(agent_env):
    env = agent_env
    await env.send(
        "Find data science courses under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    result = await env.send(
        "i would prefer the second one",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
        selection={
            "kind": "unavailable",
            "courseUse": "none",
            "evidenceKeys": [],
            "clarification": "none",
        },
    )
    assert "Applied Data Analytics" in result.reply and "enquire" in result.reply.lower()


async def test_selection_only_ack_uses_owned_reference_without_claiming_current_prices(agent_env):
    env = agent_env
    await env.send(
        "Find data science courses under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    result = await env.send(
        "i would prefer the second one",
        steps=[[("resolveCourseReference", {"excerpt": "the second one"})]],
    )
    record = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert record["state"]["fields"]["course_id"] == "DS202"
    assert "You've selected Applied Data Analytics" in result.reply
    assert "enquire" in result.reply and "28000" not in result.reply
    assert not result.actions and await env.storage.db.leads.count_documents({}) == 0


async def test_enquiry_cue_survives_redundant_unproven_course_noop(agent_env, monkeypatch):
    env = agent_env
    await select(env)
    original = env.engine.generation._structured

    async def draft(model, system, data, **kwargs):
        if model is StateDraft:
            return StateDraft(
                changes=(
                    ProposedChange(
                        field="course_id",
                        operation="set",
                        value="DS202",
                        excerpt="to enquire about it",
                    ),
                ),
                enquiry_request=EnquiryRequest(course_id="DS202", excerpt=data["customerText"]),
            )
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", draft)
    result = await env.send("i want toenquire about it")
    assert "name" in result.reply and "phone" in result.reply
    result = await env.send("Replay Learner 9876543210")
    assert "saved" in result.reply


async def test_mixed_preference_and_policy_question_is_not_swallowed(agent_env):
    env = agent_env
    await select(env)
    result = await env.send(
        "I prefer weekends, but what is your refund policy?",
        changes=[change("schedule", "weekend", "weekends")],
        steps=[[("readKnowledge", {"query": "refund"})]],
    )
    assert result.sources and "seven" in result.reply


async def test_multifield_reference_finishes_on_checked_read(agent_env, monkeypatch):
    env = agent_env
    await env.send(
        "Find data science courses under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )

    async def fields(text, course_id):
        return CourseQuestion(
            course_id=course_id, fields=("fee", "schedule", "availability"), excerpt=text
        )

    async def unnecessary(*args, **kwargs):
        raise AssertionError("Sufficient checked read continued the planner")

    monkeypatch.setattr(env.engine.generation, "select_course_question", fields)
    monkeypatch.setattr(env.engine.provider, "continue_with", unnecessary)
    result = await env.send(
        "I want the second one. What are its fee, schedule and availability?",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
    )
    assert "28000" in result.reply and "weekends" in result.reply and "available" in result.reply


async def test_why_after_saved_enquiry_asks_for_clarification(agent_env):
    from tests.test_callback_status import saved_lead

    await saved_lead(agent_env)
    before = len(agent_env.model.requests)
    result = await agent_env.send("why")
    assert "explain" in result.reply and "?" in result.reply
    assert not result.actions and len(agent_env.model.requests) == before


async def test_neutral_closing_keeps_owned_offer_without_accepting_it(agent_env):
    env = agent_env
    await select(env)
    await env.send("I prefer weekends.", changes=[change("schedule", "weekend", "weekends")])
    before = len(env.model.requests)
    response = await env.send("thanks, understood")
    assert not response.actions and len(env.model.requests) == before
    assert await env.storage.db.leads.count_documents({}) == 0
    response = await env.send("yes")
    assert "name" in response.reply and "phone" in response.reply


def test_policy_authoring_directives_are_not_customer_answers():
    from app.facts import policy_reply

    response = policy_reply(
        "Refund terms are not established here. If asked, say the information is unavailable.",
        "Is there a refund policy?",
    )
    assert "not established" in response and "If asked" not in response
    response = policy_reply(
        "If asked, explain that refunds require BOTH seven days AND no first class attendance.",
        "Refund policy?",
    )
    assert "BOTH seven days AND no first class attendance" in response
