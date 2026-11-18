"""End-to-end owned gateway/agent/action flow with mocked model HTTP and real MongoDB."""

from dataclasses import replace

import pytest

from app.tools import ToolRegistry
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


def actual_lead_id(request):
    for content in reversed(request["contents"]):
        for part in content["parts"]:
            result = part.get("functionResponse", {})
            if result.get("name") == "createLead" and result["response"].get("status") == "success":
                return {"leadID": result["response"]["result"]["leadId"]}
    raise AssertionError("No actual createLead result available for callback")


def details():
    return {"name": "Rahul", "phone": "9876543210", "statedInterest": "data science"}


async def intake(env):
    return await env.send(
        "My name is Rahul, my phone is 9876543210 and I want data science. Please call me.",
        changes=[
            change("name", "Rahul", "Rahul"),
            change("phone", "9876543210", "9876543210"),
            change("stated_interest", "data science", "data science"),
        ],
        steps=[[("createLead", details())], [("requestCallback", actual_lead_id)]],
    )


def replace_handler(env, name, handler):
    definitions = tuple(
        replace(item, handler=handler) if item.name == name else item
        for item in env.engine.tools._definitions.values()
    )
    registry = ToolRegistry(definitions)
    env.engine.tools = registry
    env.engine.executor.registry = registry


@pytest.mark.integration
class TestActionAgent:
    async def test_real_returned_lead_id_drives_callback_and_server_receipts(self, agent_env):
        env = agent_env
        result = await intake(env)
        assert [action.result.tool for action in result.actions] == [
            "createLead",
            "requestCallback",
        ]
        assert all(action.result.status == "success" for action in result.actions)
        lead_id = result.actions[0].result.data["leadId"]
        assert result.actions[1].result.data["leadId"] == lead_id
        request_id = result.actions[1].result.data["requestId"]
        assert lead_id in result.reply and request_id in result.reply
        assert "enrolment succeeded" not in result.reply
        document = await env.storage.db.leads.find_one({})
        assert document["lead_id"] == lead_id and document["callback_request_id"] == request_id
        assert document["automation"]["status"] == "pending" and document["notification"] is None

    async def test_prior_callback_intent_waits_for_phone_without_reasking_known_name(
        self, agent_env
    ):
        env = agent_env
        first = await env.send(
            "My name is Rahul and I want data science. Please call me.",
            changes=[
                change("name", "Rahul", "Rahul"),
                change("stated_interest", "data science", "data science"),
            ],
            selection={"kind": "clarify", "evidenceKeys": [], "clarification": "contact"},
        )
        assert "provide your phone" in first.reply and "provide your name" not in first.reply
        assert await env.storage.db.leads.count_documents({}) == 0
        second = await env.send(
            "My phone is 9876543210",
            changes=[change("phone", "9876543210", "9876543210")],
            steps=[[("createLead", details())], [("requestCallback", actual_lead_id)]],
        )
        assert all(action.result.status == "success" for action in second.actions)
        assert "callback is requested" in second.reply

    async def test_failed_lead_blocks_callback_handler_and_has_no_event(self, agent_env):
        env = agent_env
        calls = []

        async def failed_create(*_):
            raise RuntimeError("synthetic failure before save")

        async def forbidden_callback(*_):
            calls.append("invoked")
            raise AssertionError("callback must not be invoked after lead failure")

        replace_handler(env, "createLead", failed_create)
        replace_handler(env, "requestCallback", forbidden_callback)
        result = await env.send(
            "Rahul 9876543210 data science. Please call me.",
            changes=[
                change("name", "Rahul", "Rahul"),
                change("phone", "9876543210", "9876543210"),
                change("stated_interest", "data science", "data science"),
            ],
            steps=[[("createLead", details())], [("requestCallback", {"leadID": "guessed"})]],
        )
        assert not calls and all(action.result.status == "failure" for action in result.actions)
        assert not any(action.result.tool == "requestCallback" for action in result.actions)
        assert "Your enquiry is saved" not in result.reply
        assert await env.storage.db.leads.count_documents({}) == 0

    async def test_callback_failure_retains_saved_lead_and_pending_summary_event(self, agent_env):
        env = agent_env

        async def failed_callback(*_):
            raise RuntimeError("synthetic callback failure")

        replace_handler(env, "requestCallback", failed_callback)
        result = await intake(env)
        assert result.actions[0].result.status == "success"
        assert result.actions[1].result.status == "failure"
        assert "Your enquiry remains saved" in result.reply
        document = await env.storage.db.leads.find_one({})
        assert document["callback_requested_at"] is None
        assert document["automation"]["status"] == "pending"

    async def test_final_generation_failure_cannot_erase_verified_business_success(self, agent_env):
        env = agent_env
        env.model.final_failure = True
        result = await intake(env)
        assert all(action.result.status == "success" for action in result.actions)
        assert "Your enquiry is saved" in result.reply and "callback is requested" in result.reply
        assert "No enquiry" not in result.reply
        assert await env.storage.db.leads.count_documents({}) == 1

    async def test_later_correction_does_not_claim_saved_enquiry_updated(self, agent_env):
        env = agent_env
        await intake(env)
        result = await env.send(
            "Change my phone to 9876543211", changes=[change("phone", "9876543211", "9876543211")]
        )
        assert "have not updated" in result.reply
        document = await env.storage.db.leads.find_one({})
        assert document["customer"]["phone"] == "+919876543210"

    async def test_repeated_intent_new_message_reuses_actual_lead_and_callback(self, agent_env):
        env = agent_env
        first = await intake(env)
        repeated = await env.send(
            "Please call me again about the same enquiry",
            steps=[[("createLead", details())], [("requestCallback", actual_lead_id)]],
        )
        assert repeated.actions[0].result.data["leadId"] == first.actions[0].result.data["leadId"]
        assert (
            repeated.actions[1].result.data["requestId"]
            == first.actions[1].result.data["requestId"]
        )
        assert await env.storage.db.leads.count_documents({}) == 1
