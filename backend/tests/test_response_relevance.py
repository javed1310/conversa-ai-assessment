"""Social turns and irrelevant evidence must never trigger a catalog fallback."""

import pytest

from app.generation import SocialReply, StateDraft
from app.intent import callback_request
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change, prepare_enquiry_offer
from tests.test_course_followup import selected


def social_cue(env, monkeypatch, kind="acknowledgement"):
    original = env.engine.generation._structured

    async def structured(model, system, data, **kwargs):
        result = await original(model, system, data, **kwargs)
        if model is StateDraft:
            return result.model_copy(
                update={"social_reply": SocialReply(kind=kind, excerpt=data["customerText"])}
            )
        return result

    monkeypatch.setattr(env.engine.generation, "_structured", structured)


@pytest.mark.parametrize("text", ["okay perfect", "Great, got it", "Sounds good", "Understood."])
async def test_acknowledgement_needs_no_catalog_or_write(agent_env, monkeypatch, text):
    env = agent_env
    await selected(env)
    social_cue(env, monkeypatch)
    before = len(env.model.requests)
    result = await env.send(text)
    assert len(env.model.requests) == before + 1
    assert result.reply.startswith("Glad that helps")
    assert "28000" not in result.reply and "weekends" not in result.reply
    assert not result.actions and not result.sources
    assert await env.storage.db.leads.count_documents({}) == 0
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["agent_trace"]["outcome"] == "social"
    assert turn["agent_trace"]["next_step"] == "none"


async def test_thanks_is_not_an_enquiry_acceptance(agent_env, monkeypatch):
    env = agent_env
    await prepare_enquiry_offer(env)
    social_cue(env, monkeypatch, "thanks")
    result = await env.send("Thank you")
    assert "You're welcome" in result.reply and not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0
    turn = await env.storage.db.turns.find_one({"message_id": result.message_id})
    assert turn["agent_trace"]["next_step"] == "offer_enquiry"


@pytest.mark.parametrize(
    "text", ["okay perfect, but what is its fee?", "Thanks, I want to enquire", "okay, call me"]
)
async def test_wrong_social_cue_does_not_swallow_mixed_request(agent_env, monkeypatch, text):
    env = agent_env
    await selected(env)
    social_cue(env, monkeypatch)
    before = len(env.model.requests)
    result = await env.send(text)
    if callback_request(text):
        assert len(env.model.requests) == before + 1
        assert "phone" in result.reply and "name" in result.reply and not result.actions
    else:
        assert len(env.model.requests) > before + 1
    assert not result.reply.startswith("Glad that helps")


async def test_state_correction_is_not_discarded_by_social_cue(agent_env, monkeypatch):
    env = agent_env
    await selected(env)
    social_cue(env, monkeypatch)
    result = await env.send("My budget is 20000", changes=[change("budget", "20000", "20000")])
    assert not result.reply.startswith("Glad that helps")
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["budget"] == 20000


@pytest.mark.parametrize("course_use", [None, "none", "fields"])
async def test_irrelevant_course_read_is_not_used_as_answer(agent_env, course_use):
    env = agent_env
    await selected(env)
    selection = {"kind": "evidence", "evidenceKeys": ["tool-1"], "clarification": "none"}
    if course_use is not None:
        selection["courseUse"] = course_use
    result = await env.send(
        "Is a guaranteed overseas job included?",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
        selection=selection,
    )
    assert "don't have verified information" in result.reply
    assert "28000" not in result.reply and "weekends" not in result.reply
    assert [a.result.tool for a in result.actions] == ["getCourseDetails"]


async def test_requested_field_rendering_does_not_expand_to_overview(agent_env):
    env = agent_env
    await selected(env)
    result = await env.send(
        "What is its fee?",
        steps=[[("getCourseDetails", {"courseID": "DS202"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["tool-1"],
            "clarification": "none",
            "courseUse": "fields",
            "courseFields": ["fee"],
        },
    )
    assert "28000" in result.reply and "weekends" not in result.reply
