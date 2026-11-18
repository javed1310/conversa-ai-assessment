"""Provider failures expose safe timing categories, never keys or payloads."""

import asyncio

import httpx
import pytest

from app.gemini import GeminiClient, ProviderFailure
from app.measurements import Measurements
from tests.test_generation import SECRET, configuration


@pytest.mark.parametrize(
    "timeout_type,counter",
    [
        (httpx.ConnectTimeout, "provider_connect_timeouts"),
        (httpx.ReadTimeout, "provider_read_timeouts"),
        (httpx.WriteTimeout, "provider_write_timeouts"),
        (httpx.PoolTimeout, "provider_pool_timeouts"),
    ],
)
async def test_transport_timeout_category_is_recorded_without_sensitive_values(
    timeout_type, counter, caplog
):
    async def handler(request):
        raise timeout_type(SECRET, request=request)

    recorder = Measurements()
    async with GeminiClient(
        configuration(provider_attempts=1),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as provider:
        with recorder.operation("probe"), pytest.raises(ProviderFailure) as error:
            await provider.start("private system", "private customer text", ())
    assert error.value.code == "TIMEOUT"
    assert error.value.detail == timeout_type.__name__
    assert recorder.recent()[0]["counters"][counter] == 1
    assert timeout_type.__name__ in caplog.text and "duration_ms=" in caplog.text
    assert SECRET not in caplog.text and "private customer text" not in caplog.text


@pytest.mark.parametrize(
    "status,counter,code",
    [
        (429, "provider_http_rate_limits", "RATE_LIMIT"),
        (503, "provider_http_server_errors", "UNAVAILABLE"),
    ],
)
async def test_rate_limit_and_server_error_are_distinguishable(status, counter, code, caplog):
    recorder = Measurements()
    async with GeminiClient(
        configuration(provider_attempts=1),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, text=SECRET))
        ),
    ) as provider:
        with recorder.operation("probe"), pytest.raises(ProviderFailure) as error:
            await provider.start("system", "customer", ())
    assert error.value.code == code
    assert recorder.recent()[0]["counters"][counter] == 1
    assert f"status={status}" in caplog.text and SECRET not in caplog.text


async def test_attempt_deadline_is_distinct_from_transport_read_timeout(caplog):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    recorder = Measurements()
    async with GeminiClient(
        configuration(provider_attempts=1, provider_generation_attempt_seconds=0.01),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as provider:
        with recorder.operation("probe"), pytest.raises(ProviderFailure) as error:
            await provider.start("system", "customer", ())
    assert error.value.code == "TIMEOUT" and error.value.detail == "ATTEMPT_DEADLINE"
    assert recorder.recent()[0]["counters"]["provider_attempt_deadlines"] == 1
    assert "ATTEMPT_DEADLINE" in caplog.text


async def test_daily_quota_does_not_spend_retries_or_expose_provider_body():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": SECRET,
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [
                                {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                            ],
                        }
                    ],
                }
            },
        )

    async with GeminiClient(
        configuration(provider_attempts=3),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("system", "membership options", ())
    assert error.value.code == "RATE_LIMIT" and error.value.detail == "DAILY_QUOTA"
    assert error.value.retryable and len(calls) == 1
    assert SECRET not in str(error.value)
