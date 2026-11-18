"""Semantic choice proposals use owned focus, independent guards and durable checkpoints."""

import pytest

from app.gateway import GatewayFailure
from app.gemini import ProviderFailure
from app.generation import ChoiceReview, DiscussedChoice, StateDraft
from tests.test_agent import agent_env as agent_env
from tests.test_assessment_handoff import discussed


def proposals(env, monkeypatch, verdict=True, fail_first=False, include_choice=True):
    original = env.engine.generation._structured
    calls = []

    async def structured(model, system, data, **kwargs):
        if model is StateDraft:
            result = await original(model, system, data, **kwargs)
            if not include_choice:
                return result
            return result.model_copy(
                update={
                    "discussed_choice": DiscussedChoice(
                        course_id="DS202", excerpt=data["customerText"], purpose="enquiry"
                    )
                }
            )
        if model is ChoiceReview:
            calls.append(data)
            if fail_first and len(calls) == 1:
                raise ProviderFailure("UNAVAILABLE", True)
            return ChoiceReview(
                explicit_interest=verdict, same_course=verdict, no_contradiction=verdict
            )
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", structured)
    return calls


@pytest.mark.parametrize(
    "text",
    [
        "That works for me. Please arrange for someone to phone me about this option.",
        "I'd like an enquiry for the option we just discussed. Please phone me.",
        "Let's go with this option; could you contact me?",
    ],
)
async def test_unfamiliar_choice_keeps_selected_course_through_callback(
    agent_env, monkeypatch, text
):
    env = agent_env
    await discussed(env)
    reviews = proposals(env, monkeypatch)
    result = await env.send(text)
    assert "name" in result.reply and "phone" in result.reply and not result.actions
    assert len(reviews) == 1
    saved = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert saved["state"]["fields"]["course_id"] == "DS202"
    await env.send("Mira Sharma 9876543210")
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS202"
    assert lead["callback_request_id"]


@pytest.mark.parametrize(
    "text",
    [
        "If I decide later, I might enquire about this option.",
        '"I choose this option" is only an example.',
        "No, I do not wish to enquire about this option.",
        "Actually my budget is lower; I want a different option.",
        "I want DS101 instead of this option.",
    ],
)
async def test_raw_contradiction_blocks_even_positive_model_verdict(agent_env, monkeypatch, text):
    env = agent_env
    await discussed(env)
    reviews = proposals(env, monkeypatch)
    await env.send(text)
    state = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert state["state"]["fields"]["course_id"] is None
    assert not reviews
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_rejected_review_requires_course_confirmation_before_contact_save(
    agent_env, monkeypatch
):
    env = agent_env
    await discussed(env)
    proposals(env, monkeypatch, verdict=False)
    result = await env.send("Let's go with this option; could you contact me?")
    assert "confirm" in result.reply and not result.actions
    after = await env.send("Mira Sharma 9876543210")
    assert "confirm" in after.reply and not after.actions
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize("include_choice", [True, False])
async def test_choice_review_outage_resumes_original_proposal(
    agent_env, monkeypatch, include_choice
):
    env = agent_env
    await discussed(env)
    reviews = proposals(env, monkeypatch, fail_first=True, include_choice=include_choice)
    with pytest.raises(GatewayFailure):
        await env.send("Let's go with this option; could you contact me?")
    body = list(env.payloads.values())[-1]
    result = await env.gateway.handle(env.context, body)
    assert "name" in result.payload.reply and "phone" in result.payload.reply
    assert len(reviews) == 2
    turn = await env.storage.db.turns.find_one({"message_id": body["messageId"]})
    assert sum(step["stage"] == "customer" for step in turn["state_checkpoints"]) == 1
    assert sum(step["stage"] == "selection" for step in turn["state_checkpoints"]) == 1
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] == "DS202"


async def test_missing_optional_cue_cannot_save_a_generic_interest_lead(agent_env, monkeypatch):
    env = agent_env
    await discussed(env)
    reviews = proposals(env, monkeypatch, include_choice=False)
    result = await env.send(
        "That works for me. Please arrange for someone to phone me about this option."
    )
    assert "name" in result.reply and len(reviews) == 1
    await env.send("Mira Sharma 9876543210")
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "DS202" and lead["callback_request_id"]


async def test_literal_owned_course_recovers_omitted_intake_choice(agent_env, monkeypatch):
    env = agent_env
    reviews = proposals(env, monkeypatch, include_choice=False)
    result = await env.send("I want to enquire about PY101. Please call me.")
    assert "name" in result.reply and "phone" in result.reply and len(reviews) == 1
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] == "PY101"
    from tests.test_agent import change

    await env.send(
        "Alice Morgan +12025550199",
        changes=[
            change("name", "Alice Morgan", "Alice Morgan"),
            change("phone", "+12025550199", "+12025550199"),
        ],
    )
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] == "PY101"
    assert lead["customer"]["phone"] == "+12025550199" and lead["callback_request_id"]


@pytest.mark.parametrize(
    "text",
    [
        "Please call me about PY101 or DS202.",
        "I do not want PY101. Please call me.",
    ],
)
async def test_omitted_intake_choice_does_not_guess_ambiguous_or_denied_course(
    agent_env, monkeypatch, text
):
    env = agent_env
    reviews = proposals(env, monkeypatch, include_choice=False)
    await env.send(text)
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["course_id"] is None
    assert not reviews and await env.storage.db.leads.count_documents({}) == 0
