"""Scoped, versioned ingestion and bounded cosine retrieval over actual stored embeddings."""

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, field_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.businesses import BusinessConfig, BusinessRegistry
from app.chunking import MAX_EMBEDDING_BYTES, chunk_text
from app.contracts import Contract, ExecutionContext, Identifier, SourceRef, Text
from app.database import Database
from app.embeddings import EmbeddingFailure, EmbeddingProvider, Vector, validate_vector
from app.measurements import operation, stage
from app.provider_control import deadline
from app.rag_snapshots import Candidate, Snapshot
from app.repositories import RecordConflict, RecordNotFound, Repository, parse, stored
from app.settings import Settings


class KnowledgeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("Knowledge operation unavailable.")
        self.code = code


class DocumentInput(Contract):
    document_id: Identifier
    version: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1
    title: Annotated[Text, Field(max_length=200)]
    format: Literal["text", "markdown"]
    content: Annotated[Text, Field(max_length=64000)]

    @field_validator("content")
    @classmethod
    def safe_text(cls, value: str) -> str:
        value = value.replace("\r\n", "\n")
        if len(value.encode()) > 128 * 1024 or any(
            ord(char) < 32 and char not in "\n\r\t" for char in value
        ):
            raise ValueError("Unsupported or oversized text content")
        return value


class DocumentRecord(DocumentInput):
    business_id: Identifier
    knowledge_base_id: Identifier
    content_hash: str
    pipeline_id: str
    embedding_model: str
    embedding_profile: str = "legacy-model-dimensions-only"
    dimensions: int
    status: Literal["pending", "ingesting", "ready", "failed"] = "pending"
    expected_chunks: int = 0
    created_at: AwareDatetime
    ingestion_token: str | None = None
    lease_until: AwareDatetime | None = None
    failure_code: str | None = None


class StoredChunk(Contract):
    business_id: Identifier
    knowledge_base_id: Identifier
    document_id: Identifier
    version: int
    chunk_id: Identifier
    ordinal: int
    content_hash: str
    pipeline_id: str
    text: str
    start: int
    end: int
    tokens: int
    embedding_model: str
    embedding_profile: str = "legacy-model-dimensions-only"
    dimensions: int
    vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]


class RetrievalQuery(Contract):
    query: Annotated[Text, Field(max_length=2000)]

    @field_validator("query")
    @classmethod
    def bound_query_bytes(cls, value: str) -> str:
        if len(value.encode()) > MAX_EMBEDDING_BYTES:
            raise ValueError("Query exceeds the conservative Gemini input budget")
        return value


class RetrievalMatch(Contract):
    score: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
    text: str
    source: SourceRef


class RetrievalResult(Contract):
    status: Literal["found", "no_evidence"]
    matches: tuple[RetrievalMatch, ...]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cosine(left: Vector, right: Vector) -> float:
    if len(left) != len(right):
        raise KnowledgeFailure("EMBEDDING_IDENTITY_MISMATCH")
    # Normalize each operand before multiplication to avoid finite-value overflow.
    left_norm, right_norm = math.hypot(*left), math.hypot(*right)
    if (
        not left_norm
        or not right_norm
        or not math.isfinite(left_norm)
        or not math.isfinite(right_norm)
    ):
        raise KnowledgeFailure("INVALID_VECTOR")
    return max(
        -1.0,
        min(
            1.0,
            math.fsum((a / left_norm) * (b / right_norm) for a, b in zip(left, right, strict=True)),
        ),
    )


