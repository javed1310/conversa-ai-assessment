import json
from datetime import UTC, datetime, timedelta

import pytest
from pymongo.asynchronous.collection import AsyncCollection

from app.contracts import ChatResult
from app.recovery import inspect_recovery
from app.repositories import RecordNotFound, stored
from tests.test_actions import create, qualify
from tests.test_state import next_turn
from tests.test_state import state_env as state_env


@pytest.mark.asyncio
async def test_inspection_is_read_only_and_excludes_customer_data(state_env, monkeypatch):
    e = state_env
    context = await next_turn(e, "PRIVATE CUSTOMER TEXT AND PHONE 9876543210")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Inspection must not write")

    for method in (
        "insert_one",
        "update_one",
        "update_many",
        "find_one_and_update",
        "delete_one",
        "delete_many",
        "create_index",
    ):
        monkeypatch.setattr(AsyncCollection, method, forbidden)
    report = await inspect_recovery(e.repo, context)
    assert report.decision == "resume_guarded"
    assert not report.internal_reopen_required
    text = report.model_dump_json()
    assert "PRIVATE" not in text and "9876543210" not in text
    assert len(report.snapshot_checksum) == 64


@pytest.mark.parametrize(
    "status,prepared,expected",
    [
        ("completed", False, "replay_completed"),
        ("processing", True, "replay_prepared"),
        ("failed", True, "replay_prepared"),
        ("failed", False, "resume_guarded"),
    ],
)
@pytest.mark.asyncio
async def test_output_and_failed_turn_decisions(state_env, status, prepared, expected):
    e = state_env
    context = await next_turn(e, "Hello")
    turn = await e.repo.turn(context)
    values = {"status": status}
    output = stored(
        ChatResult(
            message_id=turn.message_id, session_id=context.session_id, reply="PRIVATE SEALED REPLY"
        )
    )
    if prepared:
        values["prepared_output"] = output
    if status == "completed":
        values.update(output=output, completed_at=datetime.now(UTC))
    if status == "failed":
        values["completed_at"] = datetime.now(UTC)
    await e.repo.db.turns.update_one({"turn_id": context.turn_id}, {"$set": values})
    report = await inspect_recovery(e.repo, context)
    assert report.decision == expected
    assert report.internal_reopen_required == (status == "failed")
    assert "PRIVATE SEALED" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_active_and_legacy_gate_decisions(state_env):
    e = state_env
    context = await next_turn(e, "Hello")
    turn = await e.repo.turn(context)
    key = {"session_id": context.session_id}
    await e.repo.db.sessions.update_one(
        key,
        {
            "$set": {
                "_pending_message_id": turn.message_id,
                "_pending_payload_hash": turn.payload_hash,
                "_pending_expires_at": datetime.now(UTC) + timedelta(seconds=30),
            }
        },
    )
    assert (await inspect_recovery(e.repo, context)).decision == "wait"
    await e.repo.db.tool_executions.insert_one(
        {
            "business_id": context.business_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": "active-call",
            "execution_id": "active-execution",
            "tool": "createLead",
            "status": "processing",
            "trace": None,
        }
    )
    assert (await inspect_recovery(e.repo, context)).decision == "wait"
    await e.repo.db.sessions.update_one(key, {"$unset": {"_pending_expires_at": ""}})
    assert (await inspect_recovery(e.repo, context)).decision == "review_required"


@pytest.mark.asyncio
async def test_uncertain_write_without_record_never_authorizes_repeat(state_env):
    e = state_env
    context = await next_turn(e, "Save an enquiry")
    await e.repo.db.tool_executions.insert_one(
        {
            "business_id": context.business_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": "call-1",
            "execution_id": "execution-1",
            "tool": "createLead",
            "status": "uncertain",
            "trace": None,
        }
    )
    report = await inspect_recovery(e.repo, context)
    assert report.decision == "review_required"
    assert report.saved_lead_id is None
    assert "RECOVERY_REQUIRES_REVIEW" in report.reasons
    assert report.executions[0].recorded_outcome == "unverified"


