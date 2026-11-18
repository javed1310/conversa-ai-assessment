"""Numeric telemetry safety, async isolation, usage coverage and actual HTTP adapter counters."""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr

from app.database import open_database
from app.embeddings import GeminiEmbedder
from app.gemini import GeminiClient, ProviderFailure
from app.main import create_app
from app.measurements import Measurements, count, operation, stage, usage
from app.settings import Settings
from tests.test_generation import envelope


def test_usage_whitelist_does_not_retain_payloads_or_unknown_values():
    recorder = Measurements()
    with recorder.operation("probe"):
        count("private-key-and-phone")
        usage(
            {
                "private": "never retain",
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": True,
                    "totalTokenCount": 15,
                    "thoughtsTokenCount": -1,
                    "cachedContentTokenCount": "private",
                    "customerName": "never retain",
                },
            },
            "generation",
        )
    encoded = json.dumps(recorder.recent())
    assert "private" not in encoded and "never retain" not in encoded
    counters = recorder.recent()[0]["counters"]
    assert counters["generation_promptTokenCount"] == 10
    assert counters["generation_totalTokenCount"] == 15
    assert "generation_candidatesTokenCount" not in counters


def test_missing_usage_is_unknown_and_explicit_zero_has_coverage():
    recorder = Measurements()
    with recorder.operation("probe"):
        usage({}, "generation")
        usage({"usageMetadata": {"totalTokenCount": 0}}, "generation")
    counters = recorder.recent()[0]["counters"]
    assert counters["generation_responses_without_usage"] == 1
    assert counters["generation_responses_with_usage"] == 1
    assert counters["generation_totalTokenCount"] == 0
    assert counters["generation_totalTokenCount_reports"] == 1


async def test_concurrent_operations_are_isolated_and_nested_operations_restore_context():
    recorder = Measurements()
    barrier = asyncio.Event()
    arrivals = 0

    async def run(value):
        nonlocal arrivals
        with recorder.operation("chat"):
            count("generation_calls", value)
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
            with recorder.operation("probe"):
                count("embedding_calls", 1)
            count("generation_calls", 1)

    tasks = [asyncio.create_task(run(value)) for value in (2, 5)]
    await asyncio.gather(*tasks)
    chats = [r for r in recorder.recent() if r["operation"] == "chat"]
    assert sorted(r["counters"]["generation_calls"] for r in chats) == [3, 6]
    assert all("embedding_calls" not in r["counters"] for r in chats)
    assert len([r for r in recorder.recent() if r["operation"] == "probe"]) == 2


async def test_cancellation_and_failure_are_counted_without_exception_text():
    started = asyncio.Event()
    recorder = Measurements()

    class Task:
        measurements = recorder

        @operation("chat")
        @stage("state")
        async def wait(self):
            started.set()
            await asyncio.Event().wait()

        @operation("chat")
        @stage("tool")
        async def fail(self):
            raise ValueError("secret contact payload")

    pending = asyncio.create_task(Task().wait())
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with pytest.raises(ValueError):
        await Task().fail()
    records = recorder.recent()
    assert records[0]["outcome"] == "cancelled"
    assert records[0]["stages"]["state"]["cancelled"] == 1
    assert records[1]["stages"]["tool"]["raised"] == 1
    assert "secret" not in json.dumps(records)


async def test_late_inherited_task_cannot_mutate_a_closed_trace():
    recorder = Measurements()
    release = asyncio.Event()

    async def late():
        await release.wait()
        count("generation_calls", 99)

    with recorder.operation("chat"):
        task = asyncio.create_task(late())
        count("generation_calls")
    release.set()
    await task
    assert recorder.recent()[0]["counters"]["generation_calls"] == 1


def test_ring_is_bounded_and_snapshots_cannot_modify_internal_records():
    recorder = Measurements(2)
    for value in range(3):
        with recorder.operation("probe"):
            count("generation_calls", value)
    records = recorder.recent()
    assert len(records) == 2 and records[0]["counters"]["generation_calls"] == 1
    records[0]["counters"]["generation_calls"] = 100
    assert recorder.recent()[0]["counters"]["generation_calls"] == 1
    with pytest.raises(ValueError):
        with recorder.operation("not-a-valid-stage"):
            pass


async def test_generation_retry_counts_attempts_separately_and_captures_usage():
    recorder = Measurements()
    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json=envelope([{"text": "hello"}])
            | {
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "totalTokenCount": 120,
                    "cachedContentTokenCount": 0,
                }
            },
        )

    async with GeminiClient(
        Settings(_env_file=None, gemini_api_key="synthetic-never-used"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    ) as client:
        with recorder.operation("probe"):
            assert (
                await client.generate(
                    system="greet",
                    contents=[{"role": "user", "parts": [{"text": "private customer input"}]}],
                )
            ).text == "hello"
    record = recorder.recent()[0]
    assert record["counters"]["generation_calls"] == 1
    assert record["counters"]["generation_http_attempts"] == 2
    assert record["counters"]["generation_totalTokenCount"] == 120
    assert record["stages"]["generation"]["calls"] == 1
    assert "private" not in json.dumps(record)


async def test_usage_is_retained_when_the_model_response_is_refused():
    recorder = Measurements()
    body = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 7}}
    async with GeminiClient(
        Settings(_env_file=None, gemini_api_key="synthetic-never-used"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    ) as client:
        with recorder.operation("probe"):
            with pytest.raises(ProviderFailure):
                await client.generate(
                    system="greet", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
                )
    record = recorder.recent()[0]
    assert record["counters"]["generation_promptTokenCount"] == 7
    assert record["stages"]["generation"]["raised"] == 1


async def test_embedding_batch_counts_texts_and_does_not_invent_missing_token_usage():
    recorder = Measurements()
    body = {"embeddings": [{"values": [1.0] + [0.0] * 127}] * 2}
    provider = GeminiEmbedder(
        Settings(_env_file=None, gemini_api_key="synthetic-never-used", embedding_dimensions=128),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    )
    try:
        with recorder.operation("probe"):
            assert len(await provider.embed(("private one", "private two"))) == 2
    finally:
        await provider.close()
    record = recorder.recent()[0]
    assert record["counters"]["embedding_calls"] == 1
    assert record["counters"]["embedding_http_attempts"] == 1
    assert record["counters"]["embedding_texts"] == 2
    assert record["counters"]["embedding_responses_without_usage"] == 1
    assert "embedding_totalTokenCount" not in record["counters"]
    assert "private" not in json.dumps(record)


@pytest.mark.integration
async def test_http_measurements_belong_to_the_application_database_lifespan(storage, monkeypatch):
    monkeypatch.setattr("app.main.open_database", open_database)
    settings = Settings(
        _env_file=None,
        mongodb_uri=SecretStr(f"mongodb://127.0.0.1:27017/{storage.db.name}"),
        ai_chat_enabled=False,
        automation_enabled=False,
    )
    app = create_app(settings)
    base = "/api/v1/businesses/training-institute"
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://measurement-test"
        ) as client,
    ):
        session = (await client.post(base + "/sessions", json={})).json()
        result = await client.post(
            base + f"/sessions/{session['sessionId']}/messages",
            headers={"X-Session-Token": session["sessionToken"]},
            json={
                "messageId": "measure-message",
                "content": "hello",
                "timestamp": "2026-11-16T00:00:00Z",
            },
        )
        assert result.status_code == 503
        actual = app.state.database.measurements.recent()
        assert len(actual) == 1 and actual[0]["operation"] == "chat"
        assert actual[0]["outcome"] == "raised"
        assert storage.measurements.recent() == []
