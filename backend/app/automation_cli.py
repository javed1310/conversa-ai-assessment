"""Explicit live automatic-summary qualification or scoped same-event retry."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.automation import AutomationWorker
from app.businesses import BusinessRegistry
from app.contracts import ChatResult
from app.database import open_database
from app.gemini import GeminiClient
from app.generation import GenerationService
from app.main import create_app
from app.persistence_models import LeadRecord
from app.repositories import parse
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings, load_settings


class LiveAutomationFailure(RuntimeError):
    pass


async def qualify(settings: Settings) -> dict[str, object]:
    name = "ai_engagement_m11_probe_" + uuid4().hex
    probe = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{name}"),
            "ai_chat_enabled": True,
            "automation_enabled": True,
            "automation_poll_ms": 100,
        }
    )
    report: dict[str, object] = {
        "liveVerified": False,
        "generatedAt": datetime.now(UTC).isoformat(),
        "provider": "gemini",
        "chatModel": settings.gemini_chat_model,
        "summaryModel": settings.gemini_summary_model,
    }
    async with open_database(probe) as database:
        try:
            await seed(database, settings.business_config_dir)
            app = create_app(probe)
            async with app.router.lifespan_context(app):
                if app.state.automation_worker is None:
                    raise LiveAutomationFailure("WORKER_NOT_RUNNING")
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://m11-live"
                ) as client:
                    started = await client.post(
                        "/api/v1/businesses/training-institute/sessions", json={}
                    )
                    if started.status_code != 201:
                        raise LiveAutomationFailure("SESSION_FAILED")
                    session = started.json()
                    path = (
                        f"/api/v1/businesses/training-institute/sessions/"
                        f"{session['sessionId']}/messages"
                    )
                    text = (
                        "My name is Rahul and my phone is 9876543210. I want data science "
                        "and select DS202. My budget is 30000 and I prefer weekends. "
                        "Please save my enquiry."
                    )
                    response = await client.post(
                        path,
                        headers={"X-Session-Token": session["sessionToken"]},
                        json={
                            "messageId": str(uuid4()),
                            "content": text,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )
                    if response.status_code != 200:
                        raise LiveAutomationFailure("CHAT_FAILED")
                    result = ChatResult.model_validate(response.json())
                    report["chatReply"] = result.reply
                    turn = await database.db.turns.find_one({"message_id": result.message_id})
                    report["agentTrace"] = (turn or {}).get("agent_trace")
                    report["actions"] = [
                        action.model_dump(mode="json", by_alias=True) for action in result.actions
                    ]
                    if not any(
                        a.result.tool == "createLead" and a.result.status == "success"
                        for a in result.actions
                    ):
                        raise LiveAutomationFailure("LEAD_NOT_CREATED")
                    lead = None
                    async with asyncio.timeout(180):
                        while True:
                            document = await database.db.leads.find_one(
                                {"session_id": session["sessionId"]}
                            )
                            if document is not None:
                                lead = parse(LeadRecord, document)
                                if lead.automation.status == "completed":
                                    break
                                if (
                                    lead.automation.status == "failed"
                                    and not lead.automation.retryable
                                ):
                                    report["failureStage"] = lead.automation.failure_code
                                    raise LiveAutomationFailure("AUTOMATION_TERMINAL_FAILURE")
                            await asyncio.sleep(0.25)
                    assert lead is not None and lead.notification is not None
                    if lead.callback_requested_at is not None:
                        raise LiveAutomationFailure("UNSOLICITED_CALLBACK")
                    summary = lead.automation.summary
                    if (
                        summary is None
                        or summary.lead_id != lead.lead_id
                        or summary.customer_name != "Rahul"
                    ):
                        raise LiveAutomationFailure("SUMMARY_IDENTITY_FAILED")
                    if not all(
                        value in summary.summary
                        for value in ("DS202", "30000", "weekend", "+919876543210")
                    ):
                        raise LiveAutomationFailure("SUMMARY_FACTS_FAILED")
                    if lead.notification.payload != summary:
                        raise LiveAutomationFailure("NOTIFICATION_PAYLOAD_FAILED")
                    report.update(
                        leadId=lead.lead_id,
                        eventId=lead.automation.event_id,
                        summary=summary.model_dump(mode="json", by_alias=True),
                        notification=lead.notification.model_dump(mode="json", by_alias=True),
                        attempts=lead.automation.attempts,
                        automaticWithoutManualTick=True,
                        callbackRequired=False,
                    )
                    notification_id = lead.notification.notification_id
                    event_id = lead.automation.event_id
                    attempts = lead.automation.attempts
            # Restart with the same database; completed events must not produce another record.
            restarted = create_app(probe)
            async with restarted.router.lifespan_context(restarted):
                await asyncio.sleep(0.4)
            persisted = parse(
                LeadRecord, await database.db.leads.find_one({"lead_id": lead.lead_id})
            )
            if (
                persisted.notification is None
                or persisted.notification.notification_id != notification_id
                or persisted.automation.event_id != event_id
                or persisted.automation.attempts != attempts
            ):
                raise LiveAutomationFailure("RESTART_DUPLICATE")
            report.update(
                liveVerified=True,
                restartStable=True,
                leadCount=await database.db.leads.count_documents({}),
                applicationDataChanged=False,
                externalNotificationSent=False,
            )
        except LiveAutomationFailure as error:
            report["failureCode"] = str(error)
        finally:
            assert database.db.name == name and name.startswith("ai_engagement_m11_probe_")
            await database.client.drop_database(name)
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    live = modes.add_parser("qualify")
    live.add_argument("--live", action="store_true")
    retry = modes.add_parser("retry")
    retry.add_argument("--business-id", required=True)
    retry.add_argument("--lead-id", required=True)
    retry.add_argument("--event-id", required=True)
    args = parser.parse_args()
    settings = load_settings()
    if args.mode == "retry":
        async with open_database(settings) as database, GeminiClient(settings) as provider:
            worker = AutomationWorker(
                database,
                BusinessRegistry.load(settings.business_config_dir),
                GenerationService(provider),
                settings,
            )
            queued = await worker.retry(args.business_id, args.lead_id, args.event_id)
            print(
                "Retry queued for the same event."
                if queued
                else "No matching failed event was changed."
            )
        return
    if not args.live:
        parser.error("qualify requires --live to explicitly request actual provider calls")
    try:
        async with asyncio.timeout(360):
            report = await qualify(settings)
    except Exception:
        report = {"liveVerified": False, "failureCode": "PROBE_FAILED"}
    target = REPOSITORY_ROOT / "docs/evidence/M11-live-results.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["liveVerified"]:
        raise SystemExit(
            f"M11 qualification incomplete: {report.get('failureCode')}. Review safe report."
        )
    print(f"PASS: automatic live summary/notification and restart report written to {target}.")


if __name__ == "__main__":
    asyncio.run(main())
