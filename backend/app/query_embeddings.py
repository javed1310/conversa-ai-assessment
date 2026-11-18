"""Lifespan-local bounded reuse of validated query vectors; no raw query retention."""

import asyncio
import hashlib
import hmac
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from time import monotonic

from app.embeddings import EmbeddingFailure, Vector, validate_vector
from app.measurements import count

Key = tuple[str, ...]


class QueryEmbeddings:
    def __init__(self, max_entries: int = 96, max_values: int = 147456, max_inflight: int = 16):
        if min(max_entries, max_values, max_inflight) < 1:
            raise ValueError("Query cache bounds must be positive")
        self.max_entries = max_entries
        self.max_values = max_values
        self.max_inflight = max_inflight
        self._secret = secrets.token_bytes(32)
        self._entries: OrderedDict[Key, tuple[Vector, float]] = OrderedDict()
        self._loading: dict[Key, asyncio.Future[Vector]] = {}
        self._values = 0

    def query_digest(self, query: str) -> str:
        return hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "vectorValues": self._values,
            "inflightKeys": len(self._loading),
        }

    def _remove(self, key: Key) -> None:
        vector, _ = self._entries.pop(key)
        self._values -= len(vector)

    async def load(
        self, key: Key, *, dimensions: int, ttl: float, loader: Callable[[], Awaitable[Vector]]
    ) -> Vector:
        async def validated() -> Vector:
            return validate_vector(await loader(), dimensions)

        if ttl <= 0:
            count("query_embedding_cache_bypass")
            return await validated()
        entry = self._entries.get(key)
        if entry is not None:
            if entry[1] > monotonic():
                self._entries.move_to_end(key)
                count("query_embedding_cache_hits")
                return entry[0]
            self._remove(key)
        waiting = self._loading.get(key)
        if waiting is not None:
            count("query_embedding_cache_shared_loads")
            return await asyncio.shield(waiting)
        if len(self._loading) >= self.max_inflight:
            count("query_embedding_cache_bypass")
            return await validated()
        count("query_embedding_cache_misses")
        future: asyncio.Future[Vector] = asyncio.get_running_loop().create_future()
        self._loading[key] = future
        try:
            vector = await validated()
            if len(vector) <= self.max_values:
                while self._entries and (
                    len(self._entries) >= self.max_entries
                    or self._values + len(vector) > self.max_values
                ):
                    self._remove(next(iter(self._entries)))
                    count("query_embedding_cache_evictions")
                self._entries[key] = (vector, monotonic() + ttl)
                self._values += len(vector)
            else:
                count("query_embedding_cache_bypass")
            future.set_result(vector)
            return vector
        except asyncio.CancelledError:
            future.set_exception(EmbeddingFailure("CANCELLED", True))
            future.exception()
            raise
        except Exception as error:
            future.set_exception(error)
            future.exception()
            raise
        finally:
            self._loading.pop(key, None)
