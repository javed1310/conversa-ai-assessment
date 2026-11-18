"""A new business/tool renders real scoped data through registration alone."""

import json
from pathlib import Path

import pytest
from pydantic import Field, ValidationError

from app.businesses import BusinessConfig, BusinessRegistry
from app.contracts import Contract, ToolSuccess
from app.tools import ToolDefinition, ToolRegistry
from tests.test_agent import agent_env  # noqa: F401


class RoomArguments(Contract):
    room_id: str = Field(pattern=r"^[A-Z][0-9]{2}$")


class RoomResult(Contract):
    room_id: str
    available: bool
    nightly_fee: int = Field(ge=0)


async def room_details(repo, context, arguments):
    await repo.turn(context)
    row = await repo.db.audit_rooms.find_one(
        {"business_id": repo.business_id, "room_id": arguments.room_id},
        {"_id": 0, "business_id": 0},
    )
    return RoomResult.model_validate(row)


def definition():
    return ToolDefinition(
        "getRoomDetails",
        "Read a room's current availability and fee",
        RoomArguments,
        RoomResult,
        room_details,
        presentation=lambda result: (
            f"Room {result.room_id} costs INR {result.nightly_fee} per night; "
            + ("available." if result.available else "unavailable.")
        ),
        current_facts=True,
    )


@pytest.mark.asyncio
async def test_new_business_and_tool_work_without_agent_edits(agent_env, tmp_path: Path):  # noqa: F811
    env = agent_env
    tool = definition()
    tools = ToolRegistry(
        tuple(env.engine.tools.get(name) for name in env.engine.tools.names) + (tool,)
    )
    config = BusinessConfig(
        business_id="demo-hotel",
        business_name="Demo Hotel",
        agent_role="Room assistant",
        instructions=("Use the room tool for current room fees and availability.",),
        knowledge_base_id="hotel-kb",
        enabled_tools=(tool.name,),
        config_version=1,
    )
    folder = tmp_path / config.business_id
    folder.mkdir()
    (folder / "config.json").write_text(config.model_dump_json(by_alias=True))
    loaded = BusinessRegistry.load(tmp_path, registered_tools=tools.names)
    registry = BusinessRegistry((*env.registry.configurations(), *loaded.configurations()))
    env.engine.businesses = env.gateway.businesses = env.engine.executor.businesses = registry
    env.engine.tools = env.engine.executor.registry = tools
    await env.storage.db.audit_rooms.insert_one(
        {
            "business_id": config.business_id,
            "room_id": "A01",
            "available": True,
            "nightly_fee": 1800,
        }
    )
    await env.storage.db.audit_rooms.insert_one(
        {
            "business_id": "foreign-hotel",
            "room_id": "A01",
            "available": True,
            "nightly_fee": 1,
        }
    )
    session = await env.gateway.start_web_session(config.business_id)
    context = await env.gateway.web_context(
        config.business_id, session.session_id, session.session_token, env.context.request_id
    )
    answer = await env.send(
        "Is room A01 available, and what is its price?",
        target=context,
        steps=[[("getRoomDetails", {"roomId": "A01"})]],
    )
    assert answer.reply == "Room A01 costs INR 1800 per night; available."
    assert isinstance(answer.actions[0].result, ToolSuccess)
    assert answer.actions[0].result.data["nightlyFee"] == 1800
    denied = await env.send("Read room A01.", steps=[[("getRoomDetails", {"roomId": "A01"})]])
    assert "1800" not in denied.reply
    assert not any(a.result.status == "success" for a in denied.actions)


@pytest.mark.parametrize(
    "data",
    [
        {"roomId": "A01", "available": True, "nightlyFee": -1},
        {"roomId": "A01", "available": True, "nightlyFee": 10, "secret": "x"},
    ],
)
def test_registered_presentation_revalidates_results(data):
    with pytest.raises(ValidationError):
        ToolRegistry((definition(),)).render("getRoomDetails", data)


def test_new_tool_needs_presentation_and_unknown_tool_configuration_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="presentation"):
        ToolDefinition("getRoomDetails", "Read room", RoomArguments, RoomResult, room_details)
    folder = tmp_path / "demo-hotel"
    folder.mkdir()
    config = {
        "businessId": "demo-hotel",
        "businessName": "Hotel",
        "agentRole": "Assistant",
        "instructions": ["Help"],
        "knowledgeBaseId": "hotel-kb",
        "enabledTools": ["getRoomDetails"],
        "configVersion": 1,
    }
    (folder / "config.json").write_text(json.dumps(config))
    from app.settings import ConfigurationError

    with pytest.raises(ConfigurationError):
        BusinessRegistry.load(tmp_path)


@pytest.mark.parametrize("broken", [lambda result: "", lambda result: 1 / 0])
def test_invalid_presenter_fails_closed(broken):
    from dataclasses import replace

    tools = ToolRegistry((replace(definition(), presentation=broken),))
    with pytest.raises(ValidationError):
        tools.render("getRoomDetails", {"roomId": "A01", "available": True, "nightlyFee": 1800})
