"""Gemini REST embeddings with explicit retrieval tasks and normalized vectors."""

import math
from typing import Any, Literal, Protocol

import httpx

from app.chunking import MAX_EMBEDDING_BYTES
from app.measurements import count, stage, usage
from app.provider_control import ProviderControl, ProviderControlFailure
from app.settings import Settings

Vector = tuple[float, ...]


class EmbeddingFailure(RuntimeError):
    def __init__(self, code: str, retryable: bool = False) -> None:
        super().__init__("Embedding operation unavailable.")
        self.code = code
        self.retryable = retryable


def validate_vector(value: object, dimensions: int) -> Vector:
    if not isinstance(value, (tuple, list)) or len(value) != dimensions:
        raise EmbeddingFailure("INVALID_RESPONSE")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in value
    ):
        raise EmbeddingFailure("INVALID_RESPONSE")
    vector = tuple(float(item) for item in value)
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingFailure("INVALID_RESPONSE")
    return vector


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def profile(self) -> str: ...

    async def embed(
        self, texts: tuple[str, ...], *, task: Literal["document", "query"] = "document"
    ) -> tuple[Vector, ...]: ...


class GeminiEmbedder:
    profile = "gemini-retrieval-document-query-l2-v1"

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        provider_control: ProviderControl | None = None,
    ) -> None:
        self.settings = settings
        self.control = provider_control or ProviderControl(settings)
        self.model = settings.gemini_embedding_model
        self.dimensions = settings.embedding_dimensions
        self._key = settings.gemini_api_key
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False
        )

    async def close(self) -> None:
        await self._client.aclose()

    @stage("embedding")
    async def embed(
        self, texts: tuple[str, ...], *, task: Literal["document", "query"] = "document"
    ) -> tuple[Vector, ...]:
        if (
            not texts
            or len(texts) > 32
            or task not in {"document", "query"}
            or any(not text.strip() or len(text.encode()) > MAX_EMBEDDING_BYTES for text in texts)
        ):
            raise EmbeddingFailure("INVALID_INPUT")
        if self._key is None or not self._key.get_secret_value().strip():
            raise EmbeddingFailure("MISSING_ACCESS")
        task_type = "RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY"
        count("embedding_calls")
        count("embedding_texts", len(texts))
        requests = [
            {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": self.dimensions,
            }
            for text in texts
        ]
        response = await self._request(requests, task)
        try:
            body = response.json()
            if not isinstance(body, dict) or "error" in body:
                raise EmbeddingFailure("INVALID_RESPONSE")
            usage(body, "embedding")
            data = body.get("embeddings")
            if not isinstance(data, list) or len(data) != len(texts):
                raise EmbeddingFailure("INVALID_RESPONSE")
            # Gemini's batch response is ordered; unlike OpenAI it has no echoed model/index.
            vectors = []
            for item in data:
                if not isinstance(item, dict):
                    raise EmbeddingFailure("INVALID_RESPONSE")
                vector = validate_vector(item.get("values"), self.dimensions)
                norm = math.hypot(*vector)
                vectors.append(tuple(value / norm for value in vector))
            return tuple(vectors)
        except (ValueError, TypeError):
            raise EmbeddingFailure("INVALID_RESPONSE") from None

    async def _request(
        self, requests: list[dict[str, Any]], task: Literal["document", "query"]
    ) -> httpx.Response:
        assert self._key is not None
        try:
            async with self.control.call(
                "chat" if task == "query" else "background",
                self.settings.provider_embedding_seconds,
            ):
                for attempt in range(self.settings.provider_attempts):
                    response = None
                    try:
                        response = await self.control.send(
                            self._client,
                            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:batchEmbedContents",
                            headers={"x-goog-api-key": self._key.get_secret_value()},
                            payload={"requests": requests},
                            lane="chat" if task == "query" else "background",
                            attempt_seconds=self.settings.provider_embedding_attempt_seconds,
                            kind="embedding",
                        )
                    except (httpx.TimeoutException, TimeoutError):
                        failure = EmbeddingFailure("TIMEOUT", True)
                    except httpx.RequestError:
                        failure = EmbeddingFailure("UNAVAILABLE", True)
                    else:
                        if response.status_code == 200:
                            return response
                        if response.status_code in {401, 403}:
                            raise EmbeddingFailure("ACCESS_DENIED")
                        if response.status_code == 429:
                            failure = EmbeddingFailure("RATE_LIMIT", True)
                        elif response.status_code >= 500:
                            failure = EmbeddingFailure("UNAVAILABLE", True)
                        else:
                            raise EmbeddingFailure("INVALID_REQUEST")
                    if attempt + 1 == self.settings.provider_attempts:
                        raise failure
                    if not await self.control.backoff(attempt, response):
                        raise failure
        except ProviderControlFailure as error:
            raise EmbeddingFailure(error.code, True) from None
        raise EmbeddingFailure("UNAVAILABLE", True)
