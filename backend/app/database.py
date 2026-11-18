"""One async Mongo client per lifespan, bounded operations and required indexes."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from pymongo import ASCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.measurements import Measurements
from app.query_embeddings import QueryEmbeddings
from app.rag_snapshots import RagSnapshots
from app.settings import Settings


class DatabaseStartupError(RuntimeError):
    """Safe startup failure without connection details."""


@dataclass
class Database:
    client: AsyncMongoClient[dict[str, Any]]
    db: AsyncDatabase[dict[str, Any]]
    measurements: Measurements = field(default_factory=Measurements, repr=False)
    query_embeddings: QueryEmbeddings = field(default_factory=QueryEmbeddings, repr=False)
    rag_snapshots: RagSnapshots = field(default_factory=RagSnapshots, repr=False)
    _publication_gates: dict[tuple[str, str], asyncio.Lock] = field(
        default_factory=dict, repr=False
    )

    def publication_gate(self, business_id: str, knowledge_base_id: str) -> asyncio.Lock:
        """Lifespan-owned gate for the supported single API process, never a distributed lock."""
        scope = (business_id, knowledge_base_id)
        gate = self._publication_gates.get(scope)
        if gate is None:
            gate = asyncio.Lock()
            self._publication_gates[scope] = gate
        return gate

    async def ready(self) -> bool:
        try:
            await self.db.command("ping")
            return True
        except PyMongoError:
            return False


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    unique: dict[str, list[tuple[str, ...]]] = {
        "courses": [("business_id", "course_id")],
        "memberships": [("business_id", "membership_id")],
        "sessions": [("business_id", "session_id")],
        "external_mappings": [("business_id", "channel", "sender_id")],
        "turns": [
            ("business_id", "turn_id"),
            ("business_id", "channel", "sender_id", "message_id"),
            ("business_id", "session_id", "sequence"),
        ],
        "leads": [
            ("business_id", "session_id"),
            ("business_id", "lead_id"),
            ("automation.event_id",),
        ],
        "documents": [("business_id", "document_id", "version")],
        "chunks": [("business_id", "document_id", "version", "chunk_id")],
        "tool_executions": [("business_id", "session_id", "turn_id", "call_id")],
    }
    for collection, indexes in unique.items():
        for keys in indexes:
            await db[collection].create_index(
                [(key, ASCENDING) for key in keys], unique=True, name="uq_" + "_".join(keys)
            )
    for collection, lookup_keys in {
        "sessions": ["business_id", "updated_at"],
        "leads": ["business_id", "created_at"],
        "turns": ["business_id", "session_id", "status"],
        "documents": ["business_id", "status"],
        "chunks": ["business_id", "document_id", "version"],
    }.items():
        await db[collection].create_index([(key, ASCENDING) for key in lookup_keys])
    await db.leads.create_index(
        [
            ("business_id", ASCENDING),
            ("automation.status", ASCENDING),
            ("automation.next_attempt_at", ASCENDING),
            ("automation.lease_until", ASCENDING),
        ],
        name="automation_eligibility",
    )
    # Equality prefix followed by fee/course ID order; justified by SD04 explain evidence.
    await db.courses.create_index(
        [
            ("business_id", ASCENDING),
            ("category", ASCENDING),
            ("fee", ASCENDING),
            ("course_id", ASCENDING),
        ],
        name="course_search_order",
    )


@asynccontextmanager
async def open_database(settings: Settings) -> AsyncIterator[Database]:
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
        settings.mongodb_uri.get_secret_value(),
        serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000,
        socketTimeoutMS=3000,
        timeoutMS=5000,
        tz_aware=True,
        maxPoolSize=20,
    )
    try:
        db = client.get_default_database("ai_engagement")
        if db.name in {"admin", "config", "local"}:
            raise DatabaseStartupError("Select an application database, not a system database.")
        try:
            await db.command("ping")
            await ensure_indexes(db)
        except PyMongoError:
            raise DatabaseStartupError(
                "Database startup failed. Check database availability, permissions and indexes."
            ) from None
        yield Database(client, db)
    finally:
        await client.close()
