"""Admission, shared deadlines, retry rules and side-effect safety; no live API requests."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError

from app.embeddings import EmbeddingFailure, GeminiEmbedder
from app.gemini import GeminiClient, ProviderFailure
from app.main import create_app
from app.measurements import Measurements
from app.provider_control import (
    ProviderControl,
    ProviderControlFailure,
    budget,
    deadline,
    remaining,
    retry_after,
)
from app.settings import Settings
from tests.test_actions import create, qualify
from tests.test_generation import envelope
from tests.test_state import state_env as state_env


def configuration(**updates):
    return Settings(_env_file=None, gemini_api_key="synthetic-never-used", **updates)


def test_child_deadline_cannot_extend_parent_and_context_restores():
    assert math.isinf(remaining())
    with deadline(1):
        before = remaining()
        with deadline(100):
            assert remaining() <= before
        with deadline(0):
            with pytest.raises(ProviderControlFailure) as error:
                budget(20)
            assert error.value.code == "DEADLINE_EXCEEDED"
        assert remaining() > 0
    assert math.isinf(remaining())


@pytest.mark.parametrize("value", [None, "bad", "NaN", "-1", "0.5", "١٢"])
def test_retry_after_ignores_invalid_values(value):
    assert retry_after(value) is None


def test_retry_after_supports_delay_seconds_dates_and_huge_valid_delays():
    assert retry_after("2") == 2
    assert retry_after("0") == 0
    assert 58 <= retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=60))) <= 60
    assert retry_after("9" * 400) == math.inf


@pytest.mark.parametrize(
    "updates",
    [
        {"provider_chat_concurrency": 0},
        {"provider_background_concurrency": 0},
        {"provider_queue_limit": 65},
        {"provider_queue_seconds": float("nan")},
        {"provider_generation_seconds": 1},
        {"provider_embedding_seconds": 1},
        {"provider_retry_base_seconds": 2, "provider_retry_max_seconds": 1},
    ],
)
def test_invalid_provider_budgets_fail_configuration(updates):
    with pytest.raises(ValidationError):
        configuration(**updates)


async def test_reserved_lanes_allow_both_classes_to_progress_without_borrowing():
    control = ProviderControl(
        configuration(provider_chat_concurrency=1, provider_background_concurrency=1)
    )
    async with control.slot("chat"):
        async with control.slot("background"):
            assert control.stats() == {
                "chat": {"active": 1, "waiting": 0},
                "background": {"active": 1, "waiting": 0},
            }
    assert all(row["active"] == 0 for row in control.stats().values())


async def test_queue_is_bounded_and_cancellation_releases_waiting_capacity():
    control = ProviderControl(configuration(provider_chat_concurrency=1, provider_queue_limit=1))
    queued_started = asyncio.Event()

    async def wait():
        queued_started.set()
        async with control.slot("chat"):
            pass

    async with control.slot("chat"):
        waiting = asyncio.create_task(wait())
        await queued_started.wait()
        assert control.stats()["chat"]["waiting"] == 1
        with pytest.raises(ProviderControlFailure) as error:
            async with control.slot("chat"):
                pass
        assert error.value.code == "PROVIDER_BUSY"
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert control.stats()["chat"]["waiting"] == 0
    async with control.slot("chat"):
        assert control.stats()["chat"]["active"] == 1


async def test_queue_wait_timeout_does_not_leak_a_permit():
    control = ProviderControl(
        configuration(provider_chat_concurrency=1, provider_queue_seconds=0.01)
    )
    async with control.slot("chat"):
        with pytest.raises(ProviderControlFailure):
            async with control.slot("chat"):
                pass
        assert control.stats()["chat"] == {"active": 1, "waiting": 0}
    async with control.slot("chat"):
        pass
    assert control.stats()["chat"]["active"] == 0


async def test_logical_call_limit_also_bounds_backoff_waiters_and_releases_on_cancel():
    control = ProviderControl(configuration(provider_chat_concurrency=1, provider_queue_limit=1))
    entered = asyncio.Event()

    async def waiting_call():
        async with control.call("chat", 10):
            entered.set()
            await asyncio.Event().wait()

    async with control.call("chat", 10):
        pending = asyncio.create_task(waiting_call())
        await entered.wait()
        assert control.logical_stats()["chat"] == 2
        assert control.stats()["chat"]["active"] == 0
        with pytest.raises(ProviderControlFailure):
            async with control.call("chat", 10):
                pass
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert control.logical_stats()["chat"] == 1
    assert control.logical_stats()["chat"] == 0


async def test_failed_logical_call_releases_its_capacity_and_restores_parent_budget():
    control = ProviderControl(configuration(provider_chat_concurrency=1, provider_queue_limit=0))
    with pytest.raises(ValueError):
        async with control.call("chat", 1):
            raise ValueError("fixture failure")
    assert control.logical_stats()["chat"] == 0 and math.isinf(remaining())
    async with control.call("chat", 1):
        assert control.logical_stats()["chat"] == 1


async def test_expired_deadline_prevents_generation_and_embedding_http():
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request))
    settings = configuration()
    control = ProviderControl(settings)
    async with GeminiClient(
        settings, http_client=httpx.AsyncClient(transport=transport), provider_control=control
    ) as model:
        embedder = GeminiEmbedder(
            settings, http_client=httpx.AsyncClient(transport=transport), provider_control=control
        )
        try:
            with deadline(0):
                with pytest.raises(ProviderFailure) as gen:
                    await model.generate(
                        system="hello", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
                    )
                with pytest.raises(EmbeddingFailure) as emb:
                    await embedder.embed(("hello",))
            assert gen.value.code == emb.value.code == "DEADLINE_EXCEEDED"
            assert calls == []
        finally:
            await embedder.close()


async def test_parent_deadline_cancels_http_without_starting_another_attempt():
    calls = 0

    async def slow(_request):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow))
    ) as model:
        with deadline(0.02):
            with pytest.raises(ProviderFailure):
                await model.generate(
                    system="hello", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
                )
    assert calls == 1


async def test_cancelling_inflight_http_releases_shared_lane():
    settings = configuration(provider_chat_concurrency=1)
    control = ProviderControl(settings)
    started = asyncio.Event()

    async def slow(_request):
        started.set()
        await asyncio.Event().wait()

    async with GeminiClient(
        settings,
        provider_control=control,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
    ) as model:
        pending = asyncio.create_task(
            model.generate(
                system="hello", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
            )
        )
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    async with control.slot("chat"):
        assert control.stats()["chat"]["active"] == 1


async def test_retry_after_that_cannot_fit_is_not_retried_early():
    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    recorder = Measurements()
    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail))
    ) as model:
        with recorder.operation("probe"):
            with pytest.raises(ProviderFailure) as error:
                await model.generate(
                    system="hello", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
                )
    assert error.value.code == "RATE_LIMIT" and len(requests) == 1
    assert recorder.recent()[0]["counters"]["provider_retry_skipped"] == 1


async def test_retry_delay_honors_retry_after_and_releases_slot(monkeypatch):
    import app.provider_control as module

    sleeps = []
    control = ProviderControl(configuration())

    async def sleep(seconds):
        sleeps.append(seconds)
        assert all(row["active"] == 0 for row in control.stats().values())

    monkeypatch.setattr(module.random, "uniform", lambda *_: 0.1)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    assert await control.backoff(0, httpx.Response(503, headers={"Retry-After": "1"}))
    assert sleeps == [1]
    with deadline(0):
        assert not await control.backoff(0, None)


async def test_application_shares_one_control_between_generation_and_embeddings():
    app = create_app(configuration(ai_chat_enabled=False))
    async with LifespanManager(app):
        assert app.state.provider.control is app.state.embedder.control
        assert app.state.provider.control is app.state.provider_control


async def test_generation_and_query_embeddings_share_chat_lane_while_background_can_run():
    settings = configuration(
        provider_chat_concurrency=1, provider_queue_limit=0, embedding_dimensions=128
    )
    control = ProviderControl(settings)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def transport(request):
        calls.append(request.url.path)
        if request.url.path.endswith(":batchEmbedContents"):
            return httpx.Response(200, json={"embeddings": [{"values": [1.0] + [0.0] * 127}]})
        if not started.is_set():
            started.set()
            await release.wait()
        return httpx.Response(200, json=envelope([{"text": "hello"}]))

    async with GeminiClient(
        settings,
        provider_control=control,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    ) as model:
        embedder = GeminiEmbedder(
            settings,
            provider_control=control,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )
        pending = asyncio.create_task(
            model.generate(
                system="hello", contents=[{"role": "user", "parts": [{"text": "hello"}]}]
            )
        )
        await started.wait()
        try:
            with pytest.raises(EmbeddingFailure) as error:
                await embedder.embed(("question",), task="query")
            assert error.value.code == "PROVIDER_BUSY"
            assert len(await embedder.embed(("document",), task="document")) == 1
            assert (
                await model.generate(
                    system="summary",
                    summary=True,
                    contents=[{"role": "user", "parts": [{"text": "hello"}]}],
                )
            ).text == "hello"
        finally:
            release.set()
            await pending
            await embedder.close()
    assert len(calls) == 3


@pytest.mark.integration
async def test_no_new_business_write_starts_after_deadline(state_env):
    ctx, customer = await qualify(state_env)
    with deadline(0):
        outcome = await state_env.executor.execute(ctx, create(customer))
    assert outcome.result.status == "failure" and outcome.handler_invoked is False
    assert await state_env.storage.db.leads.count_documents({}) == 0


@pytest.mark.integration
async def test_verified_write_receipt_replays_even_when_new_work_deadline_is_expired(state_env):
    ctx, customer = await qualify(state_env)
    call = create(customer)
    first = await state_env.executor.execute(ctx, call)
    assert first.result.status == "success"
    with deadline(0):
        again = await state_env.executor.execute(ctx, call)
    assert again.result.status == "success" and not again.handler_invoked and again.replayed
    assert again.result.data == first.result.data
    assert await state_env.storage.db.leads.count_documents({}) == 1
