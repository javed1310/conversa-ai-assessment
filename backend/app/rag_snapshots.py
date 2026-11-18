"""Bounded immutable corpus reuse; no customer queries, answers, sessions or action state."""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from app.contracts import SourceRef
from app.embeddings import Vector
from app.measurements import count

Namespace = tuple[str, str]
CacheKey = tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    source: SourceRef
    text: str
    vector: Vector


Snapshot = tuple[Candidate, ...]


@dataclass(frozen=True)
class Entry:
    namespace: Namespace
    snapshot: Snapshot
    expires: float
    values: int
    text_bytes: int


class SnapshotLoadFailure(RuntimeError):
    pass


class RagSnapshots:
    def __init__(
        self,
        max_entries: int = 8,
        max_values: int = 1_000_000,
        max_text_bytes: int = 2_000_000,
        max_inflight: int = 8,
    ):
        if min(max_entries, max_values, max_text_bytes, max_inflight) < 1:
            raise ValueError("Snapshot cache bounds must be positive")
        self.max_entries, self.max_values = max_entries, max_values
        self.max_text_bytes, self.max_inflight = max_text_bytes, max_inflight
        self.epoch = 0
        self._entries: OrderedDict[CacheKey, Entry] = OrderedDict()
        self._loading: dict[CacheKey, asyncio.Future[Snapshot]] = {}
        self._values, self._text_bytes = 0, 0

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "vectorValues": self._values,
            "textBytes": self._text_bytes,
            "inflightKeys": len(self._loading),
        }

    def _remove(self, key: CacheKey) -> None:
        entry = self._entries.pop(key)
        self._values -= entry.values
        self._text_bytes -= entry.text_bytes

    def invalidate(self, namespace: Namespace) -> None:
        self.epoch += 1
        for key, entry in list(self._entries.items()):
            if entry.namespace == namespace:
                self._remove(key)
        count("rag_snapshot_invalidations")

    def _put(self, key: CacheKey, namespace: Namespace, snapshot: Snapshot, ttl: float) -> None:
        values = sum(len(item.vector) for item in snapshot)
        text_bytes = sum(
            len(item.text.encode())
            + len(item.source.excerpt.encode())
            + len(item.source.title.encode())
            for item in snapshot
        )
        if values > self.max_values or text_bytes > self.max_text_bytes:
            count("rag_snapshot_bypass")
            return
        if key in self._entries:
            self._remove(key)
        while self._entries and (
            len(self._entries) >= self.max_entries
            or self._values + values > self.max_values
            or self._text_bytes + text_bytes > self.max_text_bytes
        ):
            self._remove(next(iter(self._entries)))
            count("rag_snapshot_evictions")
        self._entries[key] = Entry(namespace, snapshot, monotonic() + ttl, values, text_bytes)
        self._values += values
        self._text_bytes += text_bytes

    async def load(
        self,
        key: CacheKey,
        namespace: Namespace,
        epoch: int,
        ttl: float,
        loader: Callable[[], Awaitable[Snapshot]],
    ) -> Snapshot:
        if ttl <= 0:
            count("rag_snapshot_bypass")
            return await loader()
        entry = self._entries.get(key)
        if entry is not None:
            if entry.expires > monotonic():
                self._entries.move_to_end(key)
                count("rag_snapshot_hits")
                return entry.snapshot
            self._remove(key)
        waiting = self._loading.get(key)
        if waiting is not None:
            count("rag_snapshot_shared_loads")
            return await asyncio.shield(waiting)
        if len(self._loading) >= self.max_inflight:
            count("rag_snapshot_bypass")
            return await loader()
        count("rag_snapshot_misses")
        future: asyncio.Future[Snapshot] = asyncio.get_running_loop().create_future()
        self._loading[key] = future
        try:
            snapshot = await loader()
            if self.epoch == epoch:
                self._put(key, namespace, snapshot, ttl)
            else:
                count("rag_snapshot_stale_builds")
            future.set_result(snapshot)
            return snapshot
        except asyncio.CancelledError:
            future.set_exception(SnapshotLoadFailure("Snapshot build cancelled"))
            future.exception()
            raise
        except Exception as error:
            future.set_exception(error)
            future.exception()
            raise
        finally:
            self._loading.pop(key, None)
