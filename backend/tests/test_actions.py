"""Actual MongoDB write authorization, idempotency, partial success and acknowledgement loss."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from pymongo.errors import AutoReconnect

from app.actions import callback_request
from app.contracts import LeadInput
from app.repositories import RecordNotFound
from app.tools import ToolCall, ToolExecutor, ToolRegistry
from tests.test_state import next_turn, proposal
from tests.test_state import state_env as state_env


async def qualify(env, *, callback=False, general=False):
    text = "My name is Rahul, my phone is 9876543210 and I want data science."
    changes = {"name": "Rahul", "phone": "9876543210", "stated_interest": "data science"}
    if not general:
        text += " I select DS202 with budget 30000 on weekends."
        changes.update(course_id="DS202", budget=30000, schedule="weekend")
    if callback:
        text += " Please call me."
    context = await next_turn(env, text)
    saved = await env.service.apply(context, proposal(context, changes, excerpt=text))
    if callback:
        await env.service.record_callback_intent(context, text)
    customer = LeadInput.model_validate(saved.state.fields.model_dump())
    return context, customer


def create(customer, call_id=None):
    return ToolCall(
        name="createLead",
        call_id=call_id or str(uuid4()),
        arguments=customer.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def callback(lead_id, call_id=None):
    return ToolCall(
        name="requestCallback", call_id=call_id or str(uuid4()), arguments={"leadID": lead_id}
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Please call me", True),
        ("I would like a callback", True),
        ("Please arrange a callback about DS202", True),
        ("Please request the callback again", True),
        ("What is a callback?", False),
        ("How do I request a callback?", False),
        ("Please explain how to request a callback", False),
        ("Do not call me", False),
        ('Explain the phrase "please call me"', False),
    ],
)
def test_callback_questions_and_quoted_text_are_not_permission(text, expected):
    assert callback_request(text) is expected


@pytest.mark.integration
class TestBusinessActions:
    async def test_general_interest_qualifies_without_budget_schedule_or_course(self, state_env):
        env = state_env
        ctx, customer = await qualify(env, general=True)
        action = await env.executor.execute(ctx, create(customer))
        assert action.result.status == "success"
        lead = await env.repo.lead(ctx, action.result.data["leadId"])
        assert lead.customer.name == "Rahul" and lead.customer.phone == "+919876543210"
        assert lead.customer.course_id is None and lead.customer.budget is None
        assert lead.automation.status == "pending" and lead.automation.summary is None
        assert lead.automation_input[-1].turn_id == ctx.turn_id

    async def test_model_arguments_cannot_supply_customer_facts(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        forged = customer.model_copy(update={"name": "Invented"})
        action = await env.executor.execute(ctx, create(forged))
        assert action.result.code == "INVALID_PROVENANCE" and not action.handler_invoked
        assert await env.storage.db.leads.count_documents({}) == 0

    async def test_missing_phone_blocks_a_syntactically_valid_proposed_lead(self, state_env):
        env = state_env
        ctx = await next_turn(env, "My name is Rahul and I want data science")
        await env.service.apply(
            ctx,
            proposal(
                ctx,
                {"name": "Rahul", "stated_interest": "data science"},
                excerpt="My name is Rahul and I want data science",
            ),
        )
        action = await env.executor.execute(
            ctx, create(LeadInput(name="Rahul", phone="9876543210", stated_interest="data science"))
        )
        assert action.result.code == "QUALIFICATION_REQUIRED" and not action.handler_invoked

    async def test_known_optional_preferences_are_saved_even_when_model_omits_them(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        minimal = LeadInput(
            name=customer.name, phone=customer.phone, stated_interest=customer.stated_interest
        )
        action = await env.executor.execute(ctx, create(minimal))
        lead = await env.repo.lead(ctx, action.result.data["leadId"])
        assert lead.customer.budget == 30000 and lead.customer.course_id == "DS202"
        assert lead.customer.schedule == "weekend"

    async def test_duplicate_calls_new_turn_intent_and_concurrent_calls_share_one_lead(
        self, state_env
    ):
        env = state_env
        ctx, customer = await qualify(env)
        outcomes = await asyncio.gather(
            *(env.executor.execute(ctx, create(customer)) for _ in range(6))
        )
        ids = {item.result.data["leadId"] for item in outcomes if item.result.status == "success"}
        assert len(ids) == 1 and all(item.result.status == "success" for item in outcomes)
        later = await next_turn(env, "Please save the same enquiry again")
        repeated = await env.executor.execute(later, create(customer))
        assert repeated.result.data["leadId"] in ids
        assert await env.storage.db.leads.count_documents({}) == 1

    async def test_same_call_replays_and_changed_arguments_conflict(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        request = create(customer, "same-call")
        first = await env.executor.execute(ctx, request)
        again = await env.executor.execute(ctx, request)
        assert again.result == first.result and again.replayed and not again.handler_invoked
        changed = create(customer.model_copy(update={"budget": 20000}), "same-call")
        assert (await env.executor.execute(ctx, changed)).result.code == "CONFLICT"

    async def test_callback_requires_actual_owned_lead_and_customer_intent(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        lead = await env.executor.execute(ctx, create(customer))
        missing_intent = await env.executor.execute(ctx, callback(lead.result.data["leadId"]))
        assert missing_intent.result.code == "CALLBACK_INTENT_REQUIRED"
        assert not missing_intent.handler_invoked
        absent = await env.executor.execute(ctx, callback("guessed-lead"))
        assert absent.result.code == "NOT_FOUND" and not absent.handler_invoked

    async def test_callback_receipt_is_persisted_and_stable_across_repeated_requests(
        self, state_env
    ):
        env = state_env
        ctx, customer = await qualify(env, callback=True)
        lead_result = await env.executor.execute(ctx, create(customer))
        lead_id = lead_result.result.data["leadId"]
        results = await asyncio.gather(
            *(env.executor.execute(ctx, callback(lead_id)) for _ in range(5))
        )
        ids = {result.result.data["requestId"] for result in results}
        assert len(ids) == 1 and all(result.result.data["requested"] for result in results)
        lead = await env.repo.lead(ctx, lead_id)
        assert lead.callback_request_id in ids and lead.callback_requested_at is not None
        later = await next_turn(env, "Please call me again about this enquiry")
        await env.service.record_callback_intent(later, "Please call me again about this enquiry")
        again = await env.executor.execute(later, callback(lead_id))
        assert again.result.data["requestId"] == lead.callback_request_id
        assert lead.automation.event_id == (await env.repo.lead(later, lead_id)).automation.event_id

    async def test_revoked_callback_is_not_requested(self, state_env):
        env = state_env
        ctx, customer = await qualify(env, callback=True)
        saved = await env.executor.execute(ctx, create(customer))
        later = await next_turn(env, "Do not call me")
        await env.service.revoke_callback_intent(later, "Do not call me")
        denied = await env.executor.execute(later, callback(saved.result.data["leadId"]))
        assert denied.result.code == "CALLBACK_INTENT_REQUIRED"
        assert (
            await env.repo.lead(later, saved.result.data["leadId"])
        ).callback_requested_at is None

    async def test_after_save_corrections_preserve_original_snapshot_and_block_silent_retarget(
        self, state_env
    ):
        env = state_env
        ctx, customer = await qualify(env, callback=True)
        saved = await env.executor.execute(ctx, create(customer))
        lead_id = saved.result.data["leadId"]
        later = await next_turn(env, "Change my phone to 9876543211")
        await env.service.apply(
            later, proposal(later, {"phone": "9876543211"}, excerpt="9876543211")
        )
        retained = await env.executor.execute(
            later, create(customer.model_copy(update={"phone": "+919876543211"}))
        )
        assert (
            retained.result.data["leadId"] == lead_id
            and retained.result.data["activeDetailsDiffer"]
        )
        denied = await env.executor.execute(later, callback(lead_id))
        assert denied.result.code == "SAVED_DETAILS_CHANGED"
        assert (await env.repo.lead(later, lead_id)).customer.phone == "+919876543210"

    async def test_lost_lead_insert_acknowledgement_is_reconciled(self, state_env, monkeypatch):
        env = state_env
        ctx, customer = await qualify(env)
        original = type(env.storage.db.leads).insert_one
        dropped = False

        async def lose_ack(collection, document, *args, **kwargs):
            nonlocal dropped
            result = await original(collection, document, *args, **kwargs)
            if collection.name == "leads" and not dropped:
                dropped = True
                raise AutoReconnect("synthetic acknowledgement loss")
            return result

        monkeypatch.setattr(type(env.storage.db.leads), "insert_one", lose_ack)
        result = await env.executor.execute(ctx, create(customer))
        assert result.result.status == "success"
        assert await env.storage.db.leads.count_documents({}) == 1

    async def test_uncertain_uncommitted_insert_can_retry_without_duplicates(
        self, state_env, monkeypatch
    ):
        env = state_env
        ctx, customer = await qualify(env)
        original = type(env.storage.db.leads).insert_one
        failed = False

        async def interrupted(collection, document, *args, **kwargs):
            nonlocal failed
            if collection.name == "leads" and not failed:
                failed = True
                raise AutoReconnect("synthetic interruption before acknowledgement")
            return await original(collection, document, *args, **kwargs)

        monkeypatch.setattr(type(env.storage.db.leads), "insert_one", interrupted)
        request = create(customer, "uncertain")
        first = await env.executor.execute(ctx, request)
        assert first.result.code == "OUTCOME_UNKNOWN"
        assert (await env.storage.db.tool_executions.find_one({"call_id": "uncertain"}))[
            "status"
        ] == "uncertain"
        second = await env.executor.execute(ctx, request)
        assert second.result.status == "success" and second.replayed
        assert await env.storage.db.leads.count_documents({}) == 1

    async def test_callback_commit_with_lost_ack_is_reconciled(self, state_env, monkeypatch):
        env = state_env
        ctx, customer = await qualify(env, callback=True)
        lead = await env.executor.execute(ctx, create(customer))
        original = type(env.storage.db.leads).update_one
        dropped = False

        async def lose_ack(collection, key, update, *args, **kwargs):
            nonlocal dropped
            result = await original(collection, key, update, *args, **kwargs)
            if (
                collection.name == "leads"
                and "callback_requested_at" in update.get("$set", {})
                and not dropped
            ):
                dropped = True
                raise AutoReconnect("synthetic callback acknowledgement loss")
            return result

        monkeypatch.setattr(type(env.storage.db.leads), "update_one", lose_ack)
        request = callback(lead.result.data["leadId"], "callback-lost-ack")
        result = await env.executor.execute(ctx, request)
        assert result.result.status == "success" and result.result.data["requestId"]
        assert (await env.executor.execute(ctx, request)).result.data == result.result.data

    async def test_post_commit_handler_exception_does_not_report_lead_as_unsaved(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        original = env.executor.registry.get("createLead")

        async def committed_then_failed(repo, context, arguments):
            await original.handler(repo, context, arguments)
            raise RuntimeError("synthetic post-commit error")

        definitions = tuple(
            replace(item, handler=committed_then_failed) if item.name == "createLead" else item
            for item in env.executor.registry._definitions.values()
        )
        executor = ToolExecutor(env.storage, env.registry, ToolRegistry(definitions))
        outcome = await executor.execute(ctx, create(customer))
        assert outcome.result.status == "success"

    async def test_another_session_cannot_request_or_read_the_lead(self, state_env):
        env = state_env
        ctx, customer = await qualify(env, callback=True)
        result = await env.executor.execute(ctx, create(customer))
        other = await env.repo.create_session("web", "foreign-customer")
        foreign = ctx.model_copy(
            update={"session_id": other.session_id, "sender_id": other.sender_id}
        )
        with pytest.raises(RecordNotFound):
            await env.repo.lead(foreign, result.result.data["leadId"])
        denied = await env.executor.execute(foreign, callback(result.result.data["leadId"]))
        assert denied.result.code == "NOT_FOUND" and not denied.handler_invoked

    async def test_interrupted_post_commit_execution_is_reconciled_without_handler_replay(
        self, state_env
    ):
        env = state_env
        ctx, customer = await qualify(env)
        original = env.executor.registry.get("createLead")
        committed = asyncio.Event()

        async def suspended(repo, context, arguments):
            result = await original.handler(repo, context, arguments)
            committed.set()
            await asyncio.Event().wait()
            return result

        registry = ToolRegistry(
            tuple(
                replace(item, handler=suspended) if item.name == "createLead" else item
                for item in env.executor.registry._definitions.values()
            )
        )
        executor = ToolExecutor(env.storage, env.registry, registry)
        request = create(customer, "interrupted")
        task = asyncio.create_task(executor.execute(ctx, request))
        await asyncio.wait_for(committed.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        recovered = await executor.execute(ctx, request)
        assert recovered.result.status == "success" and recovered.replayed
        assert not recovered.handler_invoked
        assert await env.storage.db.leads.count_documents({}) == 1

    async def test_inflight_duplicate_without_proof_does_not_invoke_handler_twice(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        original = env.executor.registry.get("createLead")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def waiting(repo, context, arguments):
            calls.append(1)
            entered.set()
            await release.wait()
            return await original.handler(repo, context, arguments)

        registry = ToolRegistry(
            tuple(
                replace(item, handler=waiting) if item.name == "createLead" else item
                for item in env.executor.registry._definitions.values()
            )
        )
        executor = ToolExecutor(env.storage, env.registry, registry)
        request = create(customer, "processing")
        task = asyncio.create_task(executor.execute(ctx, request))
        await asyncio.wait_for(entered.wait(), 3)
        duplicate = await executor.execute(ctx, request)
        assert duplicate.result.code == "IN_PROGRESS" and not duplicate.handler_invoked
        release.set()
        assert (await task).result.status == "success" and len(calls) == 1

    async def test_fabricated_handler_receipt_is_not_accepted_as_business_success(self, state_env):
        from app.tools import LeadReceipt

        env = state_env
        ctx, customer = await qualify(env)

        async def fabricated(*_):
            return LeadReceipt(lead_id="invented")

        registry = ToolRegistry(
            tuple(
                replace(item, handler=fabricated) if item.name == "createLead" else item
                for item in env.executor.registry._definitions.values()
            )
        )
        executor = ToolExecutor(env.storage, env.registry, registry)
        denied = await executor.execute(ctx, create(customer))
        assert denied.result.code == "INVALID_RESULT"
        assert await env.storage.db.leads.count_documents({}) == 0

    async def test_trace_save_failure_preserves_verified_outcome_and_repairs_on_replay(
        self, state_env, monkeypatch
    ):
        env = state_env
        ctx, customer = await qualify(env)
        collection_type = type(env.storage.db.tool_executions)
        original = collection_type.update_one

        async def trace_unavailable(collection, key, update, *args, **kwargs):
            if (
                collection.name == "tool_executions"
                and update.get("$set", {}).get("status") == "completed"
            ):
                raise AutoReconnect("synthetic trace write interruption")
            return await original(collection, key, update, *args, **kwargs)

        request = create(customer, "trace-interrupted")
        with monkeypatch.context() as patch:
            patch.setattr(collection_type, "update_one", trace_unavailable)
            first = await env.executor.execute(ctx, request)
        assert first.result.status == "success"
        replay = await env.executor.execute(ctx, request)
        assert replay.result.status == "success" and replay.replayed and not replay.handler_invoked
        assert (await env.storage.db.tool_executions.find_one({"call_id": request.call_id}))[
            "status"
        ] == "completed"

    async def test_conflicting_preferences_still_allow_contact_enquiry(self, state_env):
        env = state_env
        ctx, customer = await qualify(env)
        later = await next_turn(env, "Change my budget to 20000")
        await env.service.apply(later, proposal(later, {"budget": 20000}, excerpt="20000"))
        saved = await env.executor.execute(
            later, create(customer.model_copy(update={"budget": 20000}))
        )
        assert saved.result.status == "success" and saved.handler_invoked
        assert await env.storage.db.leads.count_documents({}) == 1
        state = await env.service.view(later)
        assert "budget" in state.selection_conflicts
