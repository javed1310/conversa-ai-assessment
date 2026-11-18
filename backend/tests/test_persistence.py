"""Real MongoDB integration, isolated per-test databases, never mocked persistence."""

import asyncio
import json
import shutil
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pymongo.errors import AutoReconnect, DuplicateKeyError, OperationFailure

from app.businesses import BusinessRegistry
from app.contracts import (
    CallbackIntent,
    ChatResult,
    CustomerEvidence,
    ExecutionContext,
    LeadInput,
    NormalizedMessage,
    Summary,
)
from app.database import DatabaseStartupError, ensure_indexes, open_database
from app.main import create_app
from app.repositories import RecordConflict, RecordNotFound, Repository, stored
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings

pytestmark = pytest.mark.integration


def repositories(database):
    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    return tuple(
        Repository(database, registry.get(business))
        for business in ("training-institute", "fitness-studio")
    )


def context(session, turn_id="pending"):
    return ExecutionContext(
        request_id=uuid4(),
        business_id=session.business_id,
        session_id=session.session_id,
        channel=session.channel,
        sender_id=session.sender_id,
        turn_id=turn_id,
    )


async def turn_for(repo, session, message_id="msg-1", content="Please call me about Python"):
    ctx = context(session)
    message = NormalizedMessage(
        message_id=message_id,
        business_id=session.business_id,
        channel=session.channel,
        sender_id=session.sender_id,
        session_id=session.session_id,
        content=content,
        timestamp=datetime.now(UTC),
    )
    turn = await repo.begin_turn(ctx, message)
    return ctx.model_copy(update={"turn_id": turn.turn_id}), turn, message


async def test_live_database_seed_repeat_exact_records_and_scoping(storage):
    assert await storage.ready()
    counts = await seed(storage, REPOSITORY_ROOT / "businesses")
    assert counts == {"courses": 4, "memberships": 2}
    assert await seed(storage, REPOSITORY_ROOT / "businesses") == counts
    institute, fitness = repositories(storage)
    actual = {
        c.course_id: (c.name, c.fee, c.schedule, c.availability) for c in await institute.courses()
    }
    assert actual == {
        "DS101": ("Data Science Foundations", 25000, "weekday", "available"),
        "DS202": ("Applied Data Analytics", 28000, "weekend", "available"),
        "PY101": ("Python Basics", 12000, "weekend", "available"),
        "PY202": ("Advanced Python", 35000, "weekday", "full"),
    }
    assert [c.course_id for c in await institute.courses()] == ["PY101", "DS101", "DS202", "PY202"]
    assert {c.course_id: c.category for c in await institute.courses()} == {
        "DS101": "data-science",
        "DS202": "data-science",
        "PY101": "python",
        "PY202": "python",
    }
    assert all(c.currency == "INR" for c in await institute.courses())
    assert len(await fitness.memberships()) == 2
    assert {
        m.membership_id: (m.fee, m.duration_months, m.fictional)
        for m in await fitness.memberships()
    } == {"FIT-MONTHLY": (2000, 1, True), "FIT-QUARTERLY": (5400, 3, True)}
    assert not await fitness.courses() and not await institute.memberships()
    with pytest.raises(RecordNotFound):
        await fitness.course("DS202")
    with pytest.raises(RecordNotFound):
        await institute.membership("FIT-MONTHLY")
    # Seeding must not silently reset a later operational availability change.
    await storage.db.courses.update_one(
        {"business_id": "training-institute", "course_id": "DS202"},
        {"$set": {"availability": "full"}},
    )
    await seed(storage, REPOSITORY_ROOT / "businesses")
    assert (await institute.course("DS202")).availability == "full"


async def test_sessions_turns_scope_order_and_replay(storage):
    institute, fitness = repositories(storage)
    first = await institute.create_session("web", "visitor-1")
    second = await institute.create_session("web", "visitor-2")
    ctx, turn, message = await turn_for(institute, first)
    assert turn.status == "processing" and turn.output is None
    assert (await institute.history(ctx))[0].input_text == message.content
    for foreign in [
        ctx.model_copy(update={"business_id": "fitness-studio"}),
        ctx.model_copy(update={"sender_id": "visitor-2"}),
        ctx.model_copy(update={"channel": "mock"}),
        ctx.model_copy(update={"session_id": second.session_id}),
    ]:
        with pytest.raises(RecordNotFound):
            await institute.history(foreign)
    with pytest.raises(RecordNotFound):
        await fitness.session(ctx)
    assert not await institute.history(context(second))
    duplicates = await asyncio.gather(*(institute.begin_turn(ctx, message) for _ in range(8)))
    assert {result.turn_id for result in duplicates} == {turn.turn_id}
    with pytest.raises(RecordConflict):
        await institute.begin_turn(ctx, message.model_copy(update={"content": "Different content"}))
    output = ChatResult(
        message_id=turn.message_id, session_id=first.session_id, reply="Test output"
    )
    finished = await institute.complete_turn(ctx, output)
    assert finished.status == "completed" and finished.output == output
    assert (await institute.complete_turn(ctx, output)).completed_at == finished.completed_at
    with pytest.raises(RecordConflict):
        await institute.complete_turn(ctx, output.model_copy(update={"reply": "Different output"}))
    await asyncio.gather(*(turn_for(institute, first, f"parallel-{i}") for i in range(8)))
    history = await institute.history(ctx)
    assert len(history) == 9 and len({item.sequence for item in history}) == 9
    assert [item.sequence for item in history] == sorted(item.sequence for item in history)
    # Reopen using a new client to prove records are not an in-memory repository.
    uri = f"mongodb://127.0.0.1:27017/{storage.db.name}"
    async with open_database(Settings(_env_file=None, mongodb_uri=uri)) as reopened:
        again, _ = repositories(reopened)
        assert len(await again.history(ctx)) == 9


