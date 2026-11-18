"""Lifespan-local provider admission, inherited work deadlines and bounded retry delays."""

import asyncio
import math
import random
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any, Literal

import httpx

from app.measurements import count, stage
from app.settings import Settings

Lane = Literal["chat", "background"]
_deadline: ContextVar[float | None] = ContextVar("provider_deadline", default=None)


class ProviderControlFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__("Provider admission or work budget unavailable.")
        self.code = code


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    parent = _deadline.get()
    until = monotonic() + max(0, seconds)
    token = _deadline.set(min(parent, until) if parent is not None else until)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float:
    until = _deadline.get()
    return max(0, until - monotonic()) if until is not None else math.inf


def budget(limit: float) -> float:
    available = min(limit, remaining())
    if available <= 0:
        count("provider_deadline_rejections")
        raise ProviderControlFailure("DEADLINE_EXCEEDED")
    return available


def can_start_write(seconds: float = 5) -> bool:
    if remaining() < seconds:
        count("provider_deadline_rejections")
        return False
    return True


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        # RFC delay-seconds is an unsigned integer, not arbitrary floats or NaN.
        if value.strip().isascii() and value.strip().isdigit():
            delay = float(value.strip())
        else:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return None
            delay = max(0, (parsed - datetime.now(UTC)).total_seconds())
        return delay if math.isfinite(delay) else math.inf if delay > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class ProviderControl:
    """Reserved lanes prevent cross-class slot starvation; not an RPM/TPM quota service."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._slots = {
            "chat": asyncio.Semaphore(settings.provider_chat_concurrency),
            "background": asyncio.Semaphore(settings.provider_background_concurrency),
        }
        self._waiting = {"chat": 0, "background": 0}
        self._active = {"chat": 0, "background": 0}
        self._calls = {"chat": 0, "background": 0}

    def logical_stats(self) -> dict[str, int]:
        return dict(self._calls)

    @asynccontextmanager
    async def call(self, lane: Lane, seconds: float) -> AsyncIterator[None]:
        capacity = (
            self.settings.provider_chat_concurrency
            if lane == "chat"
            else self.settings.provider_background_concurrency
        ) + self.settings.provider_queue_limit
        if self._calls[lane] >= capacity:
            count("provider_call_rejections")
            raise ProviderControlFailure("PROVIDER_BUSY")
        self._calls[lane] += 1
        try:
            with deadline(seconds):
                yield
        finally:
            self._calls[lane] -= 1

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            lane: {"active": self._active[lane], "waiting": self._waiting[lane]}
            for lane in ("chat", "background")
        }

    @stage("provider_queue")
    async def _acquire(self, lane: Lane) -> None:
        queued = self._slots[lane].locked()
        if queued and self._waiting[lane] >= self.settings.provider_queue_limit:
            count("provider_queue_rejections")
            raise ProviderControlFailure("PROVIDER_BUSY")
        if queued:
            self._waiting[lane] += 1
        try:
            async with asyncio.timeout(budget(self.settings.provider_queue_seconds)):
                await self._slots[lane].acquire()
        except TimeoutError:
            count("provider_queue_rejections")
            raise ProviderControlFailure(
                "DEADLINE_EXCEEDED" if remaining() <= 0 else "PROVIDER_BUSY"
            ) from None
        finally:
            if queued:
                self._waiting[lane] -= 1

    @asynccontextmanager
    async def slot(self, lane: Lane) -> AsyncIterator[None]:
        await self._acquire(lane)
        self._active[lane] += 1
        try:
            yield
        finally:
            self._active[lane] -= 1
            self._slots[lane].release()

    async def send(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        lane: Lane,
        attempt_seconds: float,
        kind: Literal["generation", "embedding"],
    ) -> httpx.Response:
        async with self.slot(lane):
            return await self._post(
                client,
                url,
                headers=headers,
                payload=payload,
                attempt_seconds=budget(attempt_seconds),
                kind=kind,
            )

    @stage("provider_http")
    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        attempt_seconds: float,
        kind: Literal["generation", "embedding"],
    ) -> httpx.Response:
        async with asyncio.timeout(attempt_seconds):
            count(kind + "_http_attempts")
            return await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(attempt_seconds, connect=min(5, attempt_seconds)),
                follow_redirects=False,
            )

    @stage("provider_backoff")
    async def backoff(self, attempt: int, response: httpx.Response | None) -> bool:
        cap = self.settings.provider_retry_max_seconds
        jitter = random.uniform(0, min(cap, self.settings.provider_retry_base_seconds * 2**attempt))
        requested = (
            retry_after(response.headers.get("Retry-After")) if response is not None else None
        )
        delay = max(jitter, requested or 0)
        if delay > cap or delay + 0.05 >= remaining():
            count("provider_retry_skipped")
            return False
        count("provider_retries")
        await asyncio.sleep(delay)
        return True
