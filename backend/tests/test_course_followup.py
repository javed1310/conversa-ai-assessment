import pytest

from app.generation import CourseQuestion, StateDraft
from app.presentation import course_reply
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change
from tests.test_presentation import course, state


async def selected(env):
    await env.send(
        "data science under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    await env.send("second one", steps=[[("getCourseDetails", {"courseID": "DS202"})]])


def classify(env, monkeypatch, text, fields=("schedule",), course_id="DS202"):
    original = env.engine.generation._structured

    async def structured(model, *args, **kwargs):
        result = await original(model, *args, **kwargs)
        if model is StateDraft:
            return result.model_copy(
                update={
                    "course_question": CourseQuestion(
                        course_id=course_id, fields=fields, confirmation=True, excerpt=text
                    )
                }
            )
        return result

    monkeypatch.setattr(env.engine.generation, "_structured", structured)


@pytest.mark.parametrize(
    "text",
    [
        "are you sure it runs on weekends",
        "Can you confirm its schedule?",
        "Does it still have weekend classes?",
        "Are you certain it isn't a weekday course?",
    ],
)
@pytest.mark.asyncio
async def test_selected_schedule_verification_uses_one_model_call_and_fresh_tool(
    agent_env, monkeypatch, text
):
    env = agent_env
    await selected(env)
    classify(env, monkeypatch, text)
    before = len(env.model.requests)
    result = await env.send(text)
    assert len(env.model.requests) == before + 1
    assert "checked the current course record" in result.reply
    assert "weekends" in result.reply and "28000" not in result.reply
    assert [action.result.tool for action in result.actions] == ["getCourseDetails"]
    assert result.actions[0].handler_invoked
    assert await env.storage.db.leads.count_documents({}) == 0
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["schedule"] is None


@pytest.mark.asyncio
async def test_verification_does_not_reuse_previous_schedule(agent_env, monkeypatch):
    env = agent_env
    await selected(env)
    await env.storage.db.courses.update_one(
        {"business_id": "training-institute", "course_id": "DS202"},
        {"$set": {"schedule": "weekday"}},
    )
    text = "are you sure it runs on weekends"
    classify(env, monkeypatch, text)
    result = await env.send(text)
    assert "weekdays" in result.reply and "weekends" not in result.reply
    assert not result.reply.startswith("Yes")


@pytest.mark.parametrize(
    "text,fields,expected,absent",
    [
        ("How much does it cost?", ("fee",), "28000", "weekends"),
        ("Does it still have places?", ("availability",), "currently available", "28000"),
        (
            "Can you confirm its fee and schedule?",
            ("fee", "schedule"),
            "28000",
            "currently available",
        ),
    ],
)
@pytest.mark.asyncio
async def test_selected_fact_fields_are_targeted(
    agent_env, monkeypatch, text, fields, expected, absent
):
    env = agent_env
    await selected(env)
    classify(env, monkeypatch, text, fields)
    result = await env.send(text)
    assert expected in result.reply and absent not in result.reply


@pytest.mark.asyncio
async def test_mixed_policy_question_keeps_normal_planner(agent_env, monkeypatch):
    env = agent_env
    await selected(env)
    text = "Does it run on weekends and what is the refund policy?"
    classify(env, monkeypatch, text)
    before = len(env.model.requests)
    result = await env.send(
        text,
        steps=[
            [("getCourseDetails", {"courseID": "DS202"}), ("readKnowledge", {"query": "refund"})]
        ],
    )
    assert len(env.model.requests) > before + 1
    assert result.sources
    assert "seven days" in result.reply


@pytest.mark.asyncio
async def test_other_named_course_does_not_take_selected_course_shortcut(agent_env, monkeypatch):
    env = agent_env
    await selected(env)
    text = "Is Python Basics available on weekends?"
    classify(env, monkeypatch, text)
    before = len(env.model.requests)
    result = await env.send(text, steps=[[("getCourseDetails", {"courseID": "PY101"})]])
    assert len(env.model.requests) > before + 1
    assert "Python Basics" in result.reply
    assert "Applied Data Analytics" not in result.reply


@pytest.mark.asyncio
async def test_contact_correction_is_not_discarded(agent_env, monkeypatch):
    env = agent_env
    await selected(env)
    text = "My name is Rahul. Does it run on weekends?"
    classify(env, monkeypatch, text)
    before = len(env.model.requests)
    await env.send(
        text,
        changes=[change("name", "Rahul", "Rahul")],
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
    )
    assert len(env.model.requests) > before + 1
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["name"] == "Rahul"


def test_field_rendering_uses_record_not_question_polarity():
    result = course_reply(
        course(schedule="weekday"),
        "Are you sure it runs on weekends?",
        state(),
        fields=("schedule",),
        confirmation=True,
    )
    assert "weekdays" in result and "weekends" not in result and "28000" not in result
