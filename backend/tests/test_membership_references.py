"""Ordinal membership choices preserve displayed identity and fetch current records."""

import json

import pytest

from tests.test_agent import agent_env as agent_env
from tests.test_membership_completion import fitness_context


@pytest.mark.parametrize(
    "text,expected,absent",
    [
        ("i will choose first one", "Monthly Studio Membership", "Quarterly Studio Membership"),
        ("second one", "Quarterly Studio Membership", "Monthly Studio Membership"),
        ("I'll take the first plan", "Monthly Studio Membership", "Quarterly Studio Membership"),
    ],
)
async def test_membership_choice_resolves_displayed_option_without_model_call(
    agent_env, text, expected, absent
):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    before = len(env.model.requests)
    result = await env.send(text, target=context)
    assert expected in result.reply and absent not in result.reply
    assert len(env.model.requests) == before
    assert [a.result.tool for a in result.actions] == ["getMembershipDetails"]
    assert result.actions[0].result.status == "success"
    assert await env.storage.db.leads.count_documents({}) == 0
    session = await env.storage.db.sessions.find_one({"session_id": context.session_id})
    assert session["state"]["fields"]["course_id"] is None


async def test_membership_choice_reads_updated_fee_not_old_receipt(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    await env.storage.db.memberships.update_one(
        {"business_id": "fitness-studio", "membership_id": "FIT-MONTHLY"},
        {"$set": {"fee": 2300}},
    )
    reply = await env.send("i will choose first one", target=context)
    assert "2300" in reply.reply and "2000" not in reply.reply
    assert reply.actions[0].result.data["memberships"][0]["membershipId"] == "FIT-MONTHLY"


async def test_membership_reference_cannot_cross_sessions(agent_env):
    env = agent_env
    first = await fitness_context(env)
    second = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=first,
        steps=[[("getMembershipDetails", {})]],
    )
    reply = await env.send("i will choose first one", target=second)
    assert "Which membership" in reply.reply and not reply.actions


async def test_selected_membership_id_is_available_for_later_followups(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    await env.send("i will choose first one", target=context)
    await env.send(
        "What benefits come with it?",
        target=context,
        steps=[[("getMembershipDetails", {"membershipID": "FIT-MONTHLY"})]],
    )
    requests = [r for r in env.model.requests if "tools" in r]
    data = json.loads(requests[-2]["contents"][0]["parts"][0]["text"])
    assert data["discussedMembershipId"] == "FIT-MONTHLY"
    assert [item["membershipId"] for item in data["membershipReferences"]] == [
        "FIT-MONTHLY",
        "FIT-QUARTERLY",
    ]


async def test_out_of_range_choice_does_not_guess_membership(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    reply = await env.send("third one", target=context)
    assert "Which membership" in reply.reply and not reply.actions


async def test_changing_to_second_option_uses_the_original_displayed_list(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    await env.send("i will choose first one", target=context)
    result = await env.send("second one", target=context)
    assert (
        "Quarterly Studio Membership" in result.reply
        and "Monthly Studio Membership" not in result.reply
    )
    assert result.actions[0].result.data["memberships"][0]["membershipId"] == "FIT-QUARTERLY"


async def test_unproven_display_order_is_not_guessed(agent_env):
    env = agent_env
    context = await fitness_context(env)
    listed = await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    await env.storage.db.turns.update_one(
        {"message_id": listed.message_id},
        {"$set": {"output.reply": "Quarterly Studio Membership then Monthly Studio Membership"}},
    )
    result = await env.send("first one", target=context)
    assert "Which membership" in result.reply and not result.actions


@pytest.mark.parametrize(
    "text,expected,absent",
    [
        ("How much does it cost?", "costs INR 2000 for 1 month", "Access to"),
        ("How long does it last?", "lasts 1 month", "2000"),
    ],
)
async def test_focused_membership_fact_uses_fresh_record_without_model(
    agent_env, text, expected, absent
):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    await env.send("first one", target=context)
    before = len(env.model.requests)
    result = await env.send(text, target=context)
    assert expected in result.reply and absent not in result.reply
    assert len(env.model.requests) == before
    assert result.actions[0].result.status == "success"


async def test_ambiguous_membership_price_needs_selected_plan(agent_env):
    env = agent_env
    context = await fitness_context(env)
    await env.send(
        "What membership options are available?",
        target=context,
        steps=[[("getMembershipDetails", {})]],
    )
    result = await env.send("How much does it cost?", target=context)
    assert "Which membership" in result.reply and not result.actions
