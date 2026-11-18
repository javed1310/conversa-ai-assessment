import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from app.businesses import BusinessRegistry
from app.contracts import ExecutionContext, NormalizedMessage, ToolFailure, ToolSuccess
from app.database import open_database
from app.repositories import RecordNotFound, Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings
from app.tools import (
    CourseDetailsResult,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    build_tool_registry,
)


def test_catalog_rejects_duplicate_unknown_and_missing_handlers():
    registry = build_tool_registry()
    definition = registry.get("getCourseDetails")
    with pytest.raises(ValueError):
        ToolRegistry((definition, definition))
    with pytest.raises(ValueError, match="presentation"):
        replace(definition, name="deleteEverything")
    with pytest.raises(ValueError):
        replace(definition, handler=None)
    business = BusinessRegistry.load(REPOSITORY_ROOT / "businesses").get("training-institute")
    with pytest.raises(ValueError):
        ToolRegistry((definition,)).validate_business(business)


@pytest_asyncio.fixture
async def tool_environment(storage):
    businesses = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    await seed(storage, REPOSITORY_ROOT / "businesses")
    contexts = {}
    for business in businesses.configurations():
        repo = Repository(storage, business)
        session = await repo.create_session("web", "synthetic-tool-user")
        ctx = ExecutionContext(
            request_id=uuid4(),
            business_id=business.business_id,
            channel="web",
            sender_id=session.sender_id,
            session_id=session.session_id,
            turn_id="pending",
        )
        message = NormalizedMessage(
            message_id="msg-1",
            business_id=business.business_id,
            channel="web",
            sender_id=ctx.sender_id,
            session_id=ctx.session_id,
            content="Synthetic enquiry for tool checks",
            timestamp=datetime.now(UTC),
        )
        turn = await repo.begin_turn(ctx, message)
        contexts[business.business_id] = ctx.model_copy(update={"turn_id": turn.turn_id})
    executor = ToolExecutor(storage, businesses, build_tool_registry())
    return executor, contexts["training-institute"], contexts["fitness-studio"], storage


def call(name, arguments, call_id=None):
    return ToolCall(call_id=call_id or str(uuid4()), name=name, arguments=arguments)


