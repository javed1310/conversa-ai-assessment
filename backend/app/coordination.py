"""Turn gate, durable leases and sealed-response or conservative read-only recovery."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

from app.contracts import ExecutionContext
from app.persistence_models import Turn
from app.repositories import RecordNotFound, Repository


class CoordinationFailure(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__("Turn coordination could not complete.")
        self.code = code
        self.status = status


class TurnCoordinator:
    lease_seconds = 120
    operation_seconds = 90
    finalization_reserve_seconds = 10

    def __init__(self) -> None:
        self.active: dict[tuple[str, str], tuple[str, str]] = {}

    async def safe_to_resume(self, repo: Repository, context: ExecutionContext, turn: Turn) -> None:
        if (await repo.session(context)).state_sequence > turn.sequence:
            raise CoordinationFailure("STALE_TURN", 409)
        if await repo.db.turns.find_one(repo.owner(context) | {"sequence": {"$gt": turn.sequence}}):
            raise CoordinationFailure("STALE_TURN", 409)
        if turn.prepared_output is not None:
            # Replay only the sealed response: no provider, state extraction or handlers run.
            return
        uncertain = await repo.db.tool_executions.find_one(
            {
                "business_id": context.business_id,
                "session_id": context.session_id,
                "turn_id": turn.turn_id,
                "$or": [
                    {"status": "processing"},
                    {"tool": {"$in": ["createLead", "requestCallback"]}},
                ],
            }
        )
        if uncertain is not None:
            raise CoordinationFailure("RECOVERY_REQUIRES_REVIEW", 503)

    async def acquire(
        self,
        repo: Repository,
        context: ExecutionContext,
        message_id: str,
        digest: str,
        previous: Turn | None,
    ) -> str | None:
        scope = (context.business_id, context.session_id)
        active = self.active.get(scope)
        if active:
            if active == (message_id, digest):
                return None
            raise CoordinationFailure("SESSION_BUSY", 409)
        current = await repo.db.sessions.find_one(repo.owner(context))
        if current is None:
            raise RecordNotFound("Record not found.")
        pending = current.get("_pending_message_id")
        now = datetime.now(UTC)
        key: dict[str, Any] = repo.owner(context)
        if pending is None:
            key["_pending_message_id"] = None
        else:
            if pending != message_id:
                raise CoordinationFailure("SESSION_BUSY", 409)
            if current.get("_pending_payload_hash") != digest:
                raise CoordinationFailure("PAYLOAD_CONFLICT", 409)
            expiry = current.get("_pending_expires_at")
            if not isinstance(expiry, datetime):
                # Pre-M08 gates have no timestamp; require explicit recovery rather than guess.
                raise CoordinationFailure("RECOVERY_REQUIRES_REVIEW", 503)
            if expiry > now:
                return None
            key |= {
                "_pending_token": current.get("_pending_token"),
                "_pending_expires_at": {"$lte": now},
            }
        if previous is not None:
            await self.safe_to_resume(repo, context, previous)
        token = str(uuid4())
        claim = await repo.db.sessions.find_one_and_update(
            key,
            {
                "$set": {
                    "_pending_message_id": message_id,
                    "_pending_payload_hash": digest,
                    "_pending_token": token,
                    "_pending_expires_at": now + timedelta(seconds=self.lease_seconds),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if claim is None:
            raced = await repo.db.sessions.find_one(repo.owner(context))
            if raced is None:
                raise RecordNotFound("Record not found.")
            if raced.get("_pending_message_id") != message_id:
                raise CoordinationFailure("SESSION_BUSY", 409)
            if raced.get("_pending_payload_hash") != digest:
                raise CoordinationFailure("PAYLOAD_CONFLICT", 409)
            return None
        self.active[scope] = (message_id, digest)
        return token

    async def retry_failed(self, repo: Repository, context: ExecutionContext) -> None:
        """Explicit internal recovery request; never a public arbitrary-retry endpoint."""
        turn = await repo.turn(context)
        if turn.status != "failed":
            raise CoordinationFailure("TURN_NOT_FAILED", 409)
        if (context.business_id, context.session_id) in self.active:
            raise CoordinationFailure("SESSION_BUSY", 409)
        await self.safe_to_resume(repo, context, turn)
        # Reserve the original identity before reopening the turn. Restart can resume this lease.
        reserved = await repo.db.sessions.update_one(
            repo.owner(context)
            | {
                "$or": [
                    {"_pending_message_id": None},
                    {
                        "_pending_message_id": turn.message_id,
                        "_pending_payload_hash": turn.payload_hash,
                        "_pending_expires_at": {"$lte": datetime.now(UTC)},
                    },
                ]
            },
            {
                "$set": {
                    "_pending_message_id": turn.message_id,
                    "_pending_payload_hash": turn.payload_hash,
                    "_pending_token": str(uuid4()),
                    "_pending_expires_at": datetime.now(UTC),
                }
            },
        )
        if reserved.modified_count != 1:
            raise CoordinationFailure("SESSION_BUSY", 409)
        await repo.db.turns.update_one(
            repo.owner(context) | {"turn_id": turn.turn_id, "status": "failed"},
            {"$set": {"status": "processing", "completed_at": None, "processing_token": None}},
        )

    def finished(self, context: ExecutionContext) -> None:
        self.active.pop((context.business_id, context.session_id), None)
