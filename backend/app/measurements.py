"""Bounded process-local numeric measurements; never retain request/provider payloads."""

import asyncio
import copy
from collections import deque
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter
from typing import Any, Literal

Operation = Literal["chat", "ingestion", "worker", "worker_batch", "probe"]
STAGES = frozenset(
    {
        "state",
        "dialogue",
        "context",
        "generation",
        "tool",
        "reply",
        "rag",
        "ranking",
        "embedding",
        "worker_claim",
        "summary",
        "notification",
        "persistence",
        "state_proposal",
        "reply_selection",
        "reply_review",
        "planner",
        "summary_plan",
        "summary_store",
        "chunk_read_validate",
        "corpus_snapshot",
        "corpus_admission",
        "provider_queue",
        "provider_http",
        "provider_backoff",
        "rag_snapshot_load",
    }
)
COUNTERS = frozenset(
    {
        "generation_calls",
        "generation_http_attempts",
        "embedding_calls",
        "embedding_http_attempts",
        "embedding_texts",
        "turn_reads",
        "state_calls",
        "literal_catalogue_state_hits",
        "reply_calls",
        "summary_calls",
        "planner_calls",
        "other_generation_calls",
        "worker_jobs",
        "worker_job_age_ms",
        "worker_batch_jobs",
        "worker_batch_idle",
        "worker_batch_job_limit",
        "worker_batch_time_limit",
        "worker_batch_stopped",
        "worker_idle_polls",
        "provider_queue_rejections",
        "provider_call_rejections",
        "provider_deadline_rejections",
        "provider_retries",
        "provider_connect_timeouts",
        "provider_read_timeouts",
        "provider_write_timeouts",
        "provider_pool_timeouts",
        "provider_other_timeouts",
        "provider_attempt_deadlines",
        "provider_network_errors",
        "provider_http_rate_limits",
        "provider_http_server_errors",
        "provider_retry_skipped",
        "query_embedding_cache_hits",
        "query_embedding_cache_misses",
        "query_embedding_cache_shared_loads",
        "query_embedding_cache_bypass",
        "query_embedding_cache_evictions",
        "rag_snapshot_hits",
        "rag_snapshot_misses",
        "rag_snapshot_shared_loads",
        "rag_snapshot_bypass",
        "rag_snapshot_evictions",
        "rag_snapshot_invalidations",
        "rag_snapshot_stale_builds",
        "conversational_reply_deadline_fallback",
        "conversational_reply_rejected",
        "conversational_reply_provider_fallback",
        "conversational_reply_accepted",
        "conversational_reply_invalid_draft",
    }
)
TOKEN_FIELDS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "totalTokenCount",
    "cachedContentTokenCount",
    "thoughtsTokenCount",
    "toolUsePromptTokenCount",
)


@dataclass
class Frame:
    counters: dict[str, int] = field(default_factory=dict)
    stages: dict[str, dict[str, float | int]] = field(default_factory=dict)
    closed: bool = False


_current: ContextVar[Frame | None] = ContextVar("numeric_measurements", default=None)


def count(name: str, value: int = 1) -> None:
    frame = _current.get()
    if (
        frame is not None
        and not frame.closed
        and name in COUNTERS
        and type(value) is int
        and value >= 0
    ):
        frame.counters[name] = frame.counters.get(name, 0) + value


def usage(body: dict[str, Any], kind: Literal["generation", "embedding"]) -> None:
    frame = _current.get()
    if frame is None or frame.closed or kind not in {"generation", "embedding"}:
        return
    metadata = body.get("usageMetadata")
    valid = {
        name: metadata[name]
        for name in TOKEN_FIELDS
        if isinstance(metadata, dict)
        and type(metadata.get(name)) is int
        and 0 <= metadata[name] <= 1_000_000_000
    }
    coverage = kind + ("_responses_with_usage" if valid else "_responses_without_usage")
    frame.counters[coverage] = frame.counters.get(coverage, 0) + 1
    for name, value in valid.items():
        key = kind + "_" + name
        frame.counters[key] = frame.counters.get(key, 0) + value
        reported = key + "_reports"
        frame.counters[reported] = frame.counters.get(reported, 0) + 1


class Measurements:
    """A lifespan-owned bounded numeric ring; not durable logs."""

    def __init__(self, capacity: int = 128) -> None:
        if not 1 <= capacity <= 1024:
            raise ValueError("Measurement capacity must be between 1 and 1024")
        self._recent: deque[dict[str, Any]] = deque(maxlen=capacity)

    @contextmanager
    def operation(self, name: Operation) -> Iterator[None]:
        if name not in {"chat", "ingestion", "worker", "worker_batch", "probe"}:
            raise ValueError("Unknown measurement operation")
        frame = Frame()
        token = _current.set(frame)
        started = perf_counter()
        outcome = "returned"
        try:
            yield
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "raised"
            raise
        finally:
            frame.closed = True
            _current.reset(token)
            self._recent.append(
                {
                    "schemaVersion": 1,
                    "operation": name,
                    "outcome": outcome,
                    "durationMs": round(max(0, perf_counter() - started) * 1000, 3),
                    "stages": frame.stages,
                    "counters": frame.counters,
                }
            )

    def recent(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._recent))


def operation[**P, R](
    name: Operation,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            recorder = getattr(args[0], "measurements", None) if args else None
            if not isinstance(recorder, Measurements):
                return await function(*args, **kwargs)
            with recorder.operation(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def stage[**P, R](
    name: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
    if name not in STAGES:
        raise ValueError("Unknown measurement stage")

    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            frame = _current.get()
            if frame is None or frame.closed:
                return await function(*args, **kwargs)
            started = perf_counter()
            outcome = "returned"
            try:
                return await function(*args, **kwargs)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except BaseException:
                outcome = "raised"
                raise
            finally:
                if not frame.closed:
                    record = frame.stages.setdefault(
                        name,
                        {"calls": 0, "returned": 0, "raised": 0, "cancelled": 0, "durationMs": 0.0},
                    )
                    record["calls"] += 1
                    record[outcome] += 1
                    record["durationMs"] += max(0, perf_counter() - started) * 1000

        return wrapped

    return decorate
