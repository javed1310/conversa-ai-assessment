import asyncio

import pytest

from app.knowledge import KnowledgeFailure, RetrievalQuery
from app.rag_snapshots import RagSnapshots, SnapshotLoadFailure
from tests.test_knowledge import document, service


@pytest.mark.asyncio
async def test_warm_snapshot_and_publication(storage, monkeypatch):
    reader = service(storage, query_embedding_ttl_seconds=0)
    await reader.ingest(document())
    calls = 0
    original = reader._chunks

    async def chunks(record, raw=None):
        nonlocal calls
        calls += 1
        return await original(record, raw)

    monkeypatch.setattr(reader, "_chunks", chunks)
    query = RetrievalQuery(query="refund")
    first = await reader.rank_for_calibration(query)
    assert await reader.rank_for_calibration(query) == first
    assert calls == 1
    assert reader.provider.tasks.count("query") == 2
    await reader.ingest(document(version=2, content="Refund within ten days."))
    latest = await reader.rank_for_calibration(query)
    assert latest[0].source.document_version == 2
    assert latest[0].text == "Refund within ten days."


@pytest.mark.asyncio
async def test_corruption_revalidated_after_invalidation(storage):
    reader = service(storage)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    await reader.rank_for_calibration(query)
    await reader.db.chunks.update_one(reader.scope, {"$set": {"text": "tampered"}})
    reader.snapshots.invalidate(reader.namespace)
    with pytest.raises(KnowledgeFailure, match="Knowledge operation"):
        await reader.rank_for_calibration(query)
    assert reader.snapshots.stats()["entries"] == 0


@pytest.mark.asyncio
async def test_singleflight_waiter_cancellation_and_epoch():
    cache = RagSnapshots()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ()

    owner = asyncio.create_task(cache.load(("k",), ("a", "b"), 0, 30, loader))
    await entered.wait()
    waiter = asyncio.create_task(cache.load(("k",), ("a", "b"), 0, 30, loader))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    cache.invalidate(("a", "b"))
    release.set()
    assert await owner == ()
    assert calls == 1
    assert cache.stats()["entries"] == 0
    assert cache.stats()["inflightKeys"] == 0


@pytest.mark.asyncio
async def test_owner_cancel_releases_waiters():
    cache = RagSnapshots()
    entered = asyncio.Event()

    async def loader():
        entered.set()
        await asyncio.Event().wait()
        return ()

    owner = asyncio.create_task(cache.load(("k",), ("a", "b"), 0, 30, loader))
    await entered.wait()
    waiter = asyncio.create_task(cache.load(("k",), ("a", "b"), 0, 30, loader))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(SnapshotLoadFailure):
        await waiter
    assert cache.stats()["inflightKeys"] == 0


@pytest.mark.asyncio
async def test_lru_expiry_and_disabled_reuse(monkeypatch):
    import app.rag_snapshots as module

    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    cache = RagSnapshots(max_entries=1)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return ()

    await cache.load(("a",), ("a", "b"), 0, 1, loader)
    await cache.load(("a",), ("a", "b"), 0, 1, loader)
    assert calls == 1
    now[0] = 2
    await cache.load(("a",), ("a", "b"), 0, 1, loader)
    await cache.load(("b",), ("a", "b"), 0, 1, loader)
    assert cache.stats()["entries"] == 1
    await cache.load(("b",), ("a", "b"), 0, 0, loader)
    assert calls == 4


