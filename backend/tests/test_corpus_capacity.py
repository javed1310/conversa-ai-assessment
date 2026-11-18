"""Real Mongo regressions for active-corpus admission, not synthetic retrieval quality."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.chunking import chunk_text
from app.embeddings import EmbeddingFailure
from app.knowledge import KnowledgeFailure, RetrievalQuery
from app.main import create_app
from app.settings import Settings
from tests.test_knowledge import TestOnlyEmbedder, document, service

pytestmark = pytest.mark.integration


async def test_overflow_is_rejected_before_embedding_without_breaking_ready_corpus(storage):
    provider = TestOnlyEmbedder()
    kb = service(storage, provider, rag_max_chunks=2)
    await kb.ingest(document("first"))
    await kb.ingest(document("second"))
    before = len(provider.calls)
    with pytest.raises(KnowledgeFailure, match="Knowledge operation") as error:
        await kb.ingest(document("overflow"))
    assert error.value.code == "CORPUS_LIMIT"
    assert len(provider.calls) == before
    rejected = await kb.document("overflow", 1)
    assert rejected.status == "failed" and rejected.failure_code == "CORPUS_LIMIT"
    result = await kb.retrieve(RetrievalQuery(query="refund"))
    assert {match.source.document_id for match in result.matches} == {"first", "second"}


async def test_concurrent_service_instances_cannot_both_publish_last_capacity_slot(storage):
    class BarrierEmbedder(TestOnlyEmbedder):
        def __init__(self):
            super().__init__()
            self.arrivals = 0
            self.both_arrived = asyncio.Event()

        async def embed(self, texts, *, task="document"):
            if task == "document":
                self.arrivals += 1
                if self.arrivals == 2:
                    self.both_arrived.set()
                await asyncio.wait_for(self.both_arrived.wait(), 3)
            return await super().embed(texts, task=task)

    provider = BarrierEmbedder()
    first = service(storage, provider, rag_max_chunks=1)
    second = service(storage, provider, rag_max_chunks=1)
    results = await asyncio.gather(
        first.ingest(document("first")), second.ingest(document("second")), return_exceptions=True
    )
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    failures = [item for item in results if isinstance(item, KnowledgeFailure)]
    assert len(failures) == 1 and failures[0].code == "CORPUS_LIMIT"
    assert await storage.db.documents.count_documents({"status": "ready"}) == 1
    assert await storage.db.documents.count_documents({"status": "failed"}) == 1
    assert (await first.retrieve(RetrievalQuery(query="refund"))).status == "found"


async def test_replacement_counts_active_version_once_and_historical_retry_is_free(storage):
    provider = TestOnlyEmbedder()
    kb = service(storage, provider, rag_max_chunks=1)
    await kb.ingest(document(content="Refund old policy"))
    await kb.ingest(document(content="Refund new policy", version=2))
    before = len(provider.calls)
    assert (await kb.ingest(document(content="Refund old policy"))).status == "ready"
    assert len(provider.calls) == before
    result = await kb.retrieve(RetrievalQuery(query="refund"))
    assert {match.source.document_version for match in result.matches} == {2}


async def test_growing_replacement_preserves_prior_ready_version_and_smaller_version_can_publish(
    storage,
):
    kb = service(storage, rag_max_chunks=2, chunk_tokens=8, chunk_overlap=0)
    await kb.ingest(document("policy", "Refund policy."))
    await kb.ingest(document("other", "Refund terms."))
    larger = "Refund policy applies to every learner without exception."
    assert len(chunk_text(larger, 8, 0)) == 2
    with pytest.raises(KnowledgeFailure) as error:
        await kb.ingest(document("policy", larger, version=2))
    assert error.value.code == "CORPUS_LIMIT"
    result = await kb.retrieve(RetrievalQuery(query="refund"))
    assert {
        m.source.document_version for m in result.matches if m.source.document_id == "policy"
    } == {1}
    await kb.ingest(document("policy", "Refund updated.", version=3))
    result = await kb.retrieve(RetrievalQuery(query="refund"))
    assert {
        m.source.document_version for m in result.matches if m.source.document_id == "policy"
    } == {3}


async def test_older_late_publication_does_not_displace_or_charge_newer_active_version(storage):
    kb = service(storage, rag_max_chunks=1)
    await kb.ingest(document(content="Refund current policy", version=3))
    await kb.ingest(document(content="Refund historical policy", version=2))
    result = await kb.retrieve(RetrievalQuery(query="refund"))
    assert {match.source.document_version for match in result.matches} == {3}


async def test_capacity_is_scoped_to_business_and_knowledge_base(storage):
    institute = service(storage, rag_max_chunks=1)
    fitness = service(storage, business_id="fitness-studio", rag_max_chunks=1)
    await asyncio.gather(
        institute.ingest(document("institute")),
        fitness.ingest(document("fitness", "Fitness trial policy.")),
    )
    assert (await institute.retrieve(RetrievalQuery(query="refund"))).status == "found"
    assert (await fitness.retrieve(RetrievalQuery(query="fitness"))).status == "found"


async def test_failed_embedding_does_not_reserve_corpus_capacity(storage):
    provider = TestOnlyEmbedder()
    provider.fail_after = 0
    kb = service(storage, provider, rag_max_chunks=1)
    with pytest.raises(EmbeddingFailure):
        await kb.ingest(document("failed"))
    provider.fail_after = None
    await kb.ingest(document("good"))
    assert (await kb.retrieve(RetrievalQuery(query="refund"))).status == "found"


async def test_rejected_publication_retry_reuses_prepared_chunks(storage):
    class Delayed(TestOnlyEmbedder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts, *, task="document"):
            if task == "document":
                self.started.set()
                await self.release.wait()
            return await super().embed(texts, task=task)

    provider = Delayed()
    slow = service(storage, provider, rag_max_chunks=1)
    pending = asyncio.create_task(slow.ingest(document("delayed", "Refund prepared policy.")))
    await asyncio.wait_for(provider.started.wait(), 3)
    fast = service(storage, rag_max_chunks=1)
    try:
        await fast.ingest(document("winner", "Refund active policy."))
    finally:
        provider.release.set()
    with pytest.raises(KnowledgeFailure) as error:
        await pending
    assert error.value.code == "CORPUS_LIMIT"
    assert await storage.db.chunks.count_documents({"document_id": "delayed"}) == 1
    # Capacity is made available by switching the winner's latest version to another profile.
    # No production removal/update API is invented for the purpose of the test.
    other = TestOnlyEmbedder()
    other.profile = "test-only-other-profile"
    await service(storage, other, rag_max_chunks=1).ingest(
        document("winner", "Refund other profile.", 2)
    )
    before = len(provider.calls)
    assert (await slow.ingest(document("delayed", "Refund prepared policy."))).status == "ready"
    assert len(provider.calls) == before
    result = await slow.retrieve(RetrievalQuery(query="refund"))
    assert {m.source.document_id for m in result.matches} == {"delayed"}


async def test_cancelled_ingestion_does_not_block_readers_or_other_publishers(storage):
    started = asyncio.Event()

    class Waiting(TestOnlyEmbedder):
        async def embed(self, texts, *, task="document"):
            started.set()
            await asyncio.Event().wait()

    pending = asyncio.create_task(
        service(storage, Waiting(), rag_max_chunks=1).ingest(document("cancelled"))
    )
    await asyncio.wait_for(started.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    good = service(storage, rag_max_chunks=1)
    assert (await asyncio.wait_for(good.ingest(document("good")), 3)).status == "ready"
    assert (await good.retrieve(RetrievalQuery(query="refund"))).status == "found"


async def test_http_capacity_error_is_clear_non_retryable_and_preserves_ready_data(
    storage, monkeypatch
):
    @asynccontextmanager
    async def use_storage(_settings):
        yield storage

    monkeypatch.setattr("app.main.open_database", use_storage)
    provider = TestOnlyEmbedder()
    settings = Settings(
        _env_file=None,
        rag_max_chunks=1,
        rag_min_score=0.8,
        ai_chat_enabled=False,
        automation_enabled=False,
    )
    app = create_app(settings, embedding_provider=provider)
    path = "/api/v1/businesses/training-institute/documents"
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://capacity-test"
        ) as client,
    ):
        assert (
            await client.post(path, json=document("first").model_dump(by_alias=True))
        ).status_code == 200
        response = await client.post(path, json=document("overflow").model_dump(by_alias=True))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CORPUS_LIMIT"
        assert response.json()["error"]["retryable"] is False
        first = await client.get(path + "/first/versions/1")
        overflow = await client.get(path + "/overflow/versions/1")
        assert first.json()["status"] == "ready"
        assert overflow.json()["status"] == "failed"
        assert overflow.json()["failureCode"] == "CORPUS_LIMIT"