class KnowledgeService:
    def __init__(
        self,
        database: Database,
        business: BusinessConfig,
        provider: EmbeddingProvider,
        settings: Settings,
    ) -> None:
        self.db = database.db
        self.measurements = database.measurements
        self.query_embeddings = database.query_embeddings
        self.snapshots = database.rag_snapshots
        self.namespace = (business.business_id, business.knowledge_base_id)
        self._publication_gate = database.publication_gate(
            business.business_id, business.knowledge_base_id
        )
        self.business = business
        self.provider = provider
        self.settings = settings
        self.scope = {
            "business_id": business.business_id,
            "knowledge_base_id": business.knowledge_base_id,
        }
        self.pipeline_id = digest(
            json.dumps(
                {
                    "chunker": "paragraph-char-token-v1",
                    "encoding": "cl100k_base",
                    "max_utf8_bytes": MAX_EMBEDDING_BYTES,
                    "embedding_profile": provider.profile,
                    "tokens": settings.chunk_tokens,
                    "overlap": settings.chunk_overlap,
                    "model": provider.model,
                    "dimensions": provider.dimensions,
                },
                sort_keys=True,
            )
        )

    def key(self, document_id: str, version: int) -> dict[str, Any]:
        return self.scope | {"document_id": document_id, "version": version}

    async def register(self, document: DocumentInput) -> DocumentRecord:
        record = DocumentRecord(
            **document.model_dump(),
            **self.scope,
            content_hash=digest(document.content),
            pipeline_id=self.pipeline_id,
            embedding_model=self.provider.model,
            embedding_profile=self.provider.profile,
            dimensions=self.provider.dimensions,
            created_at=datetime.now(UTC),
        )
        try:
            await self.db.documents.insert_one(stored(record))
            return record
        except DuplicateKeyError:
            previous = await self.document(document.document_id, document.version)
            if any(
                getattr(previous, field) != getattr(record, field)
                for field in ("content_hash", "title", "format", "pipeline_id")
            ):
                raise RecordConflict("Document version is immutable; use a new version.") from None
            return previous

    async def document(self, document_id: str, version: int) -> DocumentRecord:
        return parse(
            DocumentRecord, await self.db.documents.find_one(self.key(document_id, version))
        )

    async def documents(self) -> tuple[DocumentRecord, ...]:
        return tuple(
            [
                parse(DocumentRecord, record)
                async for record in self.db.documents.find(self.scope)
                .sort([("document_id", 1), ("version", -1)])
                .limit(500)
            ]
        )

    @operation("ingestion")
    async def ingest(self, document: DocumentInput) -> DocumentRecord:
        previous = await self.register(document)
        if previous.status == "ready":
            await self._chunks(previous)
            return previous
        now, token = datetime.now(UTC), str(uuid4())
        key = self.key(document.document_id, document.version)
        claimed = await self.db.documents.find_one_and_update(
            key
            | {
                "$or": [
                    {"status": {"$in": ["pending", "failed"]}},
                    {"status": "ingesting", "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "status": "ingesting",
                    "ingestion_token": token,
                    "lease_until": now + timedelta(seconds=90),
                    "failure_code": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            raise KnowledgeFailure("INGESTION_BUSY")
        claim = key | {"status": "ingesting", "ingestion_token": token}
        try:
            with deadline(55):
                async with asyncio.timeout(60):
                    drafts = chunk_text(
                        document.content, self.settings.chunk_tokens, self.settings.chunk_overlap
                    )
                    if len(drafts) > self.settings.rag_max_chunks:
                        raise KnowledgeFailure("CORPUS_LIMIT")
                    await self.db.documents.update_one(
                        claim, {"$set": {"expected_chunks": len(drafts)}}
                    )
                    # Cheap preflight avoids embedding a document that already cannot fit.
                    # This does not reserve capacity: the final publication repeats the check.
                    async with self._publication_gate:
                        await self._admit(
                            await self.document(document.document_id, document.version)
                        )
                    # Immutable version/chunk identities make late same-version writes harmless.
                    for offset in range(0, len(drafts), 16):
                        batch = drafts[offset : offset + 16]
                        existing = (
                            await self.db.chunks.find(
                                key | {"chunk_id": {"$in": [draft.chunk_id for draft in batch]}}
                            )
                            .limit(len(batch) + 1)
                            .to_list()
                        )
                        saved_chunks = {item["chunk_id"]: item for item in existing}
                        if len(saved_chunks) != len(existing) or len(existing) > len(batch):
                            raise KnowledgeFailure("CORPUS_INTEGRITY")
                        missing = []
                        for draft in batch:
                            saved = saved_chunks.get(draft.chunk_id)
                            if saved is None:
                                missing.append(draft)
                            else:
                                chunk = parse(StoredChunk, saved)
                                if (
                                    chunk.business_id != self.business.business_id
                                    or chunk.knowledge_base_id != self.business.knowledge_base_id
                                    or chunk.document_id != document.document_id
                                    or chunk.version != document.version
                                    or chunk.chunk_id != draft.chunk_id
                                    or chunk.ordinal != draft.ordinal
                                    or chunk.text != draft.text
                                    or chunk.start != draft.start
                                    or chunk.end != draft.end
                                    or chunk.tokens != draft.tokens
                                    or chunk.embedding_model != self.provider.model
                                    or chunk.embedding_profile != self.provider.profile
                                    or chunk.dimensions != self.provider.dimensions
                                    or chunk.pipeline_id != self.pipeline_id
                                    or chunk.content_hash != draft.content_hash
                                ):
                                    raise KnowledgeFailure("CORPUS_INTEGRITY")
                                validate_vector(chunk.vector, self.provider.dimensions)
                        if not missing:
                            continue
                        vectors = await self.provider.embed(
                            tuple(draft.text for draft in missing), task="document"
                        )
                        if len(vectors) != len(missing):
                            raise EmbeddingFailure("INVALID_RESPONSE")
                        for draft, vector in zip(missing, vectors, strict=True):
                            chunk = StoredChunk(
                                **key,
                                chunk_id=draft.chunk_id,
                                ordinal=draft.ordinal,
                                content_hash=draft.content_hash,
                                pipeline_id=self.pipeline_id,
                                text=draft.text,
                                start=draft.start,
                                end=draft.end,
                                tokens=draft.tokens,
                                embedding_model=self.provider.model,
                                embedding_profile=self.provider.profile,
                                dimensions=self.provider.dimensions,
                                vector=validate_vector(vector, self.provider.dimensions),
                            )
                            chunk_key = key | {"chunk_id": chunk.chunk_id}
                            try:
                                await self.db.chunks.update_one(
                                    chunk_key, {"$setOnInsert": stored(chunk)}, upsert=True
                                )
                            except DuplicateKeyError:
                                if await self.db.chunks.find_one(chunk_key) is None:
                                    raise
                    current = await self.document(document.document_id, document.version)
                    await self._chunks(current)
                    async with self._publication_gate:
                        await self._admit(current)
                        publication = await self.db.documents.update_one(
                            claim,
                            {
                                "$set": {
                                    "status": "ready",
                                    "ingestion_token": None,
                                    "lease_until": None,
                                    "failure_code": None,
                                }
                            },
                        )
                        if publication.modified_count != 1:
                            raise KnowledgeFailure("CLAIM_LOST")
                        self.snapshots.invalidate(self.namespace)
                    return await self.document(document.document_id, document.version)
        except Exception as error:
            code = (
                error.code
                if isinstance(error, (EmbeddingFailure, KnowledgeFailure))
                else "INGESTION_FAILED"
            )
            await self.db.documents.update_one(
                claim,
                {
                    "$set": {
                        "status": "failed",
                        "failure_code": code,
                        "ingestion_token": None,
                        "lease_until": None,
                    }
                },
            )
            raise

    @stage("chunk_read_validate")
    async def _chunks(
        self, document: DocumentRecord, raw: list[dict[str, Any]] | None = None
    ) -> tuple[StoredChunk, ...]:
        if raw is None:
            raw = (
                await self.db.chunks.find(self.key(document.document_id, document.version))
                .limit(self.settings.rag_max_chunks + 1)
                .to_list()
            )
        if not document.expected_chunks or len(raw) != document.expected_chunks:
            raise KnowledgeFailure("CORPUS_INTEGRITY")
        chunks = tuple(
            sorted((parse(StoredChunk, item) for item in raw), key=lambda item: item.ordinal)
        )
        if [item.ordinal for item in chunks] != list(range(document.expected_chunks)):
            raise KnowledgeFailure("CORPUS_INTEGRITY")
        if (
            digest(document.content) != document.content_hash
            or chunks[0].start != 0
            or chunks[-1].end != len(document.content)
        ):
            raise KnowledgeFailure("CORPUS_INTEGRITY")
        for before, current in zip(chunks[:-1], chunks[1:], strict=True):
            if not before.start < current.start <= before.end < current.end:
                raise KnowledgeFailure("CORPUS_INTEGRITY")
        for chunk in chunks:
            if (
                chunk.business_id != document.business_id
                or chunk.knowledge_base_id != document.knowledge_base_id
                or chunk.document_id != document.document_id
                or chunk.version != document.version
                or chunk.pipeline_id != document.pipeline_id
                or chunk.embedding_model != document.embedding_model
                or chunk.embedding_profile != document.embedding_profile
                or chunk.dimensions != document.dimensions
                or digest(chunk.text) != chunk.content_hash
                or document.content[chunk.start : chunk.end] != chunk.text
            ):
                raise KnowledgeFailure("CORPUS_INTEGRITY")
            validate_vector(chunk.vector, document.dimensions)
        return chunks

    @stage("corpus_snapshot")
    async def _active_documents(self, *, metadata: bool = False) -> tuple[Any, ...]:
        """The same latest-ready compatible view is used for admission and retrieval."""
        pipeline: list[dict[str, Any]] = [
            {"$match": self.scope | {"status": "ready"}},
            {"$sort": {"version": -1}},
            {"$group": {"_id": "$document_id", "document": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$document"}},
            {
                "$match": {
                    "embedding_model": self.provider.model,
                    "embedding_profile": self.provider.profile,
                    "dimensions": self.provider.dimensions,
                }
            },
            {"$limit": self.settings.rag_max_chunks + 1},
        ]
        if metadata:
            pipeline.append(
                {
                    "$project": dict.fromkeys(
                        (
                            "business_id",
                            "knowledge_base_id",
                            "document_id",
                            "version",
                            "title",
                            "content_hash",
                            "pipeline_id",
                            "embedding_model",
                            "embedding_profile",
                            "dimensions",
                            "expected_chunks",
                        ),
                        1,
                    )
                    | {"_id": 0}
                }
            )
        records = await (await self.db.documents.aggregate(pipeline)).to_list()
        if metadata:
            return tuple(sorted(records, key=lambda item: (item["document_id"], item["version"])))
        return tuple(parse(DocumentRecord, record) for record in records)

    @stage("corpus_admission")
    async def _admit(self, candidate: DocumentRecord) -> None:
        """Caller holds the publication gate; embeddings and chunk writes remain outside it."""
        active = {item.document_id: item for item in await self._active_documents()}
        latest = await self.db.documents.find_one(
            self.scope | {"document_id": candidate.document_id, "status": "ready"},
            sort=[("version", -1)],
        )
        if latest is None or candidate.version > latest["version"]:
            active[candidate.document_id] = candidate
        if any(item.expected_chunks < 1 for item in active.values()):
            raise KnowledgeFailure("CORPUS_INTEGRITY")
        if sum(item.expected_chunks for item in active.values()) > self.settings.rag_max_chunks:
            raise KnowledgeFailure("CORPUS_LIMIT")

    @stage("ranking")
    async def rank_for_calibration(self, query: RetrievalQuery) -> tuple[RetrievalMatch, ...]:
        async with self._publication_gate:
            stamps = await self._active_documents(metadata=True)
            epoch = self.snapshots.epoch
        if any(item["expected_chunks"] < 1 for item in stamps):
            raise KnowledgeFailure("CORPUS_INTEGRITY")
        if sum(item["expected_chunks"] for item in stamps) > self.settings.rag_max_chunks:
            raise KnowledgeFailure("CORPUS_LIMIT")
        key = (
            *self.namespace,
            self.provider.model,
            self.provider.profile,
            str(self.provider.dimensions),
            self.pipeline_id,
            str(self.settings.rag_max_chunks),
            str(self.settings.rag_snapshot_ttl_seconds),
            digest(json.dumps(stamps, sort_keys=True)),
        )

        @stage("rag_snapshot_load")
        async def load() -> Snapshot:
            if not stamps:
                return ()
            selection = self.scope | {
                "$or": [
                    {"document_id": item["document_id"], "version": item["version"]}
                    for item in stamps
                ]
            }
            records = (
                await self.db.documents.find(selection | {"status": "ready"})
                .limit(self.settings.rag_max_chunks + 1)
                .to_list()
            )
            documents = {
                (item.document_id, item.version): item
                for item in (parse(DocumentRecord, record) for record in records)
            }
            raw_chunks = (
                await self.db.chunks.find(selection)
                .limit(self.settings.rag_max_chunks + 1)
                .to_list()
            )
            if len(raw_chunks) > self.settings.rag_max_chunks or len(records) != len(stamps):
                raise KnowledgeFailure("CORPUS_INTEGRITY")
            groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
            for raw in raw_chunks:
                groups.setdefault((raw["document_id"], raw["version"]), []).append(raw)
            candidates = []
            for stamp in stamps:
                identity = (stamp["document_id"], stamp["version"])
                document = documents.get(identity)
                if document is None or any(
                    getattr(document, field) != value for field, value in stamp.items()
                ):
                    raise KnowledgeFailure("CORPUS_INTEGRITY")
                for chunk in await self._chunks(document, groups.get(identity, [])):
                    vector = validate_vector(chunk.vector, document.dimensions)
                    norm = math.hypot(*vector)
                    if not math.isfinite(norm) or not norm:
                        raise KnowledgeFailure("INVALID_VECTOR")
                    candidates.append(
                        Candidate(
                            source=SourceRef(
                                **self.scope,
                                document_id=document.document_id,
                                document_version=document.version,
                                chunk_id=chunk.chunk_id,
                                title=document.title,
                                excerpt=chunk.text[:8000],
                            ),
                            text=chunk.text,
                            vector=tuple(value / norm for value in vector),
                        )
                    )
            return tuple(candidates)

        candidates = await self.snapshots.load(
            key, self.namespace, epoch, self.settings.rag_snapshot_ttl_seconds, load
        )
        if not candidates:
            return ()

        async def embed_query() -> Vector:
            vectors = await self.provider.embed((query.query,), task="query")
            if len(vectors) != 1:
                raise EmbeddingFailure("INVALID_RESPONSE")
            return validate_vector(vectors[0], self.provider.dimensions)

        vector = await self.query_embeddings.load(
            (
                *self.namespace,
                type(self.provider).__module__,
                type(self.provider).__qualname__,
                self.provider.model,
                self.provider.profile,
                str(self.provider.dimensions),
                "query",
                str(self.settings.query_embedding_ttl_seconds),
                self.query_embeddings.query_digest(query.query),
            ),
            dimensions=self.provider.dimensions,
            ttl=self.settings.query_embedding_ttl_seconds,
            loader=embed_query,
        )
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or not norm:
            raise KnowledgeFailure("INVALID_VECTOR")
        vector = tuple(value / norm for value in vector)
        ranked = [
            RetrievalMatch(
                score=max(
                    -1.0,
                    min(1.0, math.fsum(a * b for a, b in zip(vector, item.vector, strict=True))),
                ),
                text=item.text,
                source=item.source,
            )
            for item in candidates
        ]
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    -item.score,
                    item.source.document_id,
                    item.source.document_version,
                    item.source.chunk_id,
                ),
            )
        )

    @stage("rag")
    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        if self.settings.rag_min_score is None:
            raise KnowledgeFailure("RELEVANCE_UNCALIBRATED")
        ranked = await self.rank_for_calibration(query)
        matches = tuple(item for item in ranked if item.score >= self.settings.rag_min_score)[
            : self.settings.rag_top_k
        ]
        return RetrievalResult(status="found" if matches else "no_evidence", matches=matches)


class KnowledgeReader:
    """Validated read capability for later engine integration, not a model-supplied source list."""

    def __init__(
        self,
        database: Database,
        businesses: BusinessRegistry,
        provider: EmbeddingProvider,
        settings: Settings,
    ) -> None:
        self.database, self.businesses, self.provider, self.settings = (
            database,
            businesses,
            provider,
            settings,
        )

    async def read(self, context: ExecutionContext, query: RetrievalQuery) -> RetrievalResult:
        business = self.businesses.get(context.business_id)
        if business is None:
            raise RecordNotFound("Record not found.")
        await Repository(self.database, business).turn(context)
        return await KnowledgeService(
            self.database, business, self.provider, self.settings
        ).retrieve(query)