@pytest.mark.asyncio
async def test_bounds_and_failure_not_cached():
    from app.contracts import SourceRef
    from app.rag_snapshots import Candidate

    cache = RagSnapshots(max_values=1, max_text_bytes=1)
    candidate = Candidate(
        source=SourceRef(
            business_id="training-institute",
            knowledge_base_id="institute-kb",
            document_id="policy",
            document_version=1,
            chunk_id="chunk-1",
            title="Policy",
            excerpt="Refund",
        ),
        text="Refund",
        vector=(1.0, 0.0),
    )

    async def oversized():
        return (candidate,)

    assert await cache.load(("a",), ("a", "b"), 0, 30, oversized) == (candidate,)
    assert cache.stats()["entries"] == 0

    async def broken():
        raise ValueError("invalid corpus")

    with pytest.raises(ValueError):
        await cache.load(("a",), ("a", "b"), 0, 30, broken)
    assert cache.stats()["inflightKeys"] == 0
    assert cache.stats()["entries"] == 0


@pytest.mark.asyncio
async def test_ttl_zero_detects_direct_tampering(storage):
    reader = service(storage, rag_snapshot_ttl_seconds=0)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    await reader.rank_for_calibration(query)
    await reader.db.chunks.update_one(reader.scope, {"$set": {"text": "tampered"}})
    with pytest.raises(KnowledgeFailure):
        await reader.rank_for_calibration(query)


@pytest.mark.asyncio
async def test_new_service_reuses_cache_and_business_isolated(storage, monkeypatch):
    first = service(storage)
    await first.ingest(document())
    query = RetrievalQuery(query="refund")
    await first.rank_for_calibration(query)
    second = service(storage)

    async def forbidden(record, raw=None):
        raise AssertionError("warm snapshot must avoid full chunk reads")

    monkeypatch.setattr(second, "_chunks", forbidden)
    assert (await second.rank_for_calibration(query))[0].source.business_id == "training-institute"
    foreign = service(storage, business_id="fitness-studio")
    assert await foreign.rank_for_calibration(query) == ()


@pytest.mark.asyncio
async def test_warm_snapshot_still_checks_cap_and_profile(storage):
    reader = service(storage)
    await reader.ingest(document("policy"))
    await reader.ingest(document("other"))
    query = RetrievalQuery(query="refund")
    assert len(await reader.rank_for_calibration(query)) == 2
    smaller = service(storage, rag_max_chunks=1)
    with pytest.raises(KnowledgeFailure) as error:
        await smaller.rank_for_calibration(query)
    assert error.value.code == "CORPUS_LIMIT"
    from tests.test_knowledge import TestOnlyEmbedder

    provider = TestOnlyEmbedder()
    provider.profile = "different-profile"
    isolated = service(storage, provider=provider)
    assert await isolated.rank_for_calibration(query) == ()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_failed_publication_preserves_warm_evidence(storage):
    from app.embeddings import EmbeddingFailure

    reader = service(storage)
    await reader.ingest(document())
    query = RetrievalQuery(query="refund")
    first = await reader.rank_for_calibration(query)
    reader.provider.fail_after = len(reader.provider.calls)
    with pytest.raises(EmbeddingFailure):
        await reader.ingest(document(version=2, content="Refund within ten days."))
    reader.provider.fail_after = None
    assert await reader.rank_for_calibration(query) == first
    assert reader.snapshots.stats()["entries"] == 1


@pytest.mark.asyncio
async def test_cold_reads_are_batched_and_warm_reads_skip_payload(storage, monkeypatch):
    from pymongo.asynchronous.collection import AsyncCollection

    reader = service(storage)
    for index in range(4):
        await reader.ingest(document(f"policy-{index}"))
    seen = []
    original = AsyncCollection.find

    def find(collection, query=None, *args, **kwargs):
        if collection.name in {"documents", "chunks"}:
            seen.append((collection.name, query))
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(AsyncCollection, "find", find)
    query = RetrievalQuery(query="refund")
    first = await reader.rank_for_calibration(query)
    assert len(first) == 4
    assert [name for name, _ in seen] == ["documents", "chunks"]
    for _, selection in seen:
        assert selection["business_id"] == reader.business.business_id
        assert selection["knowledge_base_id"] == reader.business.knowledge_base_id
        assert len(selection["$or"]) == 4
    seen.clear()
    assert await reader.rank_for_calibration(query) == first
    assert seen == []
