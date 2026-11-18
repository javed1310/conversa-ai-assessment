"""Gemini REST exchange tests use intercepted HTTP only; no live key or API calls."""

import json
import math

import httpx
import pytest

from app.embeddings import EmbeddingFailure, GeminiEmbedder
from app.settings import Settings

DIMENSIONS = 128


def settings():
    return Settings(
        _env_file=None, gemini_api_key="synthetic-not-a-real-key", embedding_dimensions=DIMENSIONS
    )


def vector(first=1, second=0):
    return [first, second] + [0] * (DIMENSIONS - 2)


@pytest.mark.parametrize(
    "task,task_type", [("document", "RETRIEVAL_DOCUMENT"), ("query", "RETRIEVAL_QUERY")]
)
async def test_gemini_request_order_tasks_auth_and_normalization(task, task_type):
    requests = []

    def handler(request):
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:batchEmbedContents"
        )
        assert request.headers["x-goog-api-key"] == "synthetic-not-a-real-key"
        assert "Authorization" not in request.headers
        assert "synthetic-not-a-real-key" not in str(request.url)
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"embeddings": [{"values": vector(3, 4)}, {"values": vector(0, 5)}]}
        )

    provider = GeminiEmbedder(
        settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        result = await provider.embed(("first", "second"), task=task)
        assert result[0][:2] == (0.6, 0.8) and result[1][:2] == (0, 1)
        assert all(math.hypot(*item) == pytest.approx(1) for item in result)
        assert requests[0] == {
            "requests": [
                {
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": DIMENSIONS,
                }
                for text in ("first", "second")
            ]
        }
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "body",
    [
        {"embeddings": []},
        {"embeddings": [{"values": vector()}, {"values": vector()}]},
        {"data": [{"index": 0, "embedding": vector()}], "model": "text-embedding-3-small"},
        {"error": {"message": "sensitive"}},
        {"embeddings": ["wrong-item"]},
    ],
)
async def test_gemini_invalid_response_count_and_legacy_envelope_rejected(body):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    provider = GeminiEmbedder(settings(), http_client=client)
    try:
        with pytest.raises(EmbeddingFailure) as error:
            await provider.embed(("Synthetic",))
        assert error.value.code == "INVALID_RESPONSE"
    finally:
        await provider.close()


@pytest.mark.parametrize("is_timeout", [True, False])
async def test_http_connection_failures_retry_once_and_stay_safe(is_timeout):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        error = httpx.ReadTimeout if is_timeout else httpx.ConnectError
        raise error("synthetic-private-connection-detail", request=request)

    provider = GeminiEmbedder(
        settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(EmbeddingFailure) as error:
            await provider.embed(("Synthetic policy",))
        assert calls == 2 and error.value.retryable
        assert error.value.code == ("TIMEOUT" if is_timeout else "UNAVAILABLE")
        assert "synthetic-private" not in str(error.value)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "texts", [(), (" ",), ("x",) * 33, ("word " * 10000,), ("\U0001f642" * 401,)]
)
async def test_invalid_inputs_never_reach_http(texts):
    def handler(_request):
        pytest.fail("Invalid input must not reach HTTP")

    provider = GeminiEmbedder(
        settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(EmbeddingFailure) as error:
            await provider.embed(texts)
        assert error.value.code == "INVALID_INPUT"
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "ACCESS_DENIED"),
        (403, "ACCESS_DENIED"),
        (400, "INVALID_REQUEST"),
        (429, "RATE_LIMIT"),
        (500, "UNAVAILABLE"),
        (307, "INVALID_REQUEST"),
    ],
)
async def test_http_errors_safe_and_redirects_not_followed(status, code, caplog):
    def handler(_request):
        return httpx.Response(
            status,
            headers={"Location": "https://untrusted.example"},
            json={"error": {"message": "sensitive-provider-secret-123"}},
        )

    provider = GeminiEmbedder(
        settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(EmbeddingFailure) as error:
            await provider.embed(("Synthetic policy",))
        assert error.value.code == code
        assert "sensitive-provider" not in str(error.value) + caplog.text
        assert "synthetic-not-a-real-key" not in caplog.text
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "values",
    [vector(0, 0), [1, 0], vector(float("nan"), 1), vector(float("inf"), 0), vector(True, 0)],
)
async def test_http_invalid_vectors_rejected(values):
    def handler(_request):
        return httpx.Response(
            200,
            text=json.dumps({"embeddings": [{"values": values}]}),
            headers={"Content-Type": "application/json"},
        )

    provider = GeminiEmbedder(
        settings(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        with pytest.raises(EmbeddingFailure):
            await provider.embed(("Synthetic",))
    finally:
        await provider.close()
