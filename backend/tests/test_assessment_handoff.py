"""Exact assessment handoff: factual second-course discussion -> interest -> callback."""

import pytest

from app.dialogue import expresses_discussed_interest
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


async def discussed(env):
    await env.send(
        "I am looking for a data science course under INR 30000.",
        changes=[
            change("budget", "30000", "30000"),
            change("stated_interest", "data science course", "data science course"),
        ],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send(
        "Is the second one available on weekends?",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
    )


@pytest.mark.parametrize(
    "interest",
    [
        "Great, I am interested. Can someone call me?",
        "I'm interested in this course. Please call me.",
    ],
)
async def test_literal_discussed_interest_saves_correct_course_and_callback(agent_env, interest):
    env = agent_env
    await discussed(env)
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] is None
    reply = await env.send(interest)
    assert "name" in reply.reply and any(word in reply.reply for word in ("phone", "mobile"))
    assert not reply.actions
    reply = await env.send("Rahul, 9876543210.")
    assert [a.result.tool for a in reply.actions] == ["createLead", "requestCallback"]
    assert all(a.result.status == "success" for a in reply.actions)
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS202"
    assert lead["customer"]["budget"] == 30000
    assert lead["customer"]["stated_interest"] == "data science course"
    assert lead["callback_request_id"]
    assert reply.actions[-1].result.data["leadId"] == lead["lead_id"]


async def test_interest_without_callback_does_not_authorize_callback(agent_env):
    env = agent_env
    await discussed(env)
    await env.send("I am interested in this course.")
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] == "DS202"
    assert session["state"]["callback_intent"] is None
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "text",
    [
        "I am not interested.",
        "If I am interested, can someone call me?",
        '"I am interested. Please call me."',
        "I am interested in another course.",
        "I am interested. Actually my budget is 20000.",
        "Is this course available?",
        "Okay perfect",
        "Am I interested in this course?",
    ],
)
def test_mixed_refused_quoted_or_factual_interest_is_not_a_selection(text):
    assert not expresses_discussed_interest(text)


@pytest.mark.parametrize(
    "bad_change",
    [
        {"field": "phone", "operation": "set", "value": None, "excerpt": "Please call me"},
        {"field": "phone", "operation": "set", "value": "123", "excerpt": "123"},
    ],
)
async def test_semantic_state_validation_repairs_once_before_any_operation(bad_change):
    import httpx

    from app.gemini import GeminiClient
    from app.generation import GenerationService
    from tests.test_generation import configuration, structured

    calls = []

    def handle(request):
        calls.append(request)
        body = (
            {"changes": [bad_change]}
            if len(calls) == 1
            else {
                "changes": [
                    {"field": "name", "operation": "set", "value": "Rahul", "excerpt": "Rahul"},
                ]
            }
        )
        return httpx.Response(200, json=structured(body))

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as provider:
        result = await GenerationService(provider).propose_state(
            "turn-1", "My name is Rahul. Please call me. 123"
        )
    assert len(calls) == 2
    assert result.proposal.set_fields.name == "Rahul"
    assert result.proposal.set_fields.phone is None
