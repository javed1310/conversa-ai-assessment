"""Real gateway/storage recovery; never replay uncertain effects on an error label alone."""

import asyncio

import pytest

from app.contracts import ChatResult
from app.gateway import GatewayFailure
from app.gemini import ProviderFailure
from tests.test_agent import agent_env as agent_env
from tests.test_gateway import api as api
from tests.test_gateway import payload, start


@pytest.mark.parametrize("code", ["TIMEOUT", "UNAVAILABLE", "RATE_LIMIT"])
async def test_same_message_recovers_transient_failure_once_and_replays(api, code):
    client, _, engine, storage = api
    attempts = []

    async def respond(context, message):
        attempts.append(context.turn_id)
        if len(attempts) == 1:
            raise ProviderFailure(code, True)
        return ChatResult(
            message_id=message.message_id,
            session_id=message.session_id,
            reply="Verified transport recovery",
        )

    engine.respond = respond
    session, url, headers = await start(client)
    body = payload()
    assert (await client.post(url, headers=headers, json=body)).status_code == 503
    recovered = await client.post(url, headers=headers, json=body)
    assert recovered.status_code == 200
    replay = await client.post(url, headers=headers, json=body)
    assert replay.json() == recovered.json()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert await storage.db.turns.count_documents({"session_id": session["sessionId"]}) == 1
    assert await storage.db.leads.count_documents({}) == 0


async def test_transient_retry_budget_is_bounded(api):
    client, _, engine, _ = api
    attempts = []

    async def respond(context, message):
        attempts.append(context.turn_id)
        raise ProviderFailure("UNAVAILABLE", True)

    engine.respond = respond
    _, url, headers = await start(client)
    body = payload()
    for _ in range(5):
        assert (await client.post(url, headers=headers, json=body)).status_code == 503
    assert len(attempts) == 3  # Initial execution plus two explicitly requested retries.


@pytest.mark.parametrize(
    "tool,status",
    [("createLead", "completed"), ("requestCallback", "processing"), ("otherWrite", "processing")],
)
async def test_transient_failure_with_effects_requires_review(api, tool, status):
    client, _, engine, storage = api
    attempts = []

    async def respond(context, message):
        attempts.append(context.turn_id)
        await storage.db.tool_executions.insert_one(
            {
                "business_id": context.business_id,
                "session_id": context.session_id,
                "turn_id": context.turn_id,
                "call_id": "write",
                "tool": tool,
                "status": status,
            }
        )
        raise ProviderFailure("UNAVAILABLE", True)

    engine.respond = respond
    _, url, headers = await start(client)
    body = payload()
    assert (await client.post(url, headers=headers, json=body)).status_code == 503
    retry = await client.post(url, headers=headers, json=body)
    assert retry.status_code == 503
    assert retry.json()["error"]["code"] == "RECOVERY_REQUIRES_REVIEW"
    assert len(attempts) == 1


async def test_later_turn_prevents_replaying_transient_failure(api):
    client, _, engine, _ = api
    attempts = []

    async def respond(context, message):
        attempts.append(context.turn_id)
        if message.message_id == "first":
            raise ProviderFailure("TIMEOUT", True)
        return ChatResult(
            message_id=message.message_id, session_id=message.session_id, reply="Later turn"
        )

    engine.respond = respond
    _, url, headers = await start(client)
    first = payload("first")
    assert (await client.post(url, headers=headers, json=first)).status_code == 503
    assert (await client.post(url, headers=headers, json=payload("later"))).status_code == 200
    assert (await client.post(url, headers=headers, json=first)).status_code == 409
    assert len(attempts) == 2


async def test_concurrent_retries_execute_one_recovery(api):
    client, _, engine, _ = api
    attempts = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(context, message):
        attempts.append(context.turn_id)
        if len(attempts) == 1:
            raise ProviderFailure("UNAVAILABLE", True)
        entered.set()
        await release.wait()
        return ChatResult(
            message_id=message.message_id, session_id=message.session_id, reply="Single recovery"
        )

    engine.respond = respond
    _, url, headers = await start(client)
    body = payload()
    assert (await client.post(url, headers=headers, json=body)).status_code == 503
    task = asyncio.create_task(client.post(url, headers=headers, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        duplicate = await client.post(url, headers=headers, json=body)
        assert duplicate.status_code == 202
    finally:
        release.set()
        await task
    assert len(attempts) == 2


async def test_real_agent_preserves_transient_failure_for_gateway_recovery(agent_env, monkeypatch):
    env = agent_env
    original = env.engine.provider.start_context
    attempts = 0

    async def outage(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderFailure("UNAVAILABLE", True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(env.engine.provider, "start_context", outage)
    with pytest.raises(GatewayFailure) as failed:
        await env.send("What can you help me explore?")
    assert failed.value.code == "ENGINE_FAILURE"
    body = next(iter(env.payloads.values()))
    result = await env.gateway.handle(env.context, body)
    assert result.payload.status == "completed"
    assert attempts == 2
    assert await env.storage.db.turns.count_documents({}) == 1
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_changed_payload_cannot_reopen_failed_identity(api):
    client, _, engine, _ = api

    async def outage(context, message):
        raise ProviderFailure("TIMEOUT", True)

    engine.respond = outage
    _, url, headers = await start(client)
    body = payload()
    assert (await client.post(url, headers=headers, json=body)).status_code == 503
    changed = {**body, "content": "Different request"}
    assert (await client.post(url, headers=headers, json=changed)).status_code == 409