@pytest.mark.asyncio
async def test_actual_receipt_is_reported_but_not_reexecuted(state_env):
    e = state_env
    context, customer = await qualify(e)
    result = await e.executor.execute(context, create(customer))
    before = await e.repo.db.tool_executions.find({}).to_list()
    report = await inspect_recovery(e.repo, context)
    assert report.saved_lead_id == result.result.data["leadId"]
    assert report.executions[0].receipt_matches_record
    assert report.decision == "review_required"
    assert await e.repo.db.tool_executions.find({}).to_list() == before
    assert customer.phone not in report.model_dump_json()


@pytest.mark.asyncio
async def test_newer_turn_blocks_reopening_old_turn(state_env):
    e = state_env
    old = await next_turn(e, "old")
    await next_turn(e, "new")
    report = await inspect_recovery(e.repo, old)
    assert report.decision == "stale" and "STALE_TURN" in report.reasons


@pytest.mark.asyncio
async def test_foreign_owner_is_indistinguishable_from_missing(state_env):
    e = state_env
    context = await next_turn(e, "Hello")
    with pytest.raises(RecordNotFound):
        await inspect_recovery(e.repo, context.model_copy(update={"session_id": "foreign"}))


@pytest.mark.asyncio
async def test_corrupt_trace_is_a_review_not_success(state_env):
    e = state_env
    context = await next_turn(e, "Hello")
    await e.repo.db.tool_executions.insert_one(
        {
            "business_id": context.business_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "call_id": "call-1",
            "execution_id": "execution-1",
            "tool": "createLead",
            "status": "completed",
            "trace": {"secret": "PRIVATE"},
        }
    )
    report = await inspect_recovery(e.repo, context)
    assert report.decision == "review_required"
    assert report.executions[0].recorded_outcome == "unverified"
    assert "PRIVATE" not in json.dumps(report.model_dump(mode="json"))


def test_cli_audit_records_intention_without_executing_or_overwriting(
    monkeypatch, tmp_path, capsys
):
    import sys

    from app import recovery

    report = recovery.RecoveryReport(
        observed_at=datetime.now(UTC),
        business_id="training-institute",
        session_id="session-1",
        turn_id="turn-1",
        message_id="message-1",
        decision="review_required",
        internal_reopen_required=False,
        reasons=("RECOVERY_REQUIRES_REVIEW",),
        saved_lead_id=None,
        callback_request_id=None,
        executions=(),
        snapshot_checksum="0" * 64,
    )

    async def inspect(*args):
        return report

    monkeypatch.setattr(recovery, "inspect_cli", inspect)
    output = tmp_path / "audit.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recovery",
            "--business",
            "training-institute",
            "--session",
            "session-1",
            "--turn",
            "turn-1",
            "--disposition",
            "request-retry",
            "--audit-file",
            str(output),
        ],
    )
    recovery.main()
    body = json.loads(output.read_text())
    assert body["operatorIntention"] == "request-retry"
    assert body["actionPerformed"] is False and body["retryCandidate"] is False
    before = output.read_bytes()
    with pytest.raises(SystemExit, match="could not be created"):
        recovery.main()
    assert output.read_bytes() == before
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_callback_receipt_match_and_mismatch_are_explicit(state_env):
    from tests.test_actions import callback

    e = state_env
    context, customer = await qualify(e, callback=True)
    created = await e.executor.execute(context, create(customer))
    await e.executor.execute(context, callback(created.result.data["leadId"]))
    report = await inspect_recovery(e.repo, context)
    assert report.callback_request_id is not None
    assert all(item.receipt_matches_record for item in report.executions)
    await e.repo.db.leads.update_one(
        {"lead_id": report.saved_lead_id}, {"$set": {"callback_request_id": "changed-request"}}
    )
    report = await inspect_recovery(e.repo, context)
    assert report.decision == "review_required"
    assert "RECEIPT_RECORD_MISMATCH" in report.reasons


@pytest.mark.asyncio
async def test_inspection_execution_limit_fails_closed(state_env):
    e = state_env
    context = await next_turn(e, "Hello")
    await e.repo.db.tool_executions.insert_many(
        [
            {
                "business_id": context.business_id,
                "session_id": context.session_id,
                "turn_id": context.turn_id,
                "call_id": f"call-{i}",
                "execution_id": f"execution-{i}",
                "tool": "getCourseDetails",
                "status": "processing",
                "trace": None,
            }
            for i in range(65)
        ]
    )
    report = await inspect_recovery(e.repo, context)
    assert report.decision == "review_required" and "EXECUTION_LIMIT" in report.reasons
    assert len(report.executions) == 64
