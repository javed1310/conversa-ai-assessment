"""Owned list selection must not depend on model planning or reply metadata."""

from dataclasses import replace

import pytest

from app.repositories import Repository
from app.tools import ToolRegistry
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def search(env):
    await env.send(
        "I want data science courses with a maximum budget of 30000 INR.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )


@pytest.mark.parametrize(
    ("text", "course"),
    [
        ("first one", "DS101"),
        ("second one", "DS202"),
        ("the 1st course", "DS101"),
        ("2nd option!", "DS202"),
    ],
)
async def test_bare_selection_survives_unavailable_model_selection(agent_env, text, course):
    env = agent_env
    await search(env)
    before = len(env.model.requests)
    result = await env.send(
        text,
        steps=[
            [("resolveCourseReference", {"excerpt": text})],
            [("getCourseDetails", {"courseID": course})],
        ],
        selection={"kind": "unavailable", "evidenceKeys": [], "clarification": "none"},
    )
    assert (
        "Data Science Foundations" if course == "DS101" else "Applied Data Analytics"
    ) in result.reply
    assert ("25000" if course == "DS101" else "28000") in result.reply
    assert len(env.model.requests) == before
    assert [a.result.tool for a in result.actions] == ["getCourseDetails"]
    state = (
        await Repository(env.storage, env.registry.get("training-institute")).session(env.context)
    ).state
    assert state.fields.course_id == course
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize("with_list", [False, True])
async def test_missing_ordinal_clarifies_without_guessing(agent_env, with_list):
    env = agent_env
    if with_list:
        await search(env)
    result = await env.send("third one" if with_list else "first one")
    assert not result.actions
    assert "which" in result.reply.lower() or "list" in result.reply.lower()
    assert "25000" not in result.reply


async def test_selection_reads_current_record_not_previous_search_snapshot(agent_env):
    env = agent_env
    await search(env)
    await env.storage.db.courses.update_one(
        {"business_id": "training-institute", "course_id": "DS101"},
        {"$set": {"fee": 27000, "availability": "full"}},
    )
    result = await env.send("first one")
    assert "27000" in result.reply and "full" in result.reply
    assert "25000" not in result.reply


async def test_failed_selected_course_read_never_reports_old_facts(agent_env):
    env = agent_env
    await search(env)

    async def failed(*_):
        raise RuntimeError("controlled read failure")

    registry = ToolRegistry(
        tuple(
            replace(item, handler=failed) if item.name == "getCourseDetails" else item
            for item in env.engine.tools._definitions.values()
        )
    )
    env.engine.tools = registry
    env.engine.executor.registry = registry
    result = await env.send("first one")
    assert result.actions and all(a.result.status == "failure" for a in result.actions)
    assert "25000" not in result.reply and "28000" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0