@pytest.mark.integration
class TestLiveTools:
    async def test_search_exact_budget_schedule_and_no_relaxation(self, tool_environment):
        executor, institute, _, _ = tool_environment
        fixtures = [
            ({"category": "data-science", "budget": 30000}, ["DS101", "DS202"]),
            ({"category": "data-science", "budget": 25000}, ["DS101"]),
            ({"category": "data-science", "budget": 20000}, []),
            ({"category": "data-science", "budget": 30000, "schedule": "weekend"}, ["DS202"]),
            ({"category": "python", "budget": 35000}, ["PY101", "PY202"]),
            ({"category": "python", "budget": 35000, "schedule": "weekday"}, ["PY202"]),
            ({"category": "python", "budget": 0}, []),
        ]
        for arguments, expected in fixtures:
            trace = await executor.execute(institute, call("searchCourses", arguments))
            assert isinstance(trace.result, ToolSuccess)
            assert [item["courseId"] for item in trace.result.data["courses"]] == expected
            assert trace.handler_invoked and not trace.replayed

    async def test_details_current_facts_and_missing_records(self, tool_environment):
        executor, institute, _, storage = tool_environment
        trace = await executor.execute(institute, call("getCourseDetails", {"courseID": "DS202"}))
        assert trace.result.data["course"]["fee"] == 28000
        assert trace.result.data["course"]["schedule"] == "weekend"
        assert trace.result.data["course"]["availability"] == "available"
        full = await executor.execute(institute, call("getCourseDetails", {"courseID": "PY202"}))
        assert full.result.data["course"]["availability"] == "full"
        await storage.db.courses.update_one(
            {"business_id": "training-institute", "course_id": "DS202"},
            {"$set": {"availability": "full"}},
        )
        fresh = await executor.execute(institute, call("getCourseDetails", {"courseID": "DS202"}))
        assert fresh.result.data["course"]["availability"] == "full"
        missing = await executor.execute(
            institute, call("getCourseDetails", {"courseID": "NO-SUCH-ID"})
        )
        assert missing.result.code == "NOT_FOUND" and not missing.result.retryable

    async def test_memberships_and_business_specific_exposure(self, tool_environment):
        executor, institute, fitness, _ = tool_environment
        assert {item["name"] for item in await executor.specifications(institute)} == {
            "searchCourses",
            "getCourseDetails",
            "createLead",
            "requestCallback",
        }
        assert {item["name"] for item in await executor.specifications(fitness)} == {
            "getMembershipDetails"
        }
        result = await executor.execute(fitness, call("getMembershipDetails", {}))
        assert len(result.result.data["memberships"]) == 2
        one = await executor.execute(
            fitness, call("getMembershipDetails", {"membershipID": "FIT-MONTHLY"})
        )
        assert one.result.data["memberships"][0]["fee"] == 2000
        assert len(one.result.data["memberships"]) == 1
        missing = await executor.execute(
            fitness, call("getMembershipDetails", {"membershipID": "DS202"})
        )
        assert missing.result.code == "NOT_FOUND"
        forbidden = await executor.execute(institute, call("getMembershipDetails", {}))
        assert forbidden.result.code == "NOT_ALLOWED" and not forbidden.handler_invoked
        foreign = await executor.execute(fitness, call("getCourseDetails", {"courseID": "DS202"}))
        assert foreign.result.code == "NOT_ALLOWED"

    @pytest.mark.parametrize(
        "arguments",
        [
            {"category": "data-science", "budget": True},
            {"category": "data-science", "budget": "30000"},
            {"category": "data-science", "budget": -1},
            {"category": "unknown", "budget": 30000},
            {"category": "python", "budget": 30000, "schedule": "anytime"},
            {"category": "python", "budget": float("inf")},
            {"category": "python", "budget": 30000, "businessId": "fitness-studio"},
            {"category": "python", "budget": 30000, "context": {"senderId": "foreign"}},
        ],
    )
    async def test_invalid_arguments_do_not_execute_or_write(self, tool_environment, arguments):
        executor, institute, _, storage = tool_environment
        denied = await executor.execute(institute, call("searchCourses", arguments))
        assert denied.result.code == "INVALID_ARGUMENTS" and not denied.handler_invoked
        assert await storage.db.tool_executions.count_documents({}) == 0
        assert await storage.db.leads.count_documents({}) == 0

    async def test_forged_context_unknown_tools_and_deferred_writes(self, tool_environment):
        executor, institute, _, storage = tool_environment
        unknown = await executor.execute(institute, call("deleteEverything", {}))
        assert unknown.result.code == "NOT_ALLOWED"
        for change in (
            {"sender_id": "other"},
            {"channel": "mock"},
            {"turn_id": "guessed"},
            {"business_id": "fitness-studio"},
        ):
            context = institute.model_copy(update=change)
            denied = await executor.execute(
                context, call("searchCourses", {"category": "python", "budget": 30000})
            )
            assert denied.result.code in {"NOT_FOUND", "NOT_ALLOWED"} and not denied.handler_invoked
        with pytest.raises(RecordNotFound):
            await executor.specifications(institute.model_copy(update={"sender_id": "other"}))
        deferred = await executor.execute(
            institute,
            call(
                "createLead",
                {"name": "Synthetic", "phone": "9876543210", "statedInterest": "Python"},
            ),
        )
        assert deferred.result.code == "QUALIFICATION_REQUIRED" and not deferred.handler_invoked
        assert await storage.db.tool_executions.count_documents({}) == 0
        assert await storage.db.leads.count_documents({}) == 0

    async def test_trace_replay_and_conflicting_call_identity(self, tool_environment):
        executor, institute, _, storage = tool_environment
        request = call(
            "searchCourses", {"category": "data-science", "budget": 30000}, "call-replay"
        )
        original = await executor.execute(institute, request)
        await storage.db.courses.update_one({"course_id": "DS202"}, {"$set": {"fee": 99999}})
        replay = await executor.execute(institute, request)
        assert replay.execution_id == original.execution_id and replay.result == original.result
        assert replay.replayed and not replay.handler_invoked
        conflict = await executor.execute(
            institute,
            request.model_copy(update={"arguments": {"category": "python", "budget": 30000}}),
        )
        assert conflict.result.code == "CONFLICT"
        assert await storage.db.tool_executions.count_documents({}) == 1

    async def test_parallel_call_claim_and_handler_errors_are_safe(self, tool_environment, caplog):
        executor, institute, _, storage = tool_environment
        base = executor.registry.get("getCourseDetails")
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def slow(repo, context, arguments):
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return await base.handler(repo, context, arguments)

        executor.registry = ToolRegistry((replace(base, handler=slow),))
        request = call("getCourseDetails", {"courseID": "DS202"}, "parallel-call")
        pending = asyncio.create_task(executor.execute(institute, request))
        await started.wait()
        try:
            duplicate = await executor.execute(institute, request)
            assert duplicate.result.code == "IN_PROGRESS" and duplicate.replayed
        finally:
            release.set()
        assert (await pending).result.status == "success" and count == 1

        async def explode(_repo, _context, _arguments):
            raise RuntimeError("sensitive-key-9876543210")

        caplog.set_level(logging.INFO, logger="app.tools")
        executor.registry = ToolRegistry((replace(base, handler=explode),))
        trace = await executor.execute(institute, call("getCourseDetails", {"courseID": "DS202"}))
        assert trace.result.code == "UNAVAILABLE" and trace.result.retryable
        assert "sensitive-key" not in trace.model_dump_json() + caplog.text
        assert "9876543210" not in caplog.text

    async def test_invalid_handler_result_is_not_success(self, tool_environment):
        executor, institute, _, _ = tool_environment
        base = executor.registry.get("getCourseDetails")

        async def bad(_repo, _context, _arguments):
            return CourseDetailsResult.model_construct(course={"secret": "do-not-emit"})

        executor.registry = ToolRegistry((replace(base, handler=bad),))
        trace = await executor.execute(institute, call("getCourseDetails", {"courseID": "DS202"}))
        assert isinstance(trace.result, ToolFailure) and trace.result.code == "INVALID_RESULT"
        assert "do-not-emit" not in trace.model_dump_json()

    async def test_timeout_and_database_failures_are_safe(self, tool_environment):
        executor, institute, _, storage = tool_environment
        base = executor.registry.get("getCourseDetails")

        async def timeout(_repo, _context, _arguments):
            raise TimeoutError("synthetic-private-timeout")

        executor.registry = ToolRegistry((replace(base, handler=timeout),))
        trace = await executor.execute(institute, call("getCourseDetails", {"courseID": "DS202"}))
        assert trace.result.code == "UNAVAILABLE" and trace.result.retryable
        assert "synthetic-private" not in trace.model_dump_json()
        settings = Settings(
            _env_file=None, mongodb_uri=f"mongodb://127.0.0.1:27017/{storage.db.name}"
        )
        async with open_database(settings) as broken:
            await broken.client.close()
            executor.database = broken
            trace = await executor.execute(
                institute, call("getCourseDetails", {"courseID": "DS202"})
            )
            assert trace.result.code == "UNAVAILABLE" and not trace.handler_invoked
