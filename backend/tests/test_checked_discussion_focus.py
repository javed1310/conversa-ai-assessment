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