async def test_atomic_lead_pending_event_and_unique_conditional_slots(storage):
    await seed(storage, REPOSITORY_ROOT / "businesses")
    institute, fitness = repositories(storage)
    session = await institute.create_session("web", "visitor-1")
    ctx, turn, _ = await turn_for(institute, session)
    customer = LeadInput(name="Synthetic Customer", phone="9876543210", course_id="DS202")
    leads = await asyncio.gather(*(institute.create_lead(ctx, customer) for _ in range(8)))
    assert len({lead.lead_id for lead in leads}) == 1
    lead = leads[0]
    document = await storage.db.leads.find_one({"lead_id": lead.lead_id})
    assert document["automation"]["status"] == "pending"
    assert document["customer"]["phone"] == "+919876543210"
    assert document["automation_input"][0]["content"] == turn.input_text
    original_input = document["automation_input"]
    with pytest.raises(RecordNotFound):
        await fitness.lead(ctx, lead.lead_id)
    stranger = await institute.create_session("web", "other")
    with pytest.raises(RecordNotFound):
        await institute.lead(context(stranger), lead.lead_id)
    event_id = lead.automation.event_id
    with pytest.raises(RecordConflict):
        await institute.save_notification(lead.lead_id, event_id)
    intent = CallbackIntent(
        requested=True, evidence=CustomerEvidence(turn_id=turn.turn_id, excerpt="Please call me")
    )
    callbacks = await asyncio.gather(
        *(institute.request_callback(ctx, lead.lead_id, intent) for _ in range(8))
    )
    assert len({item.callback_requested_at for item in callbacks}) == 1
    summary = Summary(
        lead_id=lead.lead_id,
        customer_name=customer.name,
        summary="Synthetic customer requested a course callback.",
        intent="course enquiry",
        qualification_status="qualified",
        recommended_action="Call customer",
    )
    await asyncio.gather(
        *(institute.save_summary(lead.lead_id, event_id, summary) for _ in range(8))
    )
    notifications = await asyncio.gather(
        *(institute.save_notification(lead.lead_id, event_id) for _ in range(8))
    )
    assert len({item.notification.notification_id for item in notifications}) == 1
    assert all(item.automation.status == "completed" for item in notifications)
    with pytest.raises(RecordNotFound):
        await fitness.save_summary(lead.lead_id, event_id, summary)
    with pytest.raises(RecordNotFound):
        await institute.save_summary(lead.lead_id, "foreign-event", summary)
    again = await institute.create_lead(ctx, customer.model_copy(update={"budget": 20000}))
    assert again.customer.budget is None  # Saved snapshot is immutable, no silent correction.
    document = await storage.db.leads.find_one({"lead_id": lead.lead_id})
    assert document["automation_input"] == original_input
    assert await storage.db.leads.count_documents({}) == 1


async def test_indexes_unique_and_conflicting_index_fails(storage):
    institute, _ = repositories(storage)
    session = await institute.create_session("web", "visitor")
    with pytest.raises(DuplicateKeyError):
        await storage.db.sessions.insert_one(stored(session))
    await storage.db.courses.insert_one({"business_id": "b", "course_id": "c"})
    with pytest.raises(DuplicateKeyError):
        await storage.db.courses.insert_one({"business_id": "b", "course_id": "c"})
    for collection, identity in {
        "external_mappings": {"business_id": "b", "channel": "mock", "sender_id": "s"},
        "documents": {"business_id": "b", "document_id": "d", "version": 1},
        "chunks": {"business_id": "b", "document_id": "d", "version": 1, "chunk_id": "c"},
    }.items():
        await storage.db[collection].insert_one(dict(identity))
        with pytest.raises(DuplicateKeyError):
            await storage.db[collection].insert_one(dict(identity))
    # An incompatible existing protection is an explicit startup failure, never ignored.
    await storage.db.memberships.drop_index("uq_business_id_membership_id")
    await storage.db.memberships.create_index(
        [("business_id", 1), ("membership_id", 1)],
        name="uq_business_id_membership_id",
        unique=False,
    )
    with pytest.raises(OperationFailure):
        await ensure_indexes(storage.db)
    with pytest.raises(DatabaseStartupError):
        async with open_database(
            Settings(_env_file=None, mongodb_uri=(f"mongodb://127.0.0.1:27017/{storage.db.name}"))
        ):
            pytest.fail("Conflicting index must prevent startup")


