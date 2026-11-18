"""Real-Mongo query-boundary regressions: filter equivalence, stable order and public DTOs."""

import pytest
from pymongo.asynchronous.collection import AsyncCollection

from app.automation import AutomationWorker
from app.businesses import BusinessRegistry
from app.contracts import SearchCoursesArguments
from app.inspection import (
    CONVERSATION_PROJECTION,
    DOCUMENT_DETAIL_PROJECTION,
    DOCUMENT_PROJECTION,
    LEAD_PROJECTION,
    TURN_PROJECTION,
)
from app.repositories import Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT
from tests.test_action_agent import intake
from tests.test_agent import agent_env as agent_env
from tests.test_automation import TestPlanner
from tests.test_workspace import BASE, document
from tests.test_workspace import workspace as workspace

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "category,budget,schedule",
    [
        ("data-science", 30000, None),
        ("data-science", 30000, "weekend"),
        ("data-science", 25000, None),
        ("data-science", 0, None),
        ("python", 35000, None),
        ("python", 35000, "weekday"),
        ("python", 12000, "weekend"),
    ],
)
async def test_mongo_course_filter_matches_original_filter_and_sort(
    storage, category, budget, schedule
):
    await seed(storage, REPOSITORY_ROOT / "businesses")
    business = BusinessRegistry.load(REPOSITORY_ROOT / "businesses").get("training-institute")
    repo = Repository(storage, business)
    all_courses = await repo.courses()
    expected = tuple(
        c
        for c in all_courses
        if c.category == category
        and c.fee <= budget
        and (schedule is None or c.schedule == schedule)
    )
    actual = await repo.search_courses(
        SearchCoursesArguments(category=category, budget=budget, schedule=schedule)
    )
    assert actual == expected


async def test_search_preserves_fee_ties_full_courses_and_foreign_scope(storage):
    await seed(storage, REPOSITORY_ROOT / "businesses")
    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    repo = Repository(storage, registry.get("training-institute"))
    tied = (await repo.course("DS202")).model_dump() | {"course_id": "AA001"}
    await storage.db.courses.insert_many(
        [tied, tied | {"business_id": "fitness-studio", "course_id": "FOREIGN", "fee": 0}]
    )
    actual = await repo.search_courses(
        SearchCoursesArguments(category="data-science", budget=30000)
    )
    assert [c.course_id for c in actual] == ["DS101", "AA001", "DS202"]
    full = await repo.search_courses(
        SearchCoursesArguments(category="python", budget=35000, schedule="weekday")
    )
    assert len(full) == 1 and full[0].course_id == "PY202" and full[0].availability == "full"


async def test_search_sends_validated_filters_to_mongo_instead_of_loading_the_catalog(
    storage, monkeypatch
):
    await seed(storage, REPOSITORY_ROOT / "businesses")
    repo = Repository(
        storage, BusinessRegistry.load(REPOSITORY_ROOT / "businesses").get("training-institute")
    )
    original = AsyncCollection.find
    queries = []

    def find(collection, query=None, *args, **kwargs):
        if collection.name == "courses":
            queries.append(query)
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(AsyncCollection, "find", find)
    await repo.search_courses(
        SearchCoursesArguments(category="data-science", budget=30000, schedule="weekend")
    )
    assert queries == [
        {
            "business_id": "training-institute",
            "category": "data-science",
            "fee": {"$lte": 30000},
            "schedule": "weekend",
        }
    ]


async def test_all_inspection_reads_apply_driver_projections(workspace, monkeypatch):
    client, env, _, _ = workspace
    await intake(env)
    saved = await client.post(BASE + "/documents", json=document())
    assert saved.status_code == 200
    lead = await env.storage.db.leads.find_one({})
    seen = []
    original = AsyncCollection.find

    def find(collection, query=None, *args, **kwargs):
        if collection.name in {"sessions", "turns", "leads", "documents"}:
            projection = args[0] if args else kwargs.get("projection")
            seen.append((collection.name, projection))
        return original(collection, query, *args, **kwargs)

    monkeypatch.setattr(AsyncCollection, "find", find)
    for suffix in [
        "/conversations",
        f"/conversations/{env.context.session_id}",
        f"/conversations/{env.context.session_id}/turns",
        "/leads",
        f"/leads/{lead['lead_id']}",
        "/documents",
        "/documents/new-policy/versions/1",
    ]:
        assert (await client.get(BASE + suffix)).status_code == 200
    assert seen
    assert all(projection is not None and projection.get("_id") == 0 for _, projection in seen)
    assert all(
        projection == CONVERSATION_PROJECTION for name, projection in seen if name == "sessions"
    )
    assert all(projection == TURN_PROJECTION for name, projection in seen if name == "turns")
    assert all(projection == LEAD_PROJECTION for name, projection in seen if name == "leads")
    documents = [projection for name, projection in seen if name == "documents"]
    assert DOCUMENT_PROJECTION in documents and DOCUMENT_DETAIL_PROJECTION in documents


async def test_nested_lead_summary_and_notification_survive_without_private_automation_fields(
    workspace,
):
    client, env, _, settings = workspace
    await intake(env)
    assert await AutomationWorker(env.storage, env.registry, TestPlanner(), settings).tick()
    before = (await client.get(BASE + "/leads")).json()
    assert before["items"][0]["automation"]["summary"] is not None
    assert before["items"][0]["notificationCreatedAt"] is not None
    await env.storage.db.leads.update_one(
        {},
        {
            "$set": {
                "automation.claim_token": "private-fixture-not-a-real-secret",
                "automation.internal_fixture": "x" * 30000,
            }
        },
    )
    after = (await client.get(BASE + "/leads")).json()
    assert before == after
    assert "private-fixture" not in str(after) and "internal_fixture" not in str(after)


async def test_document_list_stays_metadata_only_while_detail_returns_content(workspace):
    client, _, _, _ = workspace
    body = document()
    assert (await client.post(BASE + "/documents", json=body)).status_code == 200
    listed = (await client.get(BASE + "/documents")).json()
    detail = (await client.get(BASE + "/documents/new-policy/versions/1")).json()
    assert all("content" not in item for item in listed["items"])
    assert detail["content"] == body["content"]
    assert detail["status"] == "ready"
