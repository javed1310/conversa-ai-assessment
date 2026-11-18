import asyncio

import pytest

from app.embeddings import EmbeddingFailure
from app.knowledge import RetrievalQuery
from app.query_embeddings import QueryEmbeddings
from tests.test_knowledge import document, service


@pytest.mark.asyncio
async def test_exact_queries_reuse_vectors_but_publication_stays_current(storage):
    reader = service(storage)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    first = await reader.rank_for_calibration(query)
    assert await service(storage, provider=reader.provider).rank_for_calibration(query) == first
    assert reader.provider.tasks.count("query") == 1
    await reader.ingest(document(version=2, content="Refund within ten days."))
    assert (await reader.rank_for_calibration(query))[0].source.document_version == 2
    assert reader.provider.tasks.count("query") == 1
    await reader.rank_for_calibration(RetrievalQuery(query="Refund"))
    assert reader.provider.tasks.count("query") == 2


@pytest.mark.asyncio
async def test_disabled_reuse_equivalent_and_scope_isolated(storage):
    reader = service(storage)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    cached = await reader.rank_for_calibration(query)
    disabled = service(storage, provider=reader.provider, query_embedding_ttl_seconds=0)
    assert await disabled.rank_for_calibration(query) == cached
    assert await disabled.rank_for_calibration(query) == cached
    assert reader.provider.tasks.count("query") == 3
    foreign = service(storage, provider=reader.provider, business_id="fitness-studio")
    await foreign.ingest(document())
    await foreign.rank_for_calibration(query)
    assert reader.provider.tasks.count("query") == 4


@pytest.mark.asyncio
async def test_lru_ttl_geometry_and_private_digest(monkeypatch):
    import app.query_embeddings as module

    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    cache = QueryEmbeddings(max_entries=2, max_values=4)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return (1.0, 0.0)

    async def get(key):
        return await cache.load((key,), dimensions=2, ttl=1, loader=loader)

    await get("a")
    await get("b")
    await get("a")
    await get("c")
    await get("b")
    assert calls == 4
    assert cache.stats()["entries"] == 2
    assert cache.stats()["vectorValues"] == 4
    now[0] = 2
    await get("b")
    assert calls == 5
    digest = cache.query_digest("private customer text")
    assert digest == cache.query_digest("private customer text")
    assert digest != QueryEmbeddings().query_digest("private customer text")
    assert "private customer text" not in repr(cache.__dict__)


@pytest.mark.parametrize(
    "vector", [(0.0, 0.0), (1.0,), (True, 1), (float("nan"), 1), (float("inf"), 1)]
)
@pytest.mark.asyncio
async def test_invalid_vectors_never_cached(vector):
    cache = QueryEmbeddings()

    async def loader():
        return vector

    for _ in range(2):
        with pytest.raises(EmbeddingFailure):
            await cache.load(("k",), dimensions=2, ttl=30, loader=loader)
    assert cache.stats() == {"entries": 0, "vectorValues": 0, "inflightKeys": 0}


@pytest.mark.asyncio
async def test_shared_success_and_waiter_cancel():
    cache = QueryEmbeddings()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return (1.0, 0.0)

    def request():
        return asyncio.create_task(cache.load(("k",), dimensions=2, ttl=30, loader=loader))

    owner = request()
    await entered.wait()
    waiter, cancelled = request(), request()
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    assert await owner == await waiter == (1.0, 0.0)
    assert calls == 1
    assert cache.stats()["inflightKeys"] == 0


@pytest.mark.asyncio
async def test_owner_cancel_and_retry():
    cache = QueryEmbeddings()
    entered = asyncio.Event()

    async def loader():
        entered.set()
        await asyncio.Event().wait()
        return (1.0, 0.0)

    owner = asyncio.create_task(cache.load(("k",), dimensions=2, ttl=30, loader=loader))
    await entered.wait()
    waiter = asyncio.create_task(cache.load(("k",), dimensions=2, ttl=30, loader=loader))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(EmbeddingFailure):
        await waiter
    assert cache.stats()["entries"] == cache.stats()["inflightKeys"] == 0


@pytest.mark.asyncio
async def test_oversized_and_failure_bypass():
    cache = QueryEmbeddings(max_values=1)

    async def loader():
        return (1.0, 0.0)

    assert await cache.load(("k",), dimensions=2, ttl=30, loader=loader) == (1.0, 0.0)
    assert cache.stats()["entries"] == 0

    async def broken():
        raise EmbeddingFailure("RATE_LIMIT", True)

    for _ in range(2):
        with pytest.raises(EmbeddingFailure):
            await cache.load(("k",), dimensions=2, ttl=30, loader=broken)
    assert cache.stats()["inflightKeys"] == 0


@pytest.mark.asyncio
async def test_new_profile_uses_new_query_embedding(storage):
    reader = service(storage)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    await reader.rank_for_calibration(query)
    reader.provider.profile = "test-only-profile-v2"
    changed = service(storage, provider=reader.provider)
    await changed.ingest(document(version=2))
    assert (await changed.rank_for_calibration(query))[0].source.document_version == 2
    assert reader.provider.tasks.count("query") == 2


@pytest.mark.asyncio
async def test_inflight_registry_overflow_bypasses_without_retention():
    cache = QueryEmbeddings(max_inflight=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked():
        entered.set()
        await release.wait()
        return (1.0, 0.0)

    owner = asyncio.create_task(cache.load(("first",), dimensions=2, ttl=30, loader=blocked))
    await entered.wait()

    async def quick():
        return (0.0, 1.0)

    assert await cache.load(("second",), dimensions=2, ttl=30, loader=quick) == (0.0, 1.0)
    assert cache.stats()["entries"] == 0
    assert cache.stats()["inflightKeys"] == 1
    release.set()
    await owner
    assert cache.stats()["entries"] == 1
