"""Checked reference identity survives a missing renderer hint; unsupported facts stay absent."""

import pytest

from app.generation import CourseQuestion
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def options(env):
    await env.send(
        "Find data science courses under 30000.",
        changes=[
            change("budget", "30000", "30000"),
            change("stated_interest", "data science", "data science"),
        ],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )


@pytest.mark.parametrize("supported", [True, False])
async def test_reference_focus_is_not_lost_when_broad_reply_selection_misses_fields(
    agent_env, monkeypatch, supported
):
    env = agent_env
    await options(env)

    async def question(text, course_id):
        assert course_id == "DS202"
        return (
            CourseQuestion(course_id=course_id, excerpt=text, fields=("schedule",))
            if supported
            else None
        )

    monkeypatch.setattr(env.engine.generation, "select_course_question", question)
    text = (
        "Would the second option fit a weekend-only schedule?"
        if supported
        else "Does the second option promise a job?"
    )
    result = await env.send(
        text,
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
        selection={
            "kind": "unavailable",
            "courseUse": "none",
            "evidenceKeys": [],
            "clarification": "none",
        },
    )
    assert ("weekends" in result.reply) is supported
    assert "28000" not in result.reply
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["agent_trace"]["discussed_course_id"] == "DS202"
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] is None
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "kind,course_use,fields",
    [
        ("evidence", "none", []),
        ("unavailable", "none", []),
        ("evidence", "fields", ["fee"]),
    ],
)
async def test_schedule_confirmation_keeps_checked_result_when_model_hints_are_missing(
    agent_env, monkeypatch, kind, course_use, fields
):
    env = agent_env
    await options(env)
    await env.send("first one", steps=[[("getCourseDetails", {"courseID": "DS101"})]])

    async def no_model_repair(*args, **kwargs):
        raise AssertionError("A literal schedule confirmation needs no further model call")

    monkeypatch.setattr(env.engine.generation, "select_course_question", no_model_repair)
    result = await env.send(
        "are you sure it runs on weekends",
        steps=[[("getCourseDetails", {"courseID": "DS101"})]],
        selection={
            "kind": kind,
            "courseUse": course_use,
            "courseFields": fields,
            "evidenceKeys": [],
            "clarification": "none",
        },
    )
    assert "weekdays" in result.reply and "25000" not in result.reply
    assert "don't have verified" not in result.reply
    assert [a.result.tool for a in result.actions] == ["getCourseDetails"]
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] == "DS101"
    assert session["state"]["fields"]["schedule"] is None
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "text,tool_course",
    [
        ("are you sure it runs on weekends", None),
        ("are you sure it runs on weekends", "DS202"),
        ("are you sure it runs on weekends and guarantees a job", "DS101"),
        ("are you sure it runs on weekends and what is the refund policy", "DS101"),
    ],
)
async def test_schedule_fallback_needs_matching_fresh_receipt_and_pure_question(
    agent_env, text, tool_course
):
    env = agent_env
    await options(env)
    await env.send("first one", steps=[[("getCourseDetails", {"courseID": "DS101"})]])
    result = await env.send(
        text,
        steps=[[("getCourseDetails", {"courseID": tool_course})]] if tool_course else [],
        selection={
            "kind": "evidence",
            "courseUse": "none",
            "evidenceKeys": [],
            "clarification": "none",
        },
    )
    assert "don't have verified" in result.reply
    assert "weekdays" not in result.reply and "weekends" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0
