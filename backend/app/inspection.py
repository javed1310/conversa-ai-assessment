"""Local assessment workspace APIs; explicit public projections and scoped keyset pages."""

import base64
import binascii
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import AwareDatetime, Field, ValidationError

from app.contracts import Channel, ChatResult, Contract, Identifier, LeadInput, Summary
from app.knowledge import DocumentInput, DocumentRecord, KnowledgeService
from app.repositories import RecordNotFound


class ConversationView(Contract):
    business_id: Identifier
    session_id: Identifier
    channel: Channel
    created_at: AwareDatetime
    updated_at: AwareDatetime
    lead_id: Identifier | None = None


class TurnView(Contract):
    turn_id: Identifier
    message_id: Identifier
    sequence: int
    input_text: str
    status: Literal["processing", "completed", "failed"]
    output: ChatResult | None = None


class AutomationView(Contract):
    event_id: Identifier
    status: Literal["pending", "processing", "completed", "failed"]
    attempts: int
    summary: Summary | None = None
    failure_code: str | None = None
    retryable: bool


class LeadView(Contract):
    business_id: Identifier
    lead_id: Identifier
    session_id: Identifier
    created_at: AwareDatetime
    customer: LeadInput
    automation: AutomationView
    callback_requested_at: AwareDatetime | None = None
    callback_request_id: Identifier | None = None
    notification_created_at: AwareDatetime | None = None


class DocumentView(Contract):
    business_id: Identifier
    document_id: Identifier
    version: int
    title: str
    format: Literal["text", "markdown"]
    status: Literal["pending", "ingesting", "ready", "failed"]
    expected_chunks: int
    failure_code: str | None = None
    created_at: AwareDatetime


class DocumentDetail(DocumentView):
    content: str


def mongo_projection(model: type[Contract], omit: tuple[str, ...] = ()) -> dict[str, int]:
    """Fetch only public DTO fields; keep wire validation as a second boundary."""
    return {"_id": 0} | {name: 1 for name in model.model_fields if name not in omit}


CONVERSATION_PROJECTION = mongo_projection(ConversationView)
TURN_PROJECTION = mongo_projection(TurnView)
DOCUMENT_PROJECTION = mongo_projection(DocumentView)
DOCUMENT_DETAIL_PROJECTION = mongo_projection(DocumentDetail)
LEAD_PROJECTION = (
    mongo_projection(LeadView, ("automation", "notification_created_at"))
    | {f"automation.{name}": 1 for name in AutomationView.model_fields}
    | {"notification.created_at": 1}
)


class Page[Item](Contract):
    items: tuple[Item, ...]
    next_cursor: str | None = None


class Cursor(Contract):
    business: Identifier
    resource: str = Field(max_length=160)
    last: Identifier
    version: int = Field(default=0, ge=0, le=1000000000)


def encode_cursor(business: str, resource: str, last: str, version: int = 0) -> str:
    payload = Cursor(business=business, resource=resource, last=last, version=version)
    return base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode()


def decode_cursor(value: str | None, business: str, resource: str) -> Cursor | None:
    if value is None:
        return None
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        cursor = Cursor.model_validate(json.loads(raw))
        if cursor.business != business or cursor.resource != resource:
            raise ValueError("Cursor scope mismatch")
        return cursor
    except (ValueError, UnicodeError, binascii.Error, ValidationError):
        from fastapi import HTTPException

        raise HTTPException(400) from None


def project[Model: Contract](model: type[Model], record: dict[str, Any]) -> Model:
    return model.model_validate({key: record[key] for key in model.model_fields if key in record})


def lead_view(record: dict[str, Any]) -> LeadView:
    return project(
        LeadView,
        record
        | {
            "automation": project(AutomationView, record["automation"]),
            "notification_created_at": (record.get("notification") or {}).get("created_at"),
        },
    )


router = APIRouter(prefix="/api/v1/businesses/{business_id}", tags=["workspace"])
Limit = Annotated[int, Query(ge=1, le=50)]
CursorQuery = Annotated[str | None, Query(max_length=1024)]


def scope(request: Request, business_id: str) -> dict[str, str]:
    if request.app.state.business_registry.get(business_id) is None:
        raise RecordNotFound("Record not found.")
    return {"business_id": business_id}


@router.get("/conversations", response_model=Page[ConversationView])
async def conversations(
    request: Request, business_id: Identifier, limit: Limit = 20, cursor: CursorQuery = None
) -> Page[ConversationView]:
    key: dict[str, Any] = scope(request, business_id)
    after = decode_cursor(cursor, business_id, "conversations")
    if after:
        key["session_id"] = {"$gt": after.last}
    records = (
        await request.app.state.database.db.sessions.find(key, CONVERSATION_PROJECTION)
        .sort("session_id", 1)
        .limit(limit + 1)
        .to_list()
    )
    return Page[ConversationView](
        items=tuple(project(ConversationView, r) for r in records[:limit]),
        next_cursor=encode_cursor(business_id, "conversations", records[limit - 1]["session_id"])
        if len(records) > limit
        else None,
    )