async def test_actual_app_readiness_and_database_outage(storage, monkeypatch):
    monkeypatch.setattr("app.main.open_database", open_database)
    settings = Settings(_env_file=None, mongodb_uri=f"mongodb://127.0.0.1:27017/{storage.db.name}")
    app = create_app(settings)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/v1/health/ready")).status_code == 200
            # A real closed client simulates unusable dependency without stopping shared Mongo.
            await app.state.database.client.close()
            unavailable = await client.get("/api/v1/health/ready")
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
            assert "mongodb://" not in unavailable.text
            assert (await client.get("/api/v1/health/live")).status_code == 200
    assert app.state.database is None


async def test_invalid_seed_owner_rejects_whole_batch_before_writes(storage, tmp_path):
    target = tmp_path / "businesses"
    shutil.copytree(REPOSITORY_ROOT / "businesses", target)
    path = target / "fitness-studio/data/memberships.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values[0]["businessId"] = "training-institute"
    path.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError):
        await seed(storage, target)
    assert await storage.db.courses.count_documents({}) == 0
    assert await storage.db.memberships.count_documents({}) == 0


async def test_failed_lead_insert_has_no_orphan_event(storage, monkeypatch):
    institute, _ = repositories(storage)
    session = await institute.create_session("web", "synthetic-failure")
    ctx, _, _ = await turn_for(institute, session)

    async def fail_insert(_self, _document, **_kwargs):
        raise AutoReconnect("Synthetic pre-write failure")

    with monkeypatch.context() as patch:
        patch.setattr(type(storage.db.leads), "insert_one", fail_insert)
        with pytest.raises(AutoReconnect):
            await institute.create_lead(
                ctx, LeadInput(name="Synthetic", phone="9876543210", stated_interest="Python")
            )
    assert await storage.db.leads.count_documents({}) == 0
    assert "events" not in await storage.db.list_collection_names()


async def test_unavailable_database_startup_is_safe_and_bounded():
    settings = Settings(
        _env_file=None, mongodb_uri="mongodb://127.0.0.1:1/ai_engagement_unavailable"
    )
    with pytest.raises(DatabaseStartupError) as error:
        async with open_database(settings):
            pytest.fail("Unavailable database cannot pass startup")
    assert "mongodb://" not in str(error.value)


async def test_first_delivery_race_and_bounded_immutable_history(storage):
    institute, _ = repositories(storage)
    session = await institute.create_session("web", "same-delivery")
    ctx = context(session)
    message = NormalizedMessage(
        message_id="first-race",
        business_id=session.business_id,
        channel="web",
        sender_id=session.sender_id,
        session_id=session.session_id,
        content="Same delivery",
        timestamp=datetime.now(UTC),
    )
    turns = await asyncio.gather(*(institute.begin_turn(ctx, message) for _ in range(12)))
    assert len({turn.turn_id for turn in turns}) == 1
    assert len(await institute.history(ctx)) == 1
    for i in range(26):
        ctx, _, _ = await turn_for(institute, session, f"later-{i}", f"Synthetic turn {i}")
    lead = await institute.create_lead(
        ctx, LeadInput(name="Synthetic", phone="9876543210", stated_interest="Python")
    )
    assert len(lead.automation_input) == 25
    assert lead.automation_input[0].content == "Synthetic turn 1"
    assert lead.automation_input[-1].content == "Synthetic turn 25"


async def test_ambiguous_insert_retry_reconciles_saved_lead(storage, monkeypatch):
    institute, _ = repositories(storage)
    session = await institute.create_session("web", "ambiguous-write")
    ctx, _, _ = await turn_for(institute, session)
    customer = LeadInput(name="Synthetic", phone="9876543210", stated_interest="Python")
    collection_type = type(storage.db.leads)
    actual_insert = collection_type.insert_one

    async def saved_then_disconnected(collection, document, **kwargs):
        await actual_insert(collection, document, **kwargs)
        raise AutoReconnect("Synthetic lost acknowledgement after successful save")

    with monkeypatch.context() as patch:
        patch.setattr(collection_type, "insert_one", saved_then_disconnected)
        reconciled = await institute.create_lead(ctx, customer)
    saved = await institute.lead_for_session(ctx)
    assert reconciled.lead_id == saved.lead_id
    retried = await institute.create_lead(ctx, customer)
    assert retried.lead_id == saved.lead_id
    assert retried.automation.event_id == saved.automation.event_id
    assert await storage.db.leads.count_documents({}) == 1
