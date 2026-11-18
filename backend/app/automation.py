"""Durable embedded-event poller with token-fenced stages and local mock notification."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import ValidationError
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.businesses import BusinessRegistry
from app.contracts import Summary
from app.database import Database
from app.gemini import ProviderFailure
from app.generation import SummaryPlan
from app.measurements import count, operation, stage
from app.persistence_models import LeadRecord, MockNotification
from app.provider_control import deadline
from app.repositories import parse, stored
from app.settings import Settings

logger = logging.getLogger(__name__)


class SummaryPlanner(Protocol):
    async def plan_summary(self, data: dict[str, object]) -> SummaryPlan: ...


class AutomationFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__("Automation stage could not complete.")
        self.code = code


@dataclass(frozen=True)
class DrainResult:
    claimed: int
    reason: Literal["idle", "job_limit", "time_limit", "stop"]


class AutomationWorker:
    lease_seconds = 120
    operation_seconds = 60

    def __init__(
        self,
        database: Database,
        businesses: BusinessRegistry,
        planner: SummaryPlanner,
        settings: Settings,
    ) -> None:
        self.database, self.businesses, self.planner, self.settings = (
            database,
            businesses,
            planner,
            settings,
        )
        self._task: asyncio.Task[None] | None = None
        self.measurements = database.measurements
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="lead-summary-worker")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.drain()
            except Exception as error:
                # No record/customer/provider payload or raw exception text enters logs.
                logger.warning("automation_poll_failed error_type=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.automation_poll_ms / 1000)
            except TimeoutError:
                pass

    @operation("worker_batch")
    async def drain(self) -> DrainResult:
        """Bound scheduling between jobs; each claimed job retains its own timeout/lease."""
        started = monotonic()
        claimed = 0
        while True:
            if self._stop.is_set():
                count("worker_batch_stopped")
                return DrainResult(claimed, "stop")
            if (monotonic() - started) * 1000 >= self.settings.automation_batch_ms:
                count("worker_batch_time_limit")
                return DrainResult(claimed, "time_limit")
            if claimed >= self.settings.automation_batch_jobs:
                count("worker_batch_job_limit")
                return DrainResult(claimed, "job_limit")
            if not await self.tick():
                count("worker_batch_idle")
                return DrainResult(claimed, "idle")
            claimed += 1
            count("worker_batch_jobs")
            # Give ready chat/shutdown tasks a turn even with immediately completed I/O.
            await asyncio.sleep(0)

    @stage("worker_claim")
    async def claim(self) -> LeadRecord | None:
        now = datetime.now(UTC)
        scope = {"business_id": {"$in": [b.business_id for b in self.businesses.configurations()]}}
        await self.database.db.leads.update_many(
            scope
            | {
                "automation.status": "processing",
                "automation.lease_until": {"$lte": now},
                "automation.attempts": {"$gte": self.settings.automation_max_attempts},
            },
            {
                "$set": {
                    "automation.status": "failed",
                    "automation.retryable": False,
                    "automation.failure_code": "ATTEMPT_LIMIT",
                    "automation.claim_token": None,
                    "automation.lease_until": None,
                    "automation.next_attempt_at": None,
                }
            },
        )
        due = {
            "$or": [
                {"automation.next_attempt_at": None},
                {"automation.next_attempt_at": {"$lte": now}},
            ]
        }
        eligible: dict[str, Any] = scope | {
            "automation.attempts": {"$lt": self.settings.automation_max_attempts},
            "$or": [
                {"automation.status": "pending", **due},
                {"automation.status": "failed", "automation.retryable": True, **due},
                {"automation.status": "processing", "automation.lease_until": {"$lte": now}},
            ],
        }
        token = str(uuid4())
        document = await self.database.db.leads.find_one_and_update(
            eligible,
            {
                "$set": {
                    "automation.status": "processing",
                    "automation.claim_token": token,
                    "automation.lease_until": now + timedelta(seconds=self.lease_seconds),
                    "automation.next_attempt_at": None,
                    "automation.failure_code": None,
                },
                "$inc": {"automation.attempts": 1},
            },
            sort=[("created_at", 1), ("lead_id", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            return None
        try:
            return parse(LeadRecord, document)
        except ValidationError:
            await self.database.db.leads.update_one(
                {"_id": document["_id"], "automation.claim_token": token},
                {
                    "$set": {
                        "automation.status": "failed",
                        "automation.retryable": False,
                        "automation.failure_code": "INVALID_JOB",
                        "automation.claim_token": None,
                        "automation.lease_until": None,
                    }
                },
            )
            return None

    def fence(self, lead: LeadRecord) -> dict[str, Any]:
        return {
            "business_id": lead.business_id,
            "lead_id": lead.lead_id,
            "automation.event_id": lead.automation.event_id,
            "automation.status": "processing",
            "automation.claim_token": lead.automation.claim_token,
            "automation.lease_until": {"$gt": datetime.now(UTC)},
        }

    @stage("summary")
    async def build_summary(self, lead: LeadRecord) -> Summary:
        history = []
        remaining = 16000
        for item in reversed(lead.automation_input):
            if item.role != "customer":
                continue
            excerpt = item.content[:3000]
            if len(excerpt) > remaining:
                break
            history.append(excerpt)
            remaining -= len(excerpt)
        history.reverse()
        plan = await self.planner.plan_summary(
            {
                "savedCustomer": lead.customer.model_dump(mode="json", by_alias=True),
                "customerHistory": history,
                "callbackIntentAtCreation": lead.automation_callback_intent is not None,
            }
        )
        plan = SummaryPlan.model_validate(plan.model_dump(mode="python"))
        if len(set(plan.customer_quotes)) != len(plan.customer_quotes) or any(
            not any(quote in text for text in history) for quote in plan.customer_quotes
        ):
            raise AutomationFailure("INVALID_SUMMARY_EVIDENCE")
        customer = lead.customer
        parts = [
            f"Customer: {customer.name}. Phone: {customer.phone}.",
            f"Course: {customer.course_id or 'not selected'}. "
            f"Interest: {customer.stated_interest or 'not provided'}.",
            f"Budget INR: {customer.budget if customer.budget is not None else 'not provided'}. "
            f"Schedule: {customer.schedule or 'not provided'}.",
        ]
        if lead.automation_callback_intent is not None:
            parts.append(
                "The customer requested a callback at enquiry creation; "
                "scheduling or completion is not confirmed."
            )
        if plan.customer_quotes:
            parts.append("Customer excerpts: " + " | ".join(f'"{q}"' for q in plan.customer_quotes))
        return Summary(
            lead_id=lead.lead_id,
            customer_name=customer.name,
            summary="\n".join(parts),
            intent={"course_enquiry": "Course enquiry", "learning_enquiry": "Learning enquiry"}[
                plan.intent
            ],
            qualification_status="qualified",
            recommended_action={
                "counselor_follow_up": "Counselor follow-up",
                "discuss_options": "Discuss suitable course options",
            }[plan.recommended_action],
        )

    @stage("summary_store")
    async def store_summary(self, lead: LeadRecord, summary: Summary) -> LeadRecord:
        summary = Summary.model_validate(summary.model_dump(mode="python"))
        if summary.lead_id != lead.lead_id or summary.customer_name != lead.customer.name:
            raise AutomationFailure("INVALID_SUMMARY_IDENTITY")
        document = await self.database.db.leads.find_one_and_update(
            self.fence(lead) | {"automation.summary": None},
            {"$set": {"automation.summary": stored(summary)}},
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AutomationFailure("LEASE_LOST")
        return parse(LeadRecord, document)

    @stage("notification")
    async def notify(self, lead: LeadRecord) -> LeadRecord:
        if lead.automation.summary is None:
            raise AutomationFailure("SUMMARY_REQUIRED")
        notification = MockNotification(
            notification_id=str(uuid4()),
            event_id=lead.automation.event_id,
            lead_id=lead.lead_id,
            created_at=datetime.now(UTC),
            payload=lead.automation.summary,
        )
        # Storage is the simulation. There is deliberately no external delivery step.
        document = await self.database.db.leads.find_one_and_update(
            self.fence(lead) | {"notification": None, "automation.summary": {"$ne": None}},
            {
                "$set": {
                    "notification": stored(notification),
                    "automation.status": "completed",
                    "automation.claim_token": None,
                    "automation.lease_until": None,
                    "automation.next_attempt_at": None,
                    "automation.failure_code": None,
                    "automation.retryable": False,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if document is None:
            raise AutomationFailure("LEASE_LOST")
        return parse(LeadRecord, document)

    async def fail(self, lead: LeadRecord, code: str, retryable: bool) -> None:
        retryable = retryable and lead.automation.attempts < self.settings.automation_max_attempts
        delay = min(
            300, self.settings.automation_retry_seconds * 2 ** (lead.automation.attempts - 1)
        )
        await self.database.db.leads.update_one(
            self.fence(lead),
            {
                "$set": {
                    "automation.status": "failed",
                    "automation.failure_code": code,
                    "automation.retryable": retryable,
                    "automation.claim_token": None,
                    "automation.lease_until": None,
                    "automation.next_attempt_at": datetime.now(UTC) + timedelta(seconds=delay)
                    if retryable
                    else None,
                }
            },
        )

    @operation("worker")
    async def tick(self) -> bool:
        lead = await self.claim()
        if lead is None:
            count("worker_idle_polls")
            return False
        count("worker_jobs")
        count(
            "worker_job_age_ms",
            min(
                1_000_000_000,
                max(0, int((datetime.now(UTC) - lead.created_at).total_seconds() * 1000)),
            ),
        )
        try:
            with deadline(max(0.001, self.operation_seconds - 5)):
                async with asyncio.timeout(self.operation_seconds):
                    if lead.automation.summary is None:
                        lead = await self.store_summary(lead, await self.build_summary(lead))
                    await self.notify(lead)
        except asyncio.CancelledError:
            raise  # Keep the durable lease so restart recovery can resume safely.
        except ProviderFailure as error:
            await self.fail(lead, error.code, error.retryable)
        except (PyMongoError, TimeoutError):
            await self.fail(lead, "UNAVAILABLE", True)
        except AutomationFailure as error:
            if error.code != "LEASE_LOST":
                await self.fail(lead, error.code, False)
        except (ValidationError, ValueError):
            await self.fail(lead, "INVALID_SUMMARY", False)
        except Exception:
            await self.fail(lead, "UNEXPECTED_ERROR", False)
        return True

    async def retry(self, business_id: str, lead_id: str, event_id: str) -> bool:
        if self.businesses.get(business_id) is None:
            return False
        result = await self.database.db.leads.update_one(
            {
                "business_id": business_id,
                "lead_id": lead_id,
                "automation.event_id": event_id,
                "automation.status": "failed",
                "notification": None,
            },
            {
                "$set": {
                    "automation.status": "pending",
                    "automation.attempts": 0,
                    "automation.failure_code": None,
                    "automation.retryable": False,
                    "automation.next_attempt_at": None,
                    "automation.claim_token": None,
                    "automation.lease_until": None,
                },
                "$inc": {"automation.manual_retries": 1},
            },
        )
        return result.modified_count == 1
