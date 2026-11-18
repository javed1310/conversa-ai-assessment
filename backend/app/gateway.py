"""Shared delivery path with persisted session gate and explicit incomplete-engine behavior."""

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pymongo.errors import PyMongoError

from app.businesses import BusinessRegistry
from app.channels import AdapterRegistry, Delivery, SessionStarted
from app.contracts import ChatResult, ExecutionContext, ProcessingResult
from app.coordination import CoordinationFailure, TurnCoordinator
from app.database import Database
from app.engine import ConversationEngine
from app.gemini import ProviderFailure
from app.measurements import operation
from app.persistence_models import Turn
from app.provider_control import deadline
from app.repositories import RecordConflict, RecordNotFound, Repository, parse
from app.state import StateService

logger = logging.getLogger(__name__)


class GatewayFailure(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Gateway:
    def __init__(
        self,
        database: Database,
        businesses: BusinessRegistry,
        adapters: AdapterRegistry,
        engine: ConversationEngine,
    ) -> None:
        self.database = database
        self.measurements = database.measurements
        self.businesses = businesses
        self.adapters = adapters
        self.engine = engine
        self.coordinator = TurnCoordinator()

    async def start_web_session(self, business_id: str) -> SessionStarted:
        business = self.businesses.get(business_id)
        if business is None:
            raise RecordNotFound("Record not found.")
        token = secrets.token_urlsafe(32)
        session = await Repository(self.database, business).create_session(
            "web", str(uuid4()), web_access_hash=hashlib.sha256(token.encode()).hexdigest()
        )
        return SessionStarted(
            business_id=business_id,
            session_id=session.session_id,
            session_token=token,
            engine_ready=self.engine.ready,
        )

    async def web_context(
        self, business_id: str, session_id: str, token: str | None, request_id: UUID
    ) -> ExecutionContext:
        if self.businesses.get(business_id) is None or token is None or not 40 <= len(token) <= 100:
            raise RecordNotFound("Record not found.")
        document = await self.database.db.sessions.find_one(
            {
                "business_id": business_id,
                "session_id": session_id,
                "channel": "web",
                "_web_access_hash": hashlib.sha256(token.encode()).hexdigest(),
            }
        )
        if document is None:
            raise RecordNotFound("Record not found.")
        return ExecutionContext(
            request_id=request_id,
            business_id=business_id,
            channel="web",
            session_id=session_id,
            sender_id=document["sender_id"],
            turn_id="pending",
        )

    async def mock_context(
        self, business_id: str, sender_id: str, request_id: UUID
    ) -> ExecutionContext:
        business = self.businesses.get(business_id)
        if business is None:
            raise RecordNotFound("Record not found.")
        session = await Repository(self.database, business).external_session(sender_id)
        return ExecutionContext(
            request_id=request_id,
            business_id=business_id,
            channel="mock",
            sender_id=sender_id,
            session_id=session.session_id,
            turn_id="pending",
        )

    @operation("chat")
    async def handle(self, context: ExecutionContext, payload: dict[str, object]) -> Delivery:
        business = self.businesses.get(context.business_id)
        if business is None:
            raise RecordNotFound("Record not found.")
        adapter = self.adapters.get(context.channel)
        message = adapter.normalize(payload, context)
        repo = Repository(self.database, business)
        await repo.session(context)
        owner = repo.owner(context)
        digest = hashlib.sha256(
            json.dumps(
                message.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identity = {key: owner[key] for key in ("business_id", "channel", "sender_id")}
        identity["message_id"] = message.message_id
        record = await self.database.db.turns.find_one(identity)
        previous = parse(Turn, record) if record is not None else None
        if previous is not None:
            if previous.payload_hash != digest or previous.session_id != context.session_id:
                raise RecordConflict("Message identity conflicts with existing payload.")
            if previous.status == "completed" and previous.output is not None:
                await self.database.db.sessions.update_one(
                    owner
                    | {"_pending_message_id": message.message_id, "_pending_payload_hash": digest},
                    {
                        "$unset": {
                            "_pending_message_id": "",
                            "_pending_payload_hash": "",
                            "_pending_token": "",
                            "_pending_expires_at": "",
                        }
                    },
                )
                return adapter.encode(previous.output, context)
            if previous.status == "failed":
                if (
                    previous.failure_code not in {"TIMEOUT", "UNAVAILABLE", "RATE_LIMIT"}
                    or previous.transient_retries >= 2
                ):
                    raise GatewayFailure(
                        503, "TURN_FAILED", "This message requires explicit recovery."
                    )
                if not self.engine.ready:
                    raise GatewayFailure(
                        503, "ENGINE_UNAVAILABLE", "Conversation engine is unavailable."
                    )
                recovery = context.model_copy(update={"turn_id": previous.turn_id})
                try:
                    await self.coordinator.retry_failed(repo, recovery, transient=True)
                except CoordinationFailure as error:
                    if error.code == "TURN_NOT_FAILED":
                        # Another retry has already reopened this identity; do not run it twice.
                        return adapter.encode(
                            ProcessingResult(
                                message_id=message.message_id, session_id=context.session_id
                            ),
                            context,
                        )
                    raise GatewayFailure(
                        error.status, error.code, "Message recovery cannot start safely."
                    ) from None
                previous = await repo.turn(recovery)
        if not self.engine.ready:
            raise GatewayFailure(
                503, "ENGINE_UNAVAILABLE", "Conversation engine is not available yet."
            )
        try:
            token = await self.coordinator.acquire(
                repo, context, message.message_id, digest, previous
            )
        except CoordinationFailure as error:
            if error.code == "PAYLOAD_CONFLICT":
                raise RecordConflict("Message identity conflicts with pending payload.") from None
            raise GatewayFailure(
                error.status, error.code, "Message processing cannot start safely."
            ) from None
        if token is None:
            return adapter.encode(
                ProcessingResult(message_id=message.message_id, session_id=context.session_id),
                context,
            )
        sessions = self.database.db.sessions
        gate = owner | {"_pending_token": token}
        unset = {
            "_pending_message_id": "",
            "_pending_payload_hash": "",
            "_pending_token": "",
            "_pending_expires_at": "",
        }
        turn: Turn | None = None
        try:
            turn = await repo.begin_turn(context, message)
            if turn.status == "completed" and turn.output is not None:
                await sessions.update_one(gate, {"$unset": unset})
                return adapter.encode(turn.output, context)
            if turn.status != "processing":
                raise GatewayFailure(503, "TURN_FAILED", "This message requires explicit recovery.")
            claimed = await self.database.db.turns.update_one(
                owner | {"turn_id": turn.turn_id, "status": "processing"},
                {"$set": {"processing_token": token}},
            )
            if claimed.matched_count != 1:
                raise GatewayFailure(503, "TURN_FAILED", "Message processing cannot start safely.")
            execution = context.model_copy(update={"turn_id": turn.turn_id, "lease_token": token})
            with deadline(
                max(
                    0.001,
                    self.coordinator.operation_seconds
                    - self.coordinator.finalization_reserve_seconds,
                )
            ):
                async with asyncio.timeout(self.coordinator.operation_seconds):
                    if turn.prepared_output is not None:
                        output = turn.prepared_output
                    else:
                        await StateService(repo).resume(execution)
                        output = await self.engine.respond(execution, message)
                        output = ChatResult.model_validate(
                            output.model_dump(mode="python", warnings=False)
                        )
                        await repo.prepare_response(execution, output)
                    completed = await repo.complete_turn(execution, output)
            assert completed.output is not None
            await sessions.update_one(gate, {"$unset": unset})
            return adapter.encode(completed.output, execution)
        except asyncio.CancelledError:
            # Keep the lease/checkpoints for bounded restart recovery of the original message.
            raise
        except Exception as error:
            logger.warning("turn_processing_failed error_type=%s", type(error).__name__)
            failure_code = "UNCLASSIFIED"
            if (
                isinstance(error, ProviderFailure)
                and error.retryable
                and error.code in {"TIMEOUT", "UNAVAILABLE", "RATE_LIMIT"}
            ):
                failure_code = error.code
            elif isinstance(error, TimeoutError):
                failure_code = "TIMEOUT"
            if turn is not None:
                try:
                    await self.database.db.turns.update_one(
                        owner
                        | {
                            "turn_id": turn.turn_id,
                            "status": "processing",
                            "processing_token": token,
                        },
                        {
                            "$set": {
                                "status": "failed",
                                "completed_at": datetime.now(UTC),
                                "failure_code": failure_code,
                            }
                        },
                    )
                    await sessions.update_one(gate, {"$unset": unset})
                except PyMongoError:
                    pass
            else:
                # An ownership/validation conflict before turn creation has no state to recover.
                await sessions.update_one(gate, {"$unset": unset})
                if isinstance(error, (RecordConflict, RecordNotFound)):
                    raise
            if isinstance(error, ProviderFailure) and error.code == "RATE_LIMIT":
                raise GatewayFailure(
                    503,
                    "RATE_LIMIT",
                    (
                        "The AI provider's daily request quota is exhausted. "
                        "Retry this same message after the quota resets."
                        if error.detail == "DAILY_QUOTA"
                        else "The AI provider is temporarily rate-limited. "
                        "Please retry this same message later."
                    ),
                ) from None
            raise GatewayFailure(
                503, "ENGINE_FAILURE", "Message processing could not complete."
            ) from None
        finally:
            self.coordinator.finished(context)
