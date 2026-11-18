"""Actual Mongo worker claims, immutable inputs, retries and lifespan; no live provider calls."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from pymongo.errors import AutoReconnect

from app.automation import AutomationFailure, AutomationWorker
from app.database import open_database
from app.gemini import ProviderFailure
from app.generation import SummaryPlan
from app.main import create_app
from app.persistence_models import LeadRecord
from app.repositories import parse
from app.settings import Settings
from tests.test_actions import create, qualify
from tests.test_state import next_turn, proposal
from tests.test_state import state_env as state_env


class TestPlanner:
    __test__ = False

    def __init__(self):
        self.calls = []
        self.failure = None
        self.invalid_quote = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def plan_summary(self, data):
        self.calls.append(data)
        self.started.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return SummaryPlan(
            intent="course_enquiry",
            recommended_action="counselor_follow_up",
            customer_quotes=("Invented customer price 99999",)
            if self.invalid_quote
            else (data["customerHistory"][-1][:80],),
        )


@pytest_asyncio.fixture
async def worker_env(state_env):
    env = state_env
    context, customer = await qualify(env, callback=True)
    saved = await env.executor.execute(context, create(customer))
    lead = await env.repo.lead(context, saved.result.data["leadId"])
    planner = TestPlanner()
    settings = Settings(
        _env_file=None,
        automation_enabled=True,
        automation_poll_ms=100,
        automation_max_attempts=2,
        automation_retry_seconds=1,
    )
    worker = AutomationWorker(env.storage, env.registry, planner, settings)

    async def current():
        return parse(LeadRecord, await env.storage.db.leads.find_one({"lead_id": lead.lead_id}))

    async def due():
        await env.storage.db.leads.update_one(
            {"lead_id": lead.lead_id},
            {"$set": {"automation.next_attempt_at": datetime.now(UTC) - timedelta(seconds=1)}},
        )

    return SimpleNamespace(
        env=env,
        context=context,
        lead=lead,
        worker=worker,
        planner=planner,
        settings=settings,
        current=current,
        due=due,
    )


@pytest.mark.integration
class TestAutomation:
    async def test_event_automatically_completes_with_six_field_summary_and_mock_record(
        self, worker_env
    ):
        e = worker_env
        assert await e.worker.tick()
        record = await e.current()
        assert record.automation.status == "completed" and record.notification is not None
        summary = record.automation.summary
        assert summary.lead_id == record.lead_id and summary.customer_name == "Rahul"
        assert summary.qualification_status == "qualified"
        assert set(summary.model_dump()) == {
            "lead_id",
            "customer_name",
            "summary",
            "intent",
            "qualification_status",
            "recommended_action",
        }
        assert (
            "30000" in summary.summary
            and "DS202" in summary.summary
            and "weekend" in summary.summary
        )
        assert (
            record.notification.payload == summary
            and record.notification.event_id == record.automation.event_id
        )
        assert record.callback_requested_at is None  # Trigger is independent of callback success.
        assert len(e.planner.calls) == 1 and not await e.worker.tick()

    async def test_concurrent_claims_have_one_owner(self, worker_env):
        e = worker_env
        claims = await asyncio.gather(*(e.worker.claim() for _ in range(8)))
        assert sum(item is not None for item in claims) == 1
        assert (await e.current()).automation.attempts == 1

    async def test_future_retry_and_terminal_failure_are_not_claimed(self, worker_env):
        e = worker_env
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {
                "$set": {
                    "automation.status": "failed",
                    "automation.retryable": True,
                    "automation.next_attempt_at": datetime.now(UTC) + timedelta(hours=1),
                }
            },
        )
        assert await e.worker.claim() is None
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {
                "$set": {
                    "automation.retryable": False,
                    "automation.next_attempt_at": None,
                }
            },
        )
        assert await e.worker.claim() is None

    async def test_expired_claim_changes_token_and_fences_old_worker(self, worker_env):
        e = worker_env
        first = await e.worker.claim()
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {"$set": {"automation.lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
        )
        replacement = AutomationWorker(e.env.storage, e.env.registry, e.planner, e.settings)
        second = await replacement.claim()
        assert second.automation.claim_token != first.automation.claim_token
        summary = await e.worker.build_summary(first)
        with pytest.raises(AutomationFailure) as error:
            await e.worker.store_summary(first, summary)
        assert error.value.code == "LEASE_LOST"
        saved = await replacement.store_summary(second, summary)
        await replacement.notify(saved)
        assert (await e.current()).automation.status == "completed"

    async def test_later_customer_correction_cannot_change_automation_snapshot(self, worker_env):
        e = worker_env
        later = await next_turn(e.env, "Change my phone to 9876543211 and budget to 20000")
        await e.env.service.apply(
            later,
            proposal(
                later,
                {"phone": "9876543211", "budget": 20000},
                excerpt="Change my phone to 9876543211 and budget to 20000",
            ),
        )
        await e.worker.tick()
        summary = (await e.current()).automation.summary
        assert "+919876543210" in summary.summary and "30000" in summary.summary
        assert "9876543211" not in summary.summary and "20000" not in summary.summary
        assert e.planner.calls[0]["savedCustomer"]["phone"] == "+919876543210"

    async def test_invalid_model_quote_is_terminal_and_preserves_lead(self, worker_env):
        e = worker_env
        e.planner.invalid_quote = True
        await e.worker.tick()
        lead = await e.current()
        assert lead.automation.status == "failed" and not lead.automation.retryable
        assert lead.automation.failure_code == "INVALID_SUMMARY_EVIDENCE"
        assert lead.automation.summary is None and lead.notification is None
        assert lead.customer == e.lead.customer

    async def test_retry_is_bounded_and_same_event_manual_retry_can_recover(self, worker_env):
        e = worker_env
        e.planner.failure = ProviderFailure("RATE_LIMIT", True)
        await e.worker.tick()
        first = await e.current()
        assert first.automation.retryable and first.automation.next_attempt_at is not None
        assert not await e.worker.tick()
        await e.due()
        await e.worker.tick()
        exhausted = await e.current()
        assert exhausted.automation.attempts == 2 and not exhausted.automation.retryable
        assert not await e.worker.tick()
        event_id = exhausted.automation.event_id
        assert await e.worker.retry(exhausted.business_id, exhausted.lead_id, event_id)
        e.planner.failure = None
        await e.worker.tick()
        complete = await e.current()
        assert (
            complete.automation.event_id == event_id and complete.automation.status == "completed"
        )
        assert complete.automation.manual_retries == 1
        assert not await e.worker.retry(complete.business_id, complete.lead_id, event_id)

    async def test_notification_retry_reuses_saved_summary_without_regeneration(
        self, worker_env, monkeypatch
    ):
        e = worker_env
        original = e.worker.notify

        async def failed(_):
            raise AutoReconnect("synthetic notification storage failure")

        with monkeypatch.context() as patch:
            patch.setattr(e.worker, "notify", failed)
            await e.worker.tick()
        failed_lead = await e.current()
        assert failed_lead.automation.summary is not None and failed_lead.notification is None
        await e.due()
        await e.worker.tick()
        complete = await e.current()
        assert complete.notification.payload == failed_lead.automation.summary
        assert len(e.planner.calls) == 1 and complete.automation.status == "completed"
        assert e.worker.notify == original

    async def test_summary_commit_lost_ack_reuses_actual_saved_summary(
        self, worker_env, monkeypatch
    ):
        e = worker_env
        collection_type = type(e.env.storage.db.leads)
        original = collection_type.find_one_and_update
        failed = False

        async def lost_ack(collection, key, update, *args, **kwargs):
            nonlocal failed
            result = await original(collection, key, update, *args, **kwargs)
            if (
                collection.name == "leads"
                and "automation.summary" in update.get("$set", {})
                and not failed
            ):
                failed = True
                raise AutoReconnect("synthetic summary acknowledgement loss")
            return result

        monkeypatch.setattr(collection_type, "find_one_and_update", lost_ack)
        await e.worker.tick()
        assert (await e.current()).automation.summary is not None
        await e.due()
        await e.worker.tick()
        assert len(e.planner.calls) == 1 and (await e.current()).notification is not None

    async def test_notification_commit_lost_ack_does_not_duplicate(self, worker_env, monkeypatch):
        e = worker_env
        collection_type = type(e.env.storage.db.leads)
        original = collection_type.find_one_and_update

        async def lost_ack(collection, key, update, *args, **kwargs):
            result = await original(collection, key, update, *args, **kwargs)
            if collection.name == "leads" and "notification" in update.get("$set", {}):
                raise AutoReconnect("synthetic notification acknowledgement loss")
            return result

        monkeypatch.setattr(collection_type, "find_one_and_update", lost_ack)
        await e.worker.tick()
        complete = await e.current()
        assert complete.automation.status == "completed" and complete.notification is not None
        identity = complete.notification.notification_id
        assert not await e.worker.tick()
        assert (await e.current()).notification.notification_id == identity

    async def test_cancelled_worker_leaves_lease_and_restart_resumes(self, worker_env):
        e = worker_env
        e.planner.release.clear()
        task = asyncio.create_task(e.worker.tick())
        await asyncio.wait_for(e.planner.started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await e.current()).automation.status == "processing"
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {"$set": {"automation.lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
        )
        e.planner.release.set()
        restarted = AutomationWorker(e.env.storage, e.env.registry, e.planner, e.settings)
        assert await restarted.tick() and (await e.current()).automation.status == "completed"

    async def test_expired_last_attempt_becomes_terminal_failed(self, worker_env):
        e = worker_env
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {
                "$set": {
                    "automation.status": "processing",
                    "automation.attempts": 2,
                    "automation.lease_until": datetime.now(UTC) - timedelta(seconds=1),
                }
            },
        )
        assert await e.worker.claim() is None
        assert (await e.current()).automation.failure_code == "ATTEMPT_LIMIT"

    async def test_lifespan_processes_automatically_and_restart_keeps_same_notification(
        self, worker_env, monkeypatch
    ):
        e = worker_env
        monkeypatch.setattr("app.main.open_database", open_database)
        settings = Settings(
            _env_file=None,
            mongodb_uri=f"mongodb://127.0.0.1:27017/{e.env.storage.db.name}",
            automation_enabled=True,
            automation_poll_ms=100,
            gemini_api_key="synthetic-not-a-key",
            ai_chat_enabled=False,
        )
        completed = asyncio.Event()
        original_notify = AutomationWorker.notify

        async def observed_notify(instance, lead):
            result = await original_notify(instance, lead)
            completed.set()
            return result

        monkeypatch.setattr(AutomationWorker, "notify", observed_notify)
        app = create_app(settings, summary_planner=e.planner)
        async with LifespanManager(app):
            worker = app.state.automation_worker
            assert worker is not None
            await asyncio.wait_for(completed.wait(), 5)
            assert (await e.current()).automation.status == "completed"
        assert worker._task is None and app.state.automation_worker is None
        identity = (await e.current()).notification.notification_id
        async with LifespanManager(create_app(settings, summary_planner=e.planner)):
            await asyncio.sleep(0.15)
        assert (await e.current()).notification.notification_id == identity
        assert len(e.planner.calls) == 1

    async def test_manual_retry_cannot_cross_business_or_change_event(self, worker_env):
        e = worker_env
        e.planner.invalid_quote = True
        await e.worker.tick()
        assert not await e.worker.retry(
            "fitness-studio", e.lead.lead_id, e.lead.automation.event_id
        )
        assert not await e.worker.retry(e.lead.business_id, e.lead.lead_id, "wrong-event")

    async def test_stale_notification_stage_cannot_overwrite_new_owner(self, worker_env):
        e = worker_env
        first = await e.worker.claim()
        first = await e.worker.store_summary(first, await e.worker.build_summary(first))
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {"$set": {"automation.lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
        )
        replacement = AutomationWorker(e.env.storage, e.env.registry, e.planner, e.settings)
        second = await replacement.claim()
        with pytest.raises(AutomationFailure) as error:
            await e.worker.notify(first)
        assert error.value.code == "LEASE_LOST"
        complete = await replacement.notify(second)
        assert complete.notification is not None and len(e.planner.calls) == 1

    async def test_summary_identity_cannot_be_supplied_by_a_different_lead(self, worker_env):
        e = worker_env
        lead = await e.worker.claim()
        summary = await e.worker.build_summary(lead)
        with pytest.raises(AutomationFailure) as error:
            await e.worker.store_summary(lead, summary.model_copy(update={"lead_id": "foreign"}))
        assert error.value.code == "INVALID_SUMMARY_IDENTITY"
        assert (await e.current()).automation.summary is None

    async def test_notification_stage_cancellation_restarts_without_new_summary(
        self, worker_env, monkeypatch
    ):
        e = worker_env
        started = asyncio.Event()

        async def suspended(_):
            started.set()
            await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(e.worker, "notify", suspended)
            task = asyncio.create_task(e.worker.tick())
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (await e.current()).automation.summary is not None
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {"$set": {"automation.lease_until": datetime.now(UTC) - timedelta(seconds=1)}},
        )
        await e.worker.tick()
        assert (await e.current()).automation.status == "completed" and len(e.planner.calls) == 1

    async def test_polling_survives_transient_database_failure(self, worker_env, monkeypatch):
        e = worker_env
        original_claim, original_notify = e.worker.claim, e.worker.notify
        complete = asyncio.Event()
        failed = False

        async def transient_claim():
            nonlocal failed
            if not failed:
                failed = True
                raise AutoReconnect("synthetic poll outage")
            return await original_claim()

        async def observed_notify(lead):
            result = await original_notify(lead)
            complete.set()
            return result

        monkeypatch.setattr(e.worker, "claim", transient_claim)
        monkeypatch.setattr(e.worker, "notify", observed_notify)
        e.worker.start()
        try:
            await asyncio.wait_for(complete.wait(), 5)
        finally:
            await e.worker.close()
        assert failed and (await e.current()).automation.status == "completed"

    async def test_operation_timeout_is_retryable_and_does_not_lose_lead(self, worker_env):
        e = worker_env
        e.worker.operation_seconds = 0.02
        e.planner.release.clear()
        await e.worker.tick()
        failed = await e.current()
        assert failed.automation.status == "failed" and failed.automation.retryable
        assert failed.automation.summary is None and failed.customer == e.lead.customer

    async def test_assistant_only_text_cannot_supply_customer_quotes(self, worker_env):
        e = worker_env
        await e.env.storage.db.leads.update_one(
            {"lead_id": e.lead.lead_id},
            {
                "$push": {
                    "automation_input": {
                        "turn_id": e.context.turn_id,
                        "role": "assistant",
                        "content": "Invented customer price 99999",
                    }
                }
            },
        )
        e.planner.invalid_quote = True
        await e.worker.tick()
        assert (await e.current()).automation.failure_code == "INVALID_SUMMARY_EVIDENCE"
