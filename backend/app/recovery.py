"""Read-only operator snapshot; recommendations never authorize or perform a replay."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, ValidationError
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app.actions import WRITE_TOOLS
from app.businesses import BusinessRegistry
from app.contracts import ActionResult, Contract, ExecutionContext, Identifier
from app.coordination import CoordinationFailure, TurnCoordinator
from app.database import Database
from app.persistence_models import LeadRecord
from app.repositories import RecordNotFound, Repository, parse
from app.settings import load_settings
from app.tools import CallbackReceipt, LeadReceipt


class ExecutionReview(Contract):
    execution_id: Identifier
    tool: Identifier
    status: Literal["processing", "completed", "uncertain", "invalid"]
    recorded_outcome: Literal["success", "failure", "unverified"] = "unverified"
    receipt_matches_record: bool = False


class RecoveryReport(Contract):
    observed_at: AwareDatetime
    business_id: Identifier
    session_id: Identifier
    turn_id: Identifier
    message_id: Identifier
    decision: Literal[
        "replay_completed",
        "replay_prepared",
        "wait",
        "resume_guarded",
        "review_required",
        "stale",
    ]
    internal_reopen_required: bool
    reasons: tuple[Identifier, ...]
    saved_lead_id: Identifier | None
    callback_request_id: Identifier | None
    executions: Annotated[tuple[ExecutionReview, ...], Field(max_length=64)]
    snapshot_checksum: str


async def inspect_recovery(repo: Repository, context: ExecutionContext) -> RecoveryReport:
    turn = await repo.turn(context)
    gate = (
        await repo.db.sessions.find_one(
            repo.owner(context),
            {
                "_pending_message_id": 1,
                "_pending_expires_at": 1,
                "_pending_payload_hash": 1,
                "_id": 0,
            },
        )
        or {}
    )
    raw_lead = await repo.db.leads.find_one(
        {"business_id": context.business_id, "session_id": context.session_id}
    )
    lead = parse(LeadRecord, raw_lead) if raw_lead is not None else None
    records = (
        await repo.db.tool_executions.find(
            {
                "business_id": context.business_id,
                "session_id": context.session_id,
                "turn_id": context.turn_id,
            },
            {"execution_id": 1, "tool": 1, "status": 1, "trace": 1, "call_id": 1, "_id": 0},
        )
        .limit(65)
        .to_list()
    )
    reviews = []
    reasons = []
    if len(records) > 64:
        reasons.append("EXECUTION_LIMIT")
    known = {
        "createLead",
        "requestCallback",
        "searchCourses",
        "getCourseDetails",
        "getMembershipDetails",
    }
    for raw in records[:64]:
        outcome: Literal["success", "failure", "unverified"] = "unverified"
        matches = False
        tool = (
            raw.get("tool")
            if isinstance(raw.get("tool"), str) and raw["tool"] in known
            else "unknown"
        )
        status = raw.get("status")
        if not isinstance(status, str) or status not in {"processing", "completed", "uncertain"}:
            status = "invalid"
        try:
            if tool == "unknown" or status == "invalid":
                raise ValueError("Invalid execution")
            if raw.get("trace") is not None:
                trace = ActionResult.model_validate(raw["trace"])
                if (
                    trace.execution_id != raw["execution_id"]
                    or trace.result.tool != tool
                    or trace.result.call_id != raw["call_id"]
                    or status != "completed"
                ):
                    raise ValueError("Inconsistent execution")
                outcome = trace.result.status
                if trace.result.status == "success" and tool in WRITE_TOOLS:
                    receipt = (
                        CallbackReceipt.model_validate(trace.result.data)
                        if tool == "requestCallback"
                        else LeadReceipt.model_validate(trace.result.data)
                    )
                    matches = lead is not None and receipt.lead_id == lead.lead_id
                    if isinstance(receipt, CallbackReceipt):
                        matches = (
                            matches
                            and lead is not None
                            and receipt.request_id == lead.callback_request_id
                            and receipt.requested_at == lead.callback_requested_at
                        )
                    if not matches:
                        reasons.append("RECEIPT_RECORD_MISMATCH")
            elif status == "completed":
                raise ValueError("Missing trace")
            reviews.append(
                ExecutionReview(
                    execution_id=raw["execution_id"],
                    tool=tool,
                    status=status,
                    recorded_outcome=outcome,
                    receipt_matches_record=matches,
                )
            )
        except (ValidationError, ValueError, KeyError):
            reasons.append("INVALID_EXECUTION_RECORD")
            reviews.append(
                ExecutionReview(execution_id="invalid-record", tool="unknown", status="invalid")
            )
    decision: Literal[
        "replay_completed",
        "replay_prepared",
        "wait",
        "resume_guarded",
        "review_required",
        "stale",
    ] = "review_required"
    if reasons:
        pass
    elif turn.status == "completed":
        decision = "replay_completed"
        reasons.append("TERMINAL_OUTPUT_AVAILABLE")
    else:
        pending = gate.get("_pending_message_id")
        expiry = gate.get("_pending_expires_at")
        if pending is not None and pending != turn.message_id:
            decision = "wait"
            reasons.append("SESSION_BUSY")
        elif pending is not None and gate.get("_pending_payload_hash") != turn.payload_hash:
            reasons.append("PAYLOAD_CONFLICT")
        elif pending is not None and not isinstance(expiry, datetime):
            reasons.append("LEGACY_GATE_REQUIRES_REVIEW")
        elif isinstance(expiry, datetime) and pending is not None and expiry > datetime.now(UTC):
            decision = "wait"
            reasons.append("ACTIVE_LEASE")
        else:
            try:
                await TurnCoordinator().safe_to_resume(repo, context, turn)
            except CoordinationFailure as error:
                reasons.append(error.code)
                decision = "stale" if error.code == "STALE_TURN" else "review_required"
            else:
                if turn.prepared_output is not None:
                    decision = "replay_prepared"
                    reasons.append("SEALED_OUTPUT_ONLY")
                else:
                    decision = "resume_guarded"
                    reasons.append("NO_UNCERTAIN_OR_BUSINESS_WRITE_EXECUTION")
    body = dict(
        observed_at=datetime.now(UTC),
        business_id=context.business_id,
        session_id=context.session_id,
        turn_id=turn.turn_id,
        message_id=turn.message_id,
        decision=decision,
        internal_reopen_required=turn.status == "failed",
        reasons=tuple(dict.fromkeys(reasons)),
        saved_lead_id=lead.lead_id if lead else None,
        callback_request_id=lead.callback_request_id if lead else None,
        executions=tuple(reviews),
        snapshot_checksum="",
    )
    report = RecoveryReport.model_validate(body)
    checksum = hashlib.sha256(
        json.dumps(
            report.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return report.model_copy(update={"snapshot_checksum": checksum})


async def inspect_cli(business_id: str, session_id: str, turn_id: str) -> RecoveryReport:
    settings = load_settings()
    business = BusinessRegistry.load(settings.business_config_dir).get(business_id)
    if business is None:
        raise RecordNotFound("Record not found.")
    # Deliberately bypass open_database: no index creation, seeding, worker or mutations.
    async with AsyncMongoClient[dict[str, Any]](
        settings.mongodb_uri.get_secret_value(),
        tz_aware=True,
        serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000,
        timeoutMS=5000,
    ) as client:
        db = client.get_default_database("ai_engagement")
        if db.name in {"admin", "config", "local"}:
            raise RecordNotFound("Record not found.")
        session = await db.sessions.find_one(
            {"business_id": business_id, "session_id": session_id},
            {"channel": 1, "sender_id": 1, "_id": 0},
        )
        if session is None:
            raise RecordNotFound("Record not found.")
        context = ExecutionContext(
            request_id=uuid4(),
            business_id=business_id,
            session_id=session_id,
            turn_id=turn_id,
            channel=session["channel"],
            sender_id=session["sender_id"],
        )
        return await inspect_recovery(Repository(Database(client, db), business), context)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--turn", required=True)
    parser.add_argument(
        "--disposition",
        choices=("review", "leave-session", "request-retry"),
        default="review",
        help="Record an operator intention only; never executes it",
    )
    parser.add_argument(
        "--audit-file", type=Path, help="Create a new redacted local audit file; never overwrite"
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(inspect_cli(args.business, args.session, args.turn))
    except (RecordNotFound, ValidationError, PyMongoError, KeyError, TypeError):
        raise SystemExit(
            "Recovery inspection unavailable; verify identifiers and database access."
        ) from None
    audit = {
        "report": report.model_dump(mode="json", by_alias=True),
        "operatorIntention": args.disposition,
        "actionPerformed": False,
        "retryCandidate": report.decision
        in {"replay_completed", "replay_prepared", "resume_guarded"},
    }
    encoded = json.dumps(audit, indent=2) + "\n"
    if args.audit_file:
        try:
            with args.audit_file.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        except OSError:
            raise SystemExit(
                "Audit file could not be created; choose a new writable path."
            ) from None
    print(encoded, end="")


if __name__ == "__main__":
    main()
