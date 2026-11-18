"""Business-independent orchestration must never execute training shortcuts for other domains."""

import ast
from pathlib import Path

from app.businesses import BusinessConfig, BusinessRegistry
from app.membership_behavior import MembershipBehavior
from app.repositories import Repository
from app.tools import ToolRegistry
from app.training_behavior import TrainingBehavior
from tests.test_agent import agent_env as agent_env
from tests.test_tool_extensions import definition


def test_shared_agent_contains_no_course_or_membership_dispatch():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/agent.py").read_text())
    names = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not names.intersection(
        {
            "searchCourses",
            "getCourseDetails",
            "getMembershipDetails",
            "resolveCourseReference",
            "training-institute",
            "fitness-studio",
        }
    )


async def test_fitness_uses_membership_behavior_without_course_reads(agent_env, monkeypatch):
    env = agent_env

    async def unexpected(*args, **kwargs):
        raise AssertionError("A fitness message attempted a course query")

    monkeypatch.setattr(Repository, "courses", unexpected)
    session = await env.gateway.start_web_session("fitness-studio")
    from uuid import uuid4

    context = await env.gateway.web_context(
        session.business_id, session.session_id, session.session_token, uuid4()
    )
    result = await env.send(
        "What membership choices do you have?",
        steps=[[("getMembershipDetails", {})]],
        target=context,
    )
    assert [a.result.tool for a in result.actions] == ["getMembershipDetails"]
    assert "INR" in result.reply
    assert (
        type(env.engine.behaviors.for_business(env.registry.get("fitness-studio"), env.engine))
        is MembershipBehavior
    )
    assert isinstance(
        env.engine.behaviors.for_business(env.registry.get("training-institute"), env.engine),
        TrainingBehavior,
    )
    assert all("resolveCourseReference" not in str(r.get("tools", [])) for r in env.model.requests)


async def test_third_business_needs_no_training_records_or_engine_edits(agent_env, monkeypatch):
    env = agent_env
    tool = definition()
    registry = ToolRegistry(
        tuple(env.engine.tools.get(name) for name in env.engine.tools.names) + (tool,)
    )
    hotel = BusinessConfig(
        business_id="fresh-hotel",
        business_name="Fresh Hotel",
        agent_role="Room information assistant",
        instructions=("Use the room tool for current facts.",),
        knowledge_base_id="fresh-hotel-kb",
        enabled_tools=(tool.name,),
        config_version=1,
    )
    businesses = BusinessRegistry((*env.registry.configurations(), hotel))
    env.engine.businesses = env.gateway.businesses = env.engine.executor.businesses = businesses
    env.engine.tools = env.engine.executor.registry = registry
    await env.storage.db.audit_rooms.insert_one(
        {"business_id": hotel.business_id, "room_id": "A01", "available": True, "nightly_fee": 1800}
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("A hotel message attempted to access a course catalog")

    monkeypatch.setattr(Repository, "courses", unexpected)
    session = await env.gateway.start_web_session(hotel.business_id)
    from uuid import uuid4

    context = await env.gateway.web_context(
        session.business_id, session.session_id, session.session_token, uuid4()
    )
    result = await env.send(
        "How much does room A01 cost?", steps=[[(tool.name, {"roomId": "A01"})]], target=context
    )
    assert "1800" in result.reply and "available" in result.reply
    assert "course" not in result.reply.lower()
    assert [a.result.tool for a in result.actions] == [tool.name]


def test_starter_questions_are_business_configuration():
    from app.settings import REPOSITORY_ROOT

    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    by_id = {b.business_id: b for b in registry.summaries().businesses}
    assert by_id["training-institute"].starter_questions[0].title == "Find the right course"
    assert by_id["fitness-studio"].starter_questions[0].title == "Explore memberships"
