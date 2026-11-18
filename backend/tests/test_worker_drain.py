import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.test_automation import worker_env as worker_env
from tests.test_state import state_env as state_env


async def add_jobs(e, count):
    original = await e.env.storage.db.leads.find_one({"lead_id": e.lead.lead_id})
    for index in range(count):
        raw = deepcopy(original)
        raw.pop("_id")
        raw["lead_id"] = str(uuid4())
        raw["session_id"] = str(uuid4())
        raw["automation"]["event_id"] = str(uuid4())
        raw["created_at"] = original["created_at"] + timedelta(seconds=index + 1)
        await e.env.storage.db.leads.insert_one(raw)


@pytest.mark.asyncio
async def test_bounded_drain_and_unique_effects(worker_env):
    e = worker_env
    await add_jobs(e, 4)
    e.worker.settings = e.settings.model_copy(update={"automation_batch_jobs": 2})
    first = await e.worker.drain()
    assert (first.claimed, first.reason) == (2, "job_limit")
    second = await e.worker.drain()
    third = await e.worker.drain()
    assert (second.claimed, second.reason) == (2, "job_limit")
    assert (third.claimed, third.reason) == (1, "idle")
    leads = await e.env.storage.db.leads.find({}).to_list()
    assert all(lead["automation"]["status"] == "completed" for lead in leads)
    assert len({lead["notification"]["notification_id"] for lead in leads}) == 5
    assert len(e.planner.calls) == 5
    assert (await e.worker.drain()).claimed == 0
    assert len(e.planner.calls) == 5
    measurements = e.env.storage.measurements.recent()
    batch = [item for item in measurements if item["operation"] == "worker_batch"][0]
    assert batch["counters"]["worker_batch_jobs"] == 2
    jobs = [
        item
        for item in measurements
        if item["operation"] == "worker" and item["counters"].get("worker_jobs")
    ]
    assert len(jobs) == 5
    assert all(item["counters"]["worker_job_age_ms"] >= 0 for item in jobs)
    assert e.lead.customer.phone not in json.dumps(measurements)


@pytest.mark.asyncio
async def test_time_bound_checked_between_jobs(worker_env, monkeypatch):
    e = worker_env
    now = [0.0]
    monkeypatch.setattr("app.automation.monotonic", lambda: now[0])
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        now[0] = 1.1
        return True

    monkeypatch.setattr(e.worker, "tick", tick)
    result = await e.worker.drain()
    assert result.claimed == calls == 1
    assert result.reason == "time_limit"


@pytest.mark.asyncio
async def test_yield_allows_ready_stop_before_next_claim(worker_env, monkeypatch):
    e = worker_env
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(e.worker, "tick", tick)
    asyncio.get_running_loop().call_soon(e.worker._stop.set)
    result = await e.worker.drain()
    assert result.claimed == calls == 1
    assert result.reason == "stop"
    assert (await e.worker.drain()).claimed == 0


@pytest.mark.asyncio
async def test_idle_cycle_stops_after_one_empty_claim(worker_env, monkeypatch):
    e = worker_env
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(e.worker, "tick", tick)
    result = await e.worker.drain()
    assert result.claimed == 0 and result.reason == "idle" and calls == 1


@pytest.mark.asyncio
async def test_cancelled_batch_preserves_durable_claim(worker_env):
    e = worker_env
    e.planner.release.clear()
    task = asyncio.create_task(e.worker.drain())
    await asyncio.wait_for(e.planner.started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lead = await e.current()
    assert lead.automation.status == "processing"
    assert lead.automation.claim_token is not None
    assert lead.notification is None


@pytest.mark.asyncio
async def test_future_retry_does_not_block_oldest_eligible_other_jobs(worker_env, monkeypatch):
    e = worker_env
    await add_jobs(e, 3)
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
    records = (
        await e.env.storage.db.leads.find({"lead_id": {"$ne": e.lead.lead_id}})
        .sort("created_at", 1)
        .to_list()
    )
    await e.env.storage.db.leads.update_one(
        {"lead_id": records[0]["lead_id"]}, {"$set": {"business_id": "fitness-studio"}}
    )
    order = []
    original = e.worker.claim

    async def claim():
        lead = await original()
        if lead is not None:
            order.append((lead.lead_id, lead.business_id))
        return lead

    monkeypatch.setattr(e.worker, "claim", claim)
    result = await e.worker.drain()
    assert result.claimed == 3 and result.reason == "idle"
    assert [lead for lead, _ in order] == [record["lead_id"] for record in records]
    assert order[0][1] == "fitness-studio"
    assert (await e.current()).automation.status == "failed"
