"""Business-scoped persistence primitives; no model, channel parsing or tool execution."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.businesses import BusinessConfig
from app.contracts import (
    AutomationEvent,
    CallbackIntent,
    Channel,
    ChatResult,
    ExecutionContext,
    LeadInput,
    NormalizedMessage,
    SearchCoursesArguments,
    Session,
    Summary,
)
from app.database import Database
from app.measurements import count, stage
from app.persistence_models import (
    Course,
    HistoryEntry,
    LeadRecord,
    Membership,
    MockNotification,
    Turn,
)


class RecordNotFound(RuntimeError):
    """Missing or foreign-owned record; deliberately indistinguishable."""


class RecordConflict(RuntimeError):
    """Identity reuse conflicts with existing immutable data."""


def parse[Model: BaseModel](model: type[Model], document: dict[str, Any] | None) -> Model:
    if document is None:
        raise RecordNotFound("Record not found.")
    return model.model_validate(
        {
            key: value
            for key, value in document.items()
            if key
            not in {
                "_id",
                "next_sequence",
                "_web_access_hash",
                "_pending_message_id",
                "_pending_payload_hash",
                "_pending_token",
                "_pending_expires_at",
            }
        }
    )


def stored(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="python")


class Repository:
    def __init__(self, database: Database, business: BusinessConfig) -> None:
        self.db = database.db
        self.business_id = business.business_id

    def owner(self, context: ExecutionContext) -> dict[str, str]:
        if context.business_id != self.business_id:
            raise RecordNotFound("Record not found.")
        return {
            "business_id": self.business_id,
            "session_id": context.session_id,
            "channel": context.channel,
            "sender_id": context.sender_id,
        }

    async def courses(self) -> tuple[Course, ...]:
        cursor = self.db.courses.find({"business_id": self.business_id}).sort(
            [("fee", 1), ("course_id", 1)]
        )
        return tuple([parse(Course, item) async for item in cursor])

    async def search_courses(self, arguments: SearchCoursesArguments) -> tuple[Course, ...]:
        key: dict[str, Any] = {
            "business_id": self.business_id,
            "category": arguments.category,
            "fee": {"$lte": arguments.budget},
        }
        if arguments.schedule is not None:
            key["schedule"] = arguments.schedule
        cursor = self.db.courses.find(key).sort([("fee", 1), ("course_id", 1)])
        return tuple([parse(Course, item) async for item in cursor])

    async def course(self, course_id: str) -> Course:
        return parse(
            Course,
            await self.db.courses.find_one(
                {"business_id": self.business_id, "course_id": course_id}
            ),
        )

    async def memberships(self) -> tuple[Membership, ...]:
        return tuple(
            [
                parse(Membership, item)
                async for item in self.db.memberships.find({"business_id": self.business_id}).sort(
                    "membership_id", 1
                )
            ]
        )

    async def membership(self, membership_id: str) -> Membership:
        return parse(
            Membership,
            await self.db.memberships.find_one(
                {"business_id": self.business_id, "membership_id": membership_id}
            ),
        )

    async def create_session(
        self,
        channel: Channel,
        sender_id: str,
        *,
        web_access_hash: str | None = None,
    ) -> Session:
        now = datetime.now(UTC)
        session = Session(
            session_id=str(uuid4()),
            business_id=self.business_id,
            channel=channel,
            sender_id=sender_id,
            created_at=now,
            updated_at=now,
        )
        document = stored(session) | {"next_sequence": 0}
        if web_access_hash is not None:
            document["_web_access_hash"] = web_access_hash
        await self.db.sessions.insert_one(document)
        return session

    async def external_session(self, sender_id: str) -> Session:
        """Publish the mapping first; every retry repairs the same chosen session ID.

        Each write is atomic independently. No cross-document transaction or automatic
        cross-channel identity merge is claimed. A failed second write is safe to retry.
        """
        identity = {"business_id": self.business_id, "channel": "mock", "sender_id": sender_id}
        try:
            mapping = await self.db.external_mappings.find_one_and_update(
                identity,
                {"$setOnInsert": identity | {"session_id": str(uuid4())}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            mapping = await self.db.external_mappings.find_one(identity)
        if mapping is None:
            raise RecordNotFound("Record not found.")
        now = datetime.now(UTC)
        session = Session(
            business_id=self.business_id,
            channel="mock",
            sender_id=sender_id,
            session_id=mapping["session_id"],
            created_at=now,
            updated_at=now,
        )
        owner = identity | {"session_id": session.session_id}
        try:
            await self.db.sessions.update_one(
                owner, {"$setOnInsert": stored(session) | {"next_sequence": 0}}, upsert=True
            )
        except DuplicateKeyError:
            # Only accept the winning session if every ownership field agrees.
            if await self.db.sessions.find_one(owner) is None:
                raise RecordConflict("Session mapping conflicts with existing ownership.") from None
        return parse(Session, await self.db.sessions.find_one(owner))

    async def session(self, context: ExecutionContext) -> Session:
        key: dict[str, Any] = self.owner(context)
        if context.lease_token is not None:
            key |= {
                "_pending_token": context.lease_token,
                "_pending_expires_at": {"$gt": datetime.now(UTC)},
            }
        return parse(Session, await self.db.sessions.find_one(key))

    @stage("persistence")
    async def begin_turn(self, context: ExecutionContext, message: NormalizedMessage) -> Turn:
        owner = self.owner(context)
        await self.session(context)
        if any(getattr(message, key) != value for key, value in owner.items()):
            raise RecordNotFound("Record not found.")
        digest = hashlib.sha256(
            json.dumps(
                message.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identity = {key: owner[key] for key in ("business_id", "channel", "sender_id")}
        identity["message_id"] = message.message_id

        async def existing() -> Turn | None:
            document = await self.db.turns.find_one(identity)
            if document is None:
                return None
            turn = parse(Turn, document)
            if turn.payload_hash != digest or turn.session_id != context.session_id:
                raise RecordConflict("Message identity conflicts with an existing payload.")
            return turn

        previous = await existing()
        if previous:
            return previous
        allocation = await self.db.sessions.find_one_and_update(
            owner,
            {"$inc": {"next_sequence": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            return_document=ReturnDocument.AFTER,
        )
        if allocation is None:
            raise RecordNotFound("Record not found.")
        turn = Turn(
            turn_id=str(uuid4()),
            **owner,
            message_id=message.message_id,
            sequence=allocation["next_sequence"],
            payload_hash=digest,
            input_text=message.content,
            input_timestamp=message.timestamp,
            created_at=datetime.now(UTC),
        )
        try:
            await self.db.turns.insert_one(stored(turn))
        except DuplicateKeyError:
            previous = await existing()
            if previous is None:
                raise RecordConflict("Turn identity allocation conflicted.") from None
            return previous
        return turn

    async def turn(self, context: ExecutionContext) -> Turn:
        count("turn_reads")
        await self.session(context)
        return parse(
            Turn, await self.db.turns.find_one(self.owner(context) | {"turn_id": context.turn_id})
        )

    async def history(self, context: ExecutionContext) -> tuple[Turn, ...]:
        await self.session(context)
        return tuple(
            [
                parse(Turn, item)
                async for item in self.db.turns.find(self.owner(context)).sort("sequence", 1)
            ]
        )

    @stage("persistence")
    async def prepare_response(self, context: ExecutionContext, output: ChatResult) -> Turn:
        """Seal a validated reply under the owning lease before terminal publication."""
        await self.session(context)
        turn = await self.turn(context)
        if output.message_id != turn.message_id or output.session_id != turn.session_id:
            raise RecordConflict("Output does not belong to this turn.")
        key = self.owner(context) | {
            "turn_id": turn.turn_id,
            "status": "processing",
            "prepared_output": None,
        }
        if context.lease_token is not None:
            key["processing_token"] = context.lease_token
        document = await self.db.turns.find_one_and_update(
            key,
            {"$set": {"prepared_output": stored(output)}},
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return parse(Turn, document)
        previous = await self.turn(context)
        if previous.prepared_output != output:
            raise RecordConflict("Turn has a different prepared response.")
        return previous

    @stage("persistence")
    async def complete_turn(self, context: ExecutionContext, output: ChatResult) -> Turn:
        turn = await self.turn(context)
        if turn.prepared_output is not None and turn.prepared_output != output:
            raise RecordConflict("Output conflicts with the prepared response.")
        if output.message_id != turn.message_id or output.session_id != turn.session_id:
            raise RecordConflict("Output does not belong to this turn.")
        key = self.owner(context) | {"turn_id": turn.turn_id, "status": "processing"}
        if context.lease_token is not None:
            key["processing_token"] = context.lease_token
        document = await self.db.turns.find_one_and_update(
            key,
            {
                "$set": {
                    "status": "completed",
                    "output": stored(output),
                    "completed_at": datetime.now(UTC),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is not None:
            return parse(Turn, document)
        previous = await self.turn(context)
        if previous.status != "completed" or previous.output != output:
            raise RecordConflict("Turn already has a different terminal outcome.")
        return previous

    async def create_lead(self, context: ExecutionContext, customer: LeadInput) -> LeadRecord:
        turn = await self.turn(context)
        if customer.course_id is not None:
            await self.course(customer.course_id)
        # Immutable, bounded history snapshot ends at the actual lead-creation turn.
        cursor = self.db.turns.find(self.owner(context) | {"sequence": {"$lte": turn.sequence}})
        turns = [parse(Turn, item) async for item in cursor.sort("sequence", -1).limit(25)]
        turns.reverse()
        entries = []
        for item in turns:
            entries.append(
                HistoryEntry(turn_id=item.turn_id, role="customer", content=item.input_text)
            )
            if item.output:
                entries.append(
                    HistoryEntry(turn_id=item.turn_id, role="assistant", content=item.output.reply)
                )
        lead = LeadRecord(
            lead_id=str(uuid4()),
            business_id=self.business_id,
            session_id=context.session_id,
            created_at=datetime.now(UTC),
            customer=customer,
            automation=AutomationEvent(event_id=str(uuid4())),
            automation_input=tuple(entries),
            automation_callback_intent=(await self.session(context)).state.callback_intent,
        )
        try:
            await self.db.leads.insert_one(stored(lead))
            return lead
        except DuplicateKeyError:
            # One inquiry per session. Return the historical saved snapshot; never overwrite it.
            return await self.lead_for_session(context)
        except PyMongoError as error:
            # Acknowledgement may be lost after Mongo committed the unique inquiry insert.
            try:
                return await self.lead_for_session(context)
            except RecordNotFound:
                raise error from None

    async def lead_for_session(self, context: ExecutionContext) -> LeadRecord:
        await self.session(context)
        return parse(
            LeadRecord,
            await self.db.leads.find_one(
                {"business_id": self.business_id, "session_id": context.session_id}
            ),
        )

    async def lead(self, context: ExecutionContext, lead_id: str) -> LeadRecord:
        lead = await self.lead_for_session(context)
        if lead.lead_id != lead_id:
            raise RecordNotFound("Record not found.")
        return lead

    async def request_callback(
        self, context: ExecutionContext, lead_id: str, intent: CallbackIntent
    ) -> LeadRecord:
        lead = await self.lead(context, lead_id)
        evidence_context = context.model_copy(update={"turn_id": intent.evidence.turn_id})
        evidence_turn = await self.turn(evidence_context)
        if intent.evidence.excerpt not in evidence_turn.input_text:
            raise RecordConflict("Callback evidence is not in the owning customer turn.")
        await self.db.leads.update_one(
            {
                "business_id": self.business_id,
                "lead_id": lead.lead_id,
                "session_id": context.session_id,
                "callback_requested_at": None,
            },
            {
                "$set": {
                    "callback_requested_at": datetime.now(UTC),
                    "callback_request_id": str(uuid4()),
                }
            },
        )
        # Backward-compatible repair of an older local record with a timestamp but no receipt ID.
        await self.db.leads.update_one(
            {
                "business_id": self.business_id,
                "lead_id": lead.lead_id,
                "session_id": context.session_id,
                "callback_requested_at": {"$ne": None},
                "callback_request_id": None,
            },
            {"$set": {"callback_request_id": str(uuid4())}},
        )
        return await self.lead(context, lead_id)

    async def save_summary(self, lead_id: str, event_id: str, summary: Summary) -> LeadRecord:
        # Worker entry: explicitly business-scoped; not a public customer-facing operation.
        key = {"business_id": self.business_id, "lead_id": lead_id, "automation.event_id": event_id}
        lead = parse(LeadRecord, await self.db.leads.find_one(key))
        if summary.lead_id != lead_id or summary.customer_name != lead.customer.name:
            raise RecordConflict("Summary identity does not match the saved lead.")
        await self.db.leads.update_one(
            key | {"automation.summary": None}, {"$set": {"automation.summary": stored(summary)}}
        )
        return parse(LeadRecord, await self.db.leads.find_one(key))

    async def save_notification(self, lead_id: str, event_id: str) -> LeadRecord:
        key = {"business_id": self.business_id, "lead_id": lead_id, "automation.event_id": event_id}
        lead = parse(LeadRecord, await self.db.leads.find_one(key))
        if lead.automation.summary is None:
            raise RecordConflict("Notification requires an already saved summary.")
        notification = MockNotification(
            notification_id=str(uuid4()),
            event_id=event_id,
            lead_id=lead_id,
            created_at=datetime.now(UTC),
            payload=lead.automation.summary,
        )
        await self.db.leads.update_one(
            key | {"notification": None},
            {"$set": {"notification": stored(notification), "automation.status": "completed"}},
        )
        return parse(LeadRecord, await self.db.leads.find_one(key))
