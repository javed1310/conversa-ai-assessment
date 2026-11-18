import dataclasses
import importlib.util
import math
import sys
from pathlib import Path

import pytest
from pymongo.asynchronous.collection import AsyncCollection

from app.chunking import chunk_text
from app.embeddings import EmbeddingFailure
from app.knowledge import KnowledgeFailure
from tests.test_knowledge import TestOnlyEmbedder, document, service


def legacy_chunker():
    spec = importlib.util.spec_from_file_location(
        "chunker_reference", Path(__file__).parent / "fixtures/chunker_reference.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.chunk_text


@pytest.mark.parametrize(
    "text",
    [
        "Short policy.",
        "# Heading\n\n" + "Refund and eligibility.\n\n" * 100,
        "x" * 5000,
        "\u0938\u0940\u0916\u0947\u0902 \U0001f642 mixed policy text.\n\n" * 100,
        "a\u0301" * 700,
        "word\r\n\r\n## Section\n" * 100,
        "line\n" * 300,
        "\n\n" * 60 + "Policy",
    ],
)
@pytest.mark.parametrize("limit,overlap", [(8, 0), (40, 8), (400, 60)])
def test_drafts_equal_recorded_pipeline_for_boundaries_unicode_and_limits(text, limit, overlap):
    reference = legacy_chunker()
    try:
        before = reference(text, limit, overlap)
    except ValueError as error:
        with pytest.raises(ValueError, match=str(error)):
            chunk_text(text, limit, overlap)
    else:
        after = chunk_text(text, limit, overlap)
        assert [dataclasses.asdict(item) for item in before] == [
            dataclasses.asdict(item) for item in after
        ]


@pytest.mark.asyncio
async def test_ingestion_batches_existing_reads_and_retry_embeds_only_missing(storage, monkeypatch):
    provider = TestOnlyEmbedder()
    provider.fail_after = 1
    reader = service(storage, provider, chunk_tokens=20, chunk_overlap=0)
    source = document(content="Refund policy statement for synthetic pipeline testing.\n\n" * 80)
    expected = chunk_text(source.content, 20, 0)
    queries = []
    original = AsyncCollection.find

    def find(collection, query=None, *args, **kwargs):
        if collection.name == "chunks" and isinstance((query or {}).get("chunk_id"), dict):
            queries.append(query)
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(AsyncCollection, "find", find)
    with pytest.raises(EmbeddingFailure):
        await reader.ingest(source)
    assert await reader.db.chunks.count_documents(reader.scope) == 16
    queries.clear()
    provider.calls.clear()
    provider.fail_after = None
    result = await reader.ingest(source)
    assert result.status == "ready"
    assert len(queries) == math.ceil(len(expected) / 16)
    assert all(len(query["chunk_id"]["$in"]) <= 16 for query in queries)
    assert all(
        query["business_id"] == reader.business.business_id
        and query["knowledge_base_id"] == reader.business.knowledge_base_id
        and query["document_id"] == source.document_id
        and query["version"] == 1
        for query in queries
    )
    assert sum(len(texts) for texts in provider.calls) == len(expected) - 16
    assert await reader.db.chunks.count_documents(reader.scope) == len(expected)
    count = len(provider.calls)
    await reader.ingest(source)
    assert len(provider.calls) == count


@pytest.mark.asyncio
async def test_corrupted_existing_chunk_fails_before_retry_embedding(storage):
    provider = TestOnlyEmbedder()
    provider.fail_after = 1
    reader = service(storage, provider, chunk_tokens=20, chunk_overlap=0)
    source = document(content="Refund policy statement for synthetic pipeline testing.\n\n" * 80)
    with pytest.raises(EmbeddingFailure):
        await reader.ingest(source)
    await reader.db.chunks.update_one(reader.scope | {"ordinal": 0}, {"$set": {"text": "tampered"}})
    provider.calls.clear()
    provider.fail_after = None
    with pytest.raises(KnowledgeFailure) as error:
        await reader.ingest(source)
    assert error.value.code == "CORPUS_INTEGRITY"
    assert provider.calls == []
    assert (await reader.document(source.document_id, 1)).status == "failed"