@router.get("/conversations/{session_id}", response_model=ConversationView)
async def conversation(
    request: Request, business_id: Identifier, session_id: Identifier
) -> ConversationView:
    record = await request.app.state.database.db.sessions.find_one(
        scope(request, business_id) | {"session_id": session_id}, CONVERSATION_PROJECTION
    )
    if record is None:
        raise RecordNotFound("Record not found.")
    return project(ConversationView, record)


@router.get("/conversations/{session_id}/turns", response_model=Page[TurnView])
async def turns(
    request: Request,
    business_id: Identifier,
    session_id: Identifier,
    limit: Limit = 20,
    cursor: CursorQuery = None,
) -> Page[TurnView]:
    await conversation(request, business_id, session_id)
    key: dict[str, Any] = scope(request, business_id) | {"session_id": session_id}
    resource = "turns:" + session_id
    after = decode_cursor(cursor, business_id, resource)
    if after:
        key["sequence"] = {"$gt": after.version}
    records = (
        await request.app.state.database.db.turns.find(key, TURN_PROJECTION)
        .sort("sequence", 1)
        .limit(limit + 1)
        .to_list()
    )
    return Page[TurnView](
        items=tuple(project(TurnView, r) for r in records[:limit]),
        next_cursor=encode_cursor(business_id, resource, "sequence", records[limit - 1]["sequence"])
        if len(records) > limit
        else None,
    )


@router.get("/leads", response_model=Page[LeadView])
async def leads(
    request: Request, business_id: Identifier, limit: Limit = 20, cursor: CursorQuery = None
) -> Page[LeadView]:
    key: dict[str, Any] = scope(request, business_id)
    after = decode_cursor(cursor, business_id, "leads")
    if after:
        key["lead_id"] = {"$gt": after.last}
    records = (
        await request.app.state.database.db.leads.find(key, LEAD_PROJECTION)
        .sort("lead_id", 1)
        .limit(limit + 1)
        .to_list()
    )
    return Page[LeadView](
        items=tuple(lead_view(r) for r in records[:limit]),
        next_cursor=encode_cursor(business_id, "leads", records[limit - 1]["lead_id"])
        if len(records) > limit
        else None,
    )


@router.get("/leads/{lead_id}", response_model=LeadView)
async def lead(request: Request, business_id: Identifier, lead_id: Identifier) -> LeadView:
    record = await request.app.state.database.db.leads.find_one(
        scope(request, business_id) | {"lead_id": lead_id}, LEAD_PROJECTION
    )
    if record is None:
        raise RecordNotFound("Record not found.")
    return lead_view(record)


@router.get("/documents", response_model=Page[DocumentView])
async def documents(
    request: Request, business_id: Identifier, limit: Limit = 20, cursor: CursorQuery = None
) -> Page[DocumentView]:
    key: dict[str, Any] = scope(request, business_id)
    after = decode_cursor(cursor, business_id, "documents")
    if after:
        key["$or"] = [
            {"document_id": {"$gt": after.last}},
            {"document_id": after.last, "version": {"$gt": after.version}},
        ]
    records = (
        await request.app.state.database.db.documents.find(key, DOCUMENT_PROJECTION)
        .sort([("document_id", 1), ("version", 1)])
        .limit(limit + 1)
        .to_list()
    )
    return Page[DocumentView](
        items=tuple(project(DocumentView, r) for r in records[:limit]),
        next_cursor=encode_cursor(
            business_id,
            "documents",
            records[limit - 1]["document_id"],
            records[limit - 1]["version"],
        )
        if len(records) > limit
        else None,
    )


@router.get("/documents/{document_id}/versions/{version}", response_model=DocumentDetail)
async def document(
    request: Request,
    business_id: Identifier,
    document_id: Identifier,
    version: Annotated[int, Path(ge=1, le=10000)],
) -> DocumentDetail:
    record = await request.app.state.database.db.documents.find_one(
        scope(request, business_id) | {"document_id": document_id, "version": version},
        DOCUMENT_DETAIL_PROJECTION,
    )
    if record is None:
        raise RecordNotFound("Record not found.")
    return project(DocumentDetail, record)


@router.post("/documents", response_model=DocumentDetail)
async def add_document(
    request: Request, business_id: Identifier, body: DocumentInput
) -> DocumentDetail:
    scope(request, business_id)
    service = KnowledgeService(
        request.app.state.database,
        request.app.state.business_registry.get(business_id),
        request.app.state.embedder,
        request.app.state.settings,
    )
    record: DocumentRecord = await service.ingest(body)
    return project(DocumentDetail, record.model_dump())
