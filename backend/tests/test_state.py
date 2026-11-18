"""Real Mongo state/provenance/reference/CAS tests; no provider requests."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from pymongo.errors import AutoReconnect

from app.businesses import BusinessRegistry
from app.contracts import (
    CustomerChanges,
    CustomerEvidence,
    ExecutionContext,
    NormalizedMessage,
    StateProposal,
)
from app.customer_values import supports_value
from app.generation import EvidencedProposal
from app.repositories import RecordNotFound, Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT
from app.state import StateFailure, StateService, qualification
from app.tools import ToolCall, ToolExecutor, build_tool_registry


@pytest_asyncio.fixture
async def state_env(storage):
    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    business = registry.get("training-institute")
    await seed(storage, REPOSITORY_ROOT / "businesses")
    repo = Repository(storage, business)
    session = await repo.create_session("web", "synthetic-state-customer")
    context = ExecutionContext(
        request_id=uuid4(),
        business_id=business.business_id,
        channel="web",
        sender_id=session.sender_id,
        session_id=session.session_id,
        turn_id="pending",
    )
    return SimpleNamespace(
        storage=storage,
        registry=registry,
        repo=repo,
        context=context,
        service=StateService(repo),
        executor=ToolExecutor(storage, registry, build_tool_registry()),
    )


async def next_turn(env, text):
    message = NormalizedMessage(
        message_id=str(uuid4()),
        business_id=env.context.business_id,
        channel=env.context.channel,
        sender_id=env.context.sender_id,
        session_id=env.context.session_id,
        content=text,
        timestamp=datetime.now(UTC),
    )
    turn = await env.repo.begin_turn(env.context, message)
    return env.context.model_copy(update={"turn_id": turn.turn_id})


def proposal(context, changes=None, clear=(), excerpt=""):
    changes = changes or {}
    return EvidencedProposal(
        proposal=StateProposal(
            set_fields=CustomerChanges.model_validate(changes), clear_fields=clear
        ),
        evidence={
            field: CustomerEvidence(turn_id=context.turn_id, excerpt=excerpt)
            for field in set(changes) | set(clear)
        },
    )


@pytest.mark.parametrize(
    "field,value,excerpt,expected",
    [
        ("budget", 20000, "20k", True),
        ("budget", 20000, "20,000 INR", True),
        ("budget", 0, "budget 0", True),
        ("budget", 20000, "budget 30000", False),
        ("schedule", "weekend", "Weekends", True),
        ("phone", "+919876543210", "+91 98765-43210", True),
        ("phone", "+919876543210", "9876543210999", False),
        ("name", "Aarav", "NotAarav", False),
    ],
)
def test_normalized_values_need_literal_support(field, value, excerpt, expected):
    assert supports_value(field, value, excerpt) is expected


@pytest.mark.integration
class TestPersistentState:
    async def test_incremental_contact_preferences_and_missing_fields(self, state_env):
        env = state_env
        text = "My name is Aarav and phone is 9876543210"
        first = await next_turn(env, text)
        saved = await env.service.apply(
            first, proposal(first, {"name": "Aarav", "phone": "9876543210"}, excerpt=text)
        )
        assert qualification(saved.state).missing == ("course_or_interest",)
        text = "I want Python with a budget of 20k on weekends"
        second = await next_turn(env, text)
        saved = await env.service.apply(
            second,
            proposal(
                second,
                {"stated_interest": "Python", "budget": 20000, "schedule": "weekend"},
                excerpt=text,
            ),
        )
        assert saved.state.fields.name == "Aarav" and saved.state.fields.phone == "+919876543210"
        assert saved.state.fields.budget == 20000 and saved.state_revision == 2
        assert saved.state.provenance["phone"].turn_id == first.turn_id
        assert saved.state.provenance["budget"].turn_id == second.turn_id
        assert qualification(saved.state).can_create_lead
        # Reopening the service proves that values come from the database, not a local dict.
        assert (await StateService(env.repo).view(second)).fields == saved.state.fields

    async def test_30000_to_20000_changes_actual_search(self, state_env):
        env = state_env
        for budget, expected in [(30000, ["DS101", "DS202"]), (20000, [])]:
            text = f"Change my budget to {budget}"
            context = await next_turn(env, text)
            saved = await env.service.apply(
                context, proposal(context, {"budget": budget}, excerpt=text)
            )
            trace = await env.executor.execute(
                context,
                ToolCall(
                    call_id=str(uuid4()),
                    name="searchCourses",
                    arguments={"category": "data-science", "budget": saved.state.fields.budget},
                ),
            )
            assert trace.result.status == "success"
            assert [item["courseId"] for item in trace.result.data["courses"]] == expected

    async def test_explicit_clear_removes_only_requested_value_and_provenance(self, state_env):
        env = state_env
        ctx = await next_turn(env, "Aarav 9876543210")
        await env.service.apply(
            ctx, proposal(ctx, {"name": "Aarav", "phone": "9876543210"}, excerpt="Aarav 9876543210")
        )
        ctx = await next_turn(env, "Please remove my phone")
        saved = await env.service.apply(
            ctx, proposal(ctx, clear=("phone",), excerpt="remove my phone")
        )
        assert saved.state.fields.phone is None and "phone" not in saved.state.provenance
        assert saved.state.fields.name == "Aarav"
        assert "phone" in qualification(saved.state).missing

    @pytest.mark.parametrize(
        "text,excerpt",
        [
            ("What is the phone number?", "phone number"),
            ("Do not remove my phone", "remove my phone"),
        ],
    )
    async def test_clear_without_intent_or_omitted_negation_is_rejected(
        self, state_env, text, excerpt
    ):
        env = state_env
        ctx = await next_turn(env, text)
        with pytest.raises(StateFailure) as error:
            await env.service.apply(ctx, proposal(ctx, clear=("phone",), excerpt=excerpt))
        assert error.value.code == "CLEAR_REQUIRES_INTENT"
        assert (await env.repo.session(ctx)).state_revision == 0

    @pytest.mark.parametrize(
        "case", ["foreign_turn", "absent_excerpt", "invented_value", "missing_evidence"]
    )
    async def test_invalid_provenance_does_not_mutate(self, state_env, case):
        env = state_env
        ctx = await next_turn(env, "My name is Aarav")
        patch = proposal(
            ctx,
            {"name": "Other" if case == "invented_value" else "Aarav"},
            excerpt="Other" if case == "absent_excerpt" else "My name is Aarav",
        )
        if case == "foreign_turn":
            patch = patch.model_copy(
                update={"evidence": {"name": CustomerEvidence(turn_id="foreign", excerpt="Aarav")}}
            )
        if case == "missing_evidence":
            patch = patch.model_copy(update={"evidence": {}})
        with pytest.raises(StateFailure):
            await env.service.apply(ctx, patch)
        assert (await env.repo.session(ctx)).state_revision == 0

    async def test_replayed_patch_applies_once_and_changed_payload_conflicts(self, state_env):
        env = state_env
        ctx = await next_turn(env, "Aarav has a budget of 30000")
        patch = proposal(ctx, {"name": "Aarav"}, excerpt="Aarav")
        results = await asyncio.gather(*(env.service.apply(ctx, patch) for _ in range(6)))
        assert {item.state_revision for item in results} == {1}
        assert len((await env.repo.turn(ctx)).state_checkpoints) == 1
        with pytest.raises(StateFailure) as error:
            await env.service.apply(ctx, proposal(ctx, {"budget": 30000}, excerpt="30000"))
        assert error.value.code == "CHECKPOINT_CONFLICT"

    @pytest.mark.parametrize("fail_after_commit", [False, True])
    async def test_checkpoint_recovers_before_or_after_session_commit(
        self, state_env, monkeypatch, fail_after_commit
    ):
        env = state_env
        ctx = await next_turn(env, "My name is Aarav")
        patch = proposal(ctx, {"name": "Aarav"}, excerpt="Aarav")
        collection_type = type(env.storage.db.sessions)
        original = collection_type.find_one_and_update
        failed = False

        async def interrupted(collection, key, update, *args, **kwargs):
            nonlocal failed
            if collection.name == "sessions" and "state" in update.get("$set", {}) and not failed:
                failed = True
                if fail_after_commit:
                    await original(collection, key, update, *args, **kwargs)
                raise AutoReconnect("synthetic interrupted commit")
            return await original(collection, key, update, *args, **kwargs)

        monkeypatch.setattr(collection_type, "find_one_and_update", interrupted)
        with pytest.raises(AutoReconnect):
            await env.service.apply(ctx, patch)
        assert len((await env.repo.turn(ctx)).state_checkpoints) == 1
        saved = await StateService(env.repo).resume(ctx)
        assert saved.state.fields.name == "Aarav" and saved.state_revision == 1
        assert (await env.service.resume(ctx)).state_revision == 1

    async def test_later_turn_prevents_stale_overwrite(self, state_env):
        env = state_env
        first = await next_turn(env, "budget 30000")
        await env.service.apply(first, proposal(first, {"budget": 30000}, excerpt="30000"))
        second = await next_turn(env, "budget 20000")
        await env.service.apply(second, proposal(second, {"budget": 20000}, excerpt="20000"))
        with pytest.raises(StateFailure) as error:
            await env.service.apply(first, proposal(first, {"budget": 30000}, excerpt="30000"))
        assert error.value.code == "STALE_TURN"
        assert (await env.service.view(second)).fields.budget == 20000

    async def test_display_order_and_second_reference_are_actual_tool_backed(self, state_env):
        env = state_env
        ctx = await next_turn(env, "Show data science courses")
        trace = await env.executor.execute(
            ctx,
            ToolCall(
                call_id="search",
                name="searchCourses",
                arguments={"category": "data-science", "budget": 30000},
            ),
        )
        assert trace.result.status == "success"
        await env.service.record_displayed(ctx, "search", ("DS101", "DS202"))
        ctx = await next_turn(env, "I want the second one")
        resolved = await env.service.resolve_reference(ctx, "the second one")
        assert resolved.course_id == "DS202"
        saved = await env.service.select_reference(ctx, "the second one")
        assert saved.state.fields.course_id == "DS202"
        assert saved.state.provenance["course_id"].turn_id == ctx.turn_id

    async def test_presentation_order_is_preserved_not_resorted(self, state_env):
        env = state_env
        ctx = await next_turn(env, "courses")
        await env.executor.execute(
            ctx,
            ToolCall(
                call_id="search",
                name="searchCourses",
                arguments={"category": "data-science", "budget": 30000},
            ),
        )
        await env.service.record_displayed(ctx, "search", ("DS202", "DS101"))
        ctx = await next_turn(env, "the second one")
        assert (await env.service.resolve_reference(ctx, "second one")).course_id == "DS101"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("the second one", "REFERENCE_UNAVAILABLE"),
            ("first or second", "REFERENCE_AMBIGUOUS"),
            ("not the second one", "REFERENCE_AMBIGUOUS"),
        ],
    )
    async def test_unavailable_or_ambiguous_references_require_clarification(
        self, state_env, text, expected
    ):
        env = state_env
        ctx = await next_turn(env, text)
        with pytest.raises(StateFailure) as error:
            await env.service.resolve_reference(ctx, text)
        assert error.value.code == expected

    async def test_display_cannot_be_invented(self, state_env):
        env = state_env
        ctx = await next_turn(env, "courses")
        with pytest.raises(StateFailure):
            await env.service.record_displayed(ctx, "fake", ("DS202",))
        await env.executor.execute(
            ctx,
            ToolCall(
                call_id="search",
                name="searchCourses",
                arguments={"category": "data-science", "budget": 30000},
            ),
        )
        with pytest.raises(StateFailure):
            await env.service.record_displayed(ctx, "search", ("PY101",))

    async def test_correction_preserves_selection_and_flags_conflicts(self, state_env):
        env = state_env
        ctx = await next_turn(env, "I want DS202 and my budget is 30000 on weekends")
        await env.service.apply(
            ctx,
            proposal(
                ctx,
                {"course_id": "DS202", "budget": 30000, "schedule": "weekend"},
                excerpt="I want DS202 and my budget is 30000 on weekends",
            ),
        )
        ctx = await next_turn(env, "Change my budget to 20000 and schedule to weekdays")
        saved = await env.service.apply(
            ctx,
            proposal(
                ctx,
                {"budget": 20000, "schedule": "weekday"},
                excerpt="Change my budget to 20000 and schedule to weekdays",
            ),
        )
        assert saved.state.fields.course_id == "DS202"
        assert set(saved.state.selection_conflicts) >= {"budget", "schedule"}
        assert qualification(saved.state).needs_clarification
        assert not qualification(saved.state).can_create_lead  # Contact is still required.
        assert set(qualification(saved.state).missing) == {"name", "phone"}
        assert [course.course_id for course in saved.state.last_displayed_courses] == []

    async def test_fresh_record_changes_are_not_hidden_by_old_state(self, state_env):
        env = state_env
        ctx = await next_turn(env, "I want PY101")
        await env.service.apply(ctx, proposal(ctx, {"course_id": "PY101"}, excerpt="PY101"))
        await env.storage.db.courses.update_one(
            {"course_id": "PY101"}, {"$set": {"availability": "full"}}
        )
        assert "full" in (await env.service.view(ctx)).selection_conflicts

    async def test_customer_sessions_and_businesses_are_isolated(self, state_env):
        env = state_env
        ctx = await next_turn(env, "Aarav 9876543210")
        await env.service.apply(
            ctx, proposal(ctx, {"name": "Aarav", "phone": "9876543210"}, excerpt="Aarav 9876543210")
        )
        other = await env.repo.create_session("web", "another-customer")
        foreign = ctx.model_copy(
            update={"session_id": other.session_id, "sender_id": other.sender_id}
        )
        assert (await env.service.view(foreign)).fields.name is None
        with pytest.raises(RecordNotFound):
            await env.service.apply(foreign, proposal(ctx, {"name": "Aarav"}, excerpt="Aarav"))
        fitness = Repository(env.storage, env.registry.get("fitness-studio"))
        with pytest.raises(RecordNotFound):
            await StateService(fitness).view(ctx)

    async def test_callback_authorization_can_be_explicitly_revoked(self, state_env):
        env = state_env
        first = await next_turn(env, "Please call me")
        await env.service.record_callback_intent(first, "call me")
        later = await next_turn(env, "Do not call me")
        saved = await env.service.revoke_callback_intent(later, "Do not call me")
        assert saved.state.callback_intent is None

    async def test_state_correction_does_not_rewrite_saved_lead_snapshot(self, state_env):
        from app.contracts import LeadInput

        env = state_env
        text = "Aarav 9876543210 Python budget 30000"
        ctx = await next_turn(env, text)
        saved = await env.service.apply(
            ctx,
            proposal(
                ctx,
                {
                    "name": "Aarav",
                    "phone": "9876543210",
                    "stated_interest": "Python",
                    "budget": 30000,
                },
                excerpt=text,
            ),
        )
        lead = await env.repo.create_lead(
            ctx, LeadInput.model_validate(saved.state.fields.model_dump())
        )
        later = await next_turn(env, "Change my budget to 20000")
        await env.service.apply(
            later, proposal(later, {"budget": 20000}, excerpt="budget to 20000")
        )
        historical = await env.repo.lead(later, lead.lead_id)
        assert historical.customer.budget == 30000
        assert (await env.service.view(later)).fields.budget == 20000

    async def test_concurrent_different_turns_cannot_silently_overwrite(
        self, state_env, monkeypatch
    ):
        env = state_env
        ready = asyncio.Event()
        entered = 0
        original = env.service._consistent

        async def synchronize(state):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            return await original(state)

        monkeypatch.setattr(env.service, "_consistent", synchronize)
        first = await next_turn(env, "budget 30000")
        second = await next_turn(env, "budget 20000")
        results = await asyncio.gather(
            env.service.apply(first, proposal(first, {"budget": 30000}, excerpt="30000")),
            env.service.apply(second, proposal(second, {"budget": 20000}, excerpt="20000")),
            return_exceptions=True,
        )
        assert sum(isinstance(item, StateFailure) for item in results) >= 1
        assert (await env.repo.session(second)).state_revision == 1

    @pytest.mark.parametrize(
        "text,valid",
        [("Please call me", True), ("Do not call me", False), ("What are your hours?", False)],
    )
    async def test_callback_intent_has_actual_customer_evidence(self, state_env, text, valid):
        env = state_env
        ctx = await next_turn(env, text)
        if valid:
            saved = await env.service.record_callback_intent(ctx, "call me")
            assert saved.state.callback_intent.evidence.turn_id == ctx.turn_id
        else:
            with pytest.raises(StateFailure):
                await env.service.record_callback_intent(
                    ctx, "call me" if "call me" in text else text
                )


def test_null_and_invalid_scalars_never_clear_or_create_fields():
    for value in [
        {"phone": None},
        {"budget": True},
        {"budget": float("nan")},
        {"schedule": "holiday"},
    ]:
        with pytest.raises(ValidationError):
            CustomerChanges.model_validate(value)
