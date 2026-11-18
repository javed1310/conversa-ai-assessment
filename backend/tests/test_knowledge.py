"""Offline pipeline mechanics only: all synthetic vectors are confined to disposable test DBs."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.businesses import BusinessRegistry
from app.chunking import chunk_text, token_count
from app.contracts import ExecutionContext
from app.embeddings import EmbeddingFailure, GeminiEmbedder, validate_vector
from app.knowledge import (
    DocumentInput,
    KnowledgeFailure,
    KnowledgeReader,
    KnowledgeService,
    RetrievalQuery,
    cosine,
)
from app.knowledge_cli import fixture_inputs
from app.repositories import RecordConflict, RecordNotFound, Repository
from app.settings import REPOSITORY_ROOT, Settings


class TestOnlyEmbedder:
    __test__ = False
    model = "test-only-vector-model"
    dimensions = 3
    profile = "test-only-fixture-v1"

    def __init__(self):
        self.calls = []
        self.tasks = []
        self.fail_after = None

    async def embed(self, texts, *, task="document"):
        self.calls.append(texts)
        self.tasks.append(task)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise EmbeddingFailure("UNAVAILABLE", True)

        # Deliberately not semantic-quality evidence; just deterministic mechanical fixtures.
        def vector(text):
            lower = text.lower()
            if "refund" in lower:
                return (1.0, 0.0, 0.0)
            if "python" in lower:
                return (0.0, 1.0, 0.0)
            if "fitness" in lower:
                return (0.0, 0.0, 1.0)
            return (-1.0, -1.0, -1.0)

        return tuple(vector(text) for text in texts)


def document(
    identifier="policy", content="Refund within seven days AND no first class attended.", version=1
):
    return DocumentInput(
        document_id=identifier,
        version=version,
        title="Synthetic policy",
        format="markdown",
        content=content,
    )


def service(storage, provider=None, business_id="training-institute", **options):
    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    business = registry.get(business_id)
    settings = Settings(_env_file=None, rag_min_score=0.8, **options)
    return KnowledgeService(storage, business, provider or TestOnlyEmbedder(), settings)


def test_five_fixtures_and_policy_authority():
    inputs = fixture_inputs(Settings(_env_file=None))
    assert len(inputs) == 5
    institute = {
        item.document_id: item.content for owner, item in inputs if owner == "training-institute"
    }
    assert set(institute) == {"course-overview", "eligibility", "refund", "faq"}
    assert "basic programming knowledge" in institute["eligibility"]
    assert "no programming prerequisites" in institute["eligibility"]
    assert "BOTH" in institute["refund"] and "AND" in institute["refund"]
    assert (
        "within seven days" in institute["refund"]
        and "not attended the first class" in institute["refund"]
    )
    assert "getCourseDetails" in institute["course-overview"]
    assert all(len(chunk_text(item.content)) == 1 for _, item in inputs)


@pytest.mark.parametrize(
    "change",
    [
        {"format": "pdf"},
        {"title": " "},
        {"content": " "},
        {"content": "x" * 64001},
        {"content": "emoji\x00invalid"},
        {"version": True},
        {"businessId": "other"},
        {"content": "🙂" * 40000},
    ],
)
def test_document_inputs_reject_invalid_or_oversized_values(change):
    with pytest.raises(ValidationError):
        DocumentInput.model_validate(document().model_dump() | change)


def test_chunker_preserves_unicode_offsets_overlap_and_determinism():
    text = "# Heading\n\n" + ("Python सीखें 🙂 Refund conditions remain together.\n\n" * 70)
    chunks = chunk_text(text, 40, 8)
    assert chunks == chunk_text(text, 40, 8)
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    rebuilt = text[: chunks[0].end]
    for before, current in zip(chunks[:-1], chunks[1:], strict=True):
        assert before.start < current.start <= before.end < current.end
        assert token_count(text[current.start : before.end]) <= 8
        rebuilt += text[before.end : current.end]
    assert rebuilt == text
    assert all(item.text == text[item.start : item.end] and item.tokens <= 40 for item in chunks)
    assert all("�" not in item.text for item in chunks)


@pytest.mark.parametrize(
    "vector", [[0, 0, 0], [1, 2], [True, 0, 0], [float("nan"), 0, 1], [float("inf"), 0, 0]]
)
def test_invalid_embedding_vectors_rejected(vector):
    with pytest.raises(EmbeddingFailure):
        validate_vector(vector, 3)


def test_cosine_is_numeric_and_dimension_checked():
    assert cosine((1.0, 0.0), (1.0, 0.0)) == 1
    assert cosine((1.0, 0.0), (0.0, 1.0)) == 0
    assert cosine((1e308, 0.0), (1e308, 0.0)) == 1
    with pytest.raises(KnowledgeFailure):
        cosine((1.0,), (1.0, 2.0))


async def test_missing_key_has_no_production_fallback():
    provider = GeminiEmbedder(Settings(_env_file=None))
    with pytest.raises(EmbeddingFailure) as error:
        await provider.embed(("Refund eligibility",))
    assert error.value.code == "MISSING_ACCESS"
    await provider.close()


async def test_openai_key_cannot_activate_gemini(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-legacy-key")
    provider = GeminiEmbedder(Settings(_env_file=None))
    try:
        with pytest.raises(EmbeddingFailure) as error:
            await provider.embed(("Refund eligibility",))
        assert error.value.code == "MISSING_ACCESS"
    finally:
        await provider.close()


def test_gemini_byte_bound_is_independent_of_chunk_token_heuristic():
    from app.chunking import MAX_EMBEDDING_BYTES

    chunks = chunk_text("x" * 5000)
    assert len(chunks) > 1
    assert all(len(chunk.text.encode()) <= MAX_EMBEDDING_BYTES for chunk in chunks)
    with pytest.raises(ValidationError):
        RetrievalQuery(query="🙂" * 401)


@pytest.mark.integration
class TestKnowledgeStorage:
    async def test_missing_real_access_fails_without_fabricated_chunks(self, storage):
        settings = Settings(_env_file=None)
        provider = GeminiEmbedder(settings)
        business = BusinessRegistry.load(REPOSITORY_ROOT / "businesses").get("training-institute")
        try:
            knowledge = KnowledgeService(storage, business, provider, settings)
            with pytest.raises(EmbeddingFailure) as error:
                await knowledge.ingest(document())
            assert error.value.code == "MISSING_ACCESS"
            assert (await knowledge.document("policy", 1)).status == "failed"
            assert await storage.db.chunks.count_documents({}) == 0
        finally:
            await provider.close()

    async def test_pending_scope_immutability_and_publish(self, storage):
        provider = TestOnlyEmbedder()
        institute = service(storage, provider)
        pending = await institute.register(document())
        assert pending.status == "pending" and pending.expected_chunks == 0
        assert (await institute.retrieve(RetrievalQuery(query="refund"))).status == "no_evidence"
        assert not provider.calls
        ready = await institute.ingest(document())
        assert ready.status == "ready" and ready.expected_chunks == 1
        assert (await institute.ingest(document())).status == "ready"
        assert len(provider.calls) == 1
        assert await storage.db.chunks.count_documents({}) == 1
        with pytest.raises(RecordConflict):
            await institute.register(document(content="Changed refund text"))
        with pytest.raises(RecordNotFound):
            await service(storage, business_id="fitness-studio").document("policy", 1)

    async def test_retrieved_sources_correspond_to_actual_owned_chunks(self, storage):
        tracked = TestOnlyEmbedder()
        institute = service(storage, tracked)
        fitness = service(storage, business_id="fitness-studio")
        await institute.ingest(document())
        await fitness.ingest(document("policy", "Fitness trial visit policy"))
        result = await institute.retrieve(RetrievalQuery(query="refund"))
        assert tracked.tasks == ["document", "query"]
        assert result.status == "found" and len(result.matches) == 1
        source = result.matches[0].source
        chunk = await storage.db.chunks.find_one(institute.scope | {"chunk_id": source.chunk_id})
        assert (
            source.business_id == "training-institute"
            and source.knowledge_base_id == "training-institute-kb"
        )
        assert source.document_version == 1 and source.excerpt in chunk["text"]
        assert result.matches[0].text == chunk["text"]
        foreign = await fitness.retrieve(RetrievalQuery(query="refund"))
        assert foreign.status == "no_evidence"

    async def test_partial_failure_is_invisible_then_retry_fills_without_duplicates(self, storage):
        provider = TestOnlyEmbedder()
        provider.fail_after = 1
        institute = service(storage, provider, chunk_tokens=20, chunk_overlap=0)
        input_doc = document(
            content=("Refund policy statement for synthetic pipeline testing.\n\n" * 80)
        )
        with pytest.raises(EmbeddingFailure):
            await institute.ingest(input_doc)
        assert (await institute.document("policy", 1)).status == "failed"
        assert await storage.db.chunks.count_documents({}) == 16
        assert not (await institute.retrieve(RetrievalQuery(query="refund"))).matches
        provider.fail_after = None
        saved = await institute.ingest(input_doc)
        assert saved.status == "ready"
        assert await storage.db.chunks.count_documents({}) == saved.expected_chunks
        identities = [item["chunk_id"] async for item in storage.db.chunks.find({})]
        assert len(identities) == len(set(identities))

    async def test_latest_ready_version_and_failed_new_version(self, storage):
        provider = TestOnlyEmbedder()
        institute = service(storage, provider)
        await institute.ingest(document(content="Refund old version"))
        provider.fail_after = 1
        with pytest.raises(EmbeddingFailure):
            await institute.ingest(document(content="Refund new version", version=2))
        provider.fail_after = None
        result = await institute.retrieve(RetrievalQuery(query="refund"))
        assert result.matches[0].source.document_version == 1
        await institute.ingest(document(content="Refund new version", version=2))
        result = await institute.retrieve(RetrievalQuery(query="refund"))
        assert {item.source.document_version for item in result.matches} == {2}

    async def test_mismatched_active_identity_never_compares_older_vectors(self, storage):
        institute = service(storage)
        await institute.ingest(document())
        other = TestOnlyEmbedder()
        other.model = "different-test-model"
        await service(storage, other).ingest(document(content="Refund v2", version=2))
        assert not (await institute.retrieve(RetrievalQuery(query="refund"))).matches

    async def test_different_task_profile_never_mixes_same_model_vectors(self, storage):
        institute = service(storage)
        await institute.ingest(document())
        other = TestOnlyEmbedder()
        other.profile = "incompatible-task-profile"
        await service(storage, other).ingest(document(content="Refund v2", version=2))
        assert not (await institute.retrieve(RetrievalQuery(query="refund"))).matches

    async def test_corruption_and_corpus_cap_fail_closed(self, storage):
        institute = service(storage)
        await institute.ingest(document("one"))
        await institute.ingest(document("two"))
        with pytest.raises(KnowledgeFailure) as error:
            await service(storage, rag_max_chunks=1).retrieve(RetrievalQuery(query="refund"))
        assert error.value.code == "CORPUS_LIMIT"
        await storage.db.chunks.update_one(
            {"document_id": "one"}, {"$set": {"embedding_model": "wrong"}}
        )
        with pytest.raises(KnowledgeFailure) as error:
            await institute.retrieve(RetrievalQuery(query="refund"))
        assert error.value.code == "CORPUS_INTEGRITY"

    async def test_lease_retry_and_stale_writer_cannot_overwrite_new_publication(self, storage):
        started, release = asyncio.Event(), asyncio.Event()
        base = TestOnlyEmbedder()

        class Slow(TestOnlyEmbedder):
            async def embed(self, texts, *, task="document"):
                started.set()
                await release.wait()
                return await super().embed(texts, task=task)

        slow = service(storage, Slow())
        pending = asyncio.create_task(slow.ingest(document()))
        await asyncio.wait_for(started.wait(), 3)
        try:
            with pytest.raises(KnowledgeFailure) as error:
                await service(storage, base).ingest(document())
            assert error.value.code == "INGESTION_BUSY"
            await storage.db.documents.update_one(
                {"document_id": "policy"},
                {"$set": {"lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
            )
            assert (await service(storage, base).ingest(document())).status == "ready"
        finally:
            release.set()
        with pytest.raises(KnowledgeFailure):
            await pending
        assert (await slow.document("policy", 1)).status == "ready"
        assert await storage.db.chunks.count_documents({}) == 1

    async def test_missing_calibration_is_explicit_and_read_requires_owned_turn(self, storage):
        registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
        business = registry.get("training-institute")
        provider = TestOnlyEmbedder()
        settings = Settings(_env_file=None)
        with pytest.raises(KnowledgeFailure) as error:
            await KnowledgeService(storage, business, provider, settings).retrieve(
                RetrievalQuery(query="refund")
            )
        assert error.value.code == "RELEVANCE_UNCALIBRATED"
        reader = KnowledgeReader(storage, registry, provider, settings)
        repo = Repository(storage, business)
        session = await repo.create_session("web", "synthetic-reader")
        context = ExecutionContext(
            request_id=uuid4(),
            business_id=business.business_id,
            channel="web",
            sender_id="wrong",
            session_id=session.session_id,
            turn_id="guessed",
        )
        with pytest.raises(RecordNotFound):
            await reader.read(context, RetrievalQuery(query="refund"))

    async def test_new_document_scoped_and_unknown_returns_no_evidence(self, storage):
        institute = service(storage)
        await institute.ingest(document("added", "Python synthetic newly added policy"))
        assert (await institute.retrieve(RetrievalQuery(query="Python"))).matches[
            0
        ].source.document_id == "added"
        assert not (
            await service(storage, business_id="fitness-studio").retrieve(
                RetrievalQuery(query="Python")
            )
        ).matches
        assert (
            await institute.retrieve(RetrievalQuery(query="spacecraft"))
        ).status == "no_evidence"
