"""Explicit M10 actual Gemini inquiry-to-callback qualification in a disposable local database."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.contracts import ChatResult
from app.database import open_database
from app.main import create_app
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings, load_settings


class LiveActionCheckFailure(RuntimeError):
    pass


async def qualify(settings: Settings, pace: float) -> dict[str, object]:
    name = "ai_engagement_m10_probe_" + uuid4().hex
    probe = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{name}"),
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    cases: list[dict[str, object]] = []
    report: dict[str, object] = {
        "liveVerified": False,
        "generatedAt": datetime.now(UTC).isoformat(),
        "model": settings.gemini_chat_model,
        "provider": "gemini",
        "cases": cases,
        "scope": "Actual Gemini/HTTP/state/lead/callback using fictional assessment data.",
    }
    async with open_database(probe) as database:
        try:
            await seed(database, settings.business_config_dir)
            app = create_app(probe)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://m10-live"
                ) as client:
                    started = await client.post(
                        "/api/v1/businesses/training-institute/sessions", json={}
                    )
                    if started.status_code != 201 or not started.json().get("engineReady"):
                        raise LiveActionCheckFailure("ENGINE_NOT_READY")
                    session = started.json()
                    path = (
                        f"/api/v1/businesses/training-institute/sessions/"
                        f"{session['sessionId']}/messages"
                    )
                    headers = {"X-Session-Token": session["sessionToken"]}

                    async def ask(case: str, text: str) -> tuple[ChatResult, dict[str, object]]:
                        if cases:
                            await asyncio.sleep(pace)
                        response = await client.post(
                            path,
                            headers=headers,
                            json={
                                "messageId": str(uuid4()),
                                "content": text,
                                "timestamp": datetime.now(UTC).isoformat(),
                            },
                        )
                        if response.status_code != 200:
                            report["httpFailure"] = {
                                "case": case,
                                "status": response.status_code,
                                "code": response.json().get("error", {}).get("code", "UNKNOWN"),
                            }
                            raise LiveActionCheckFailure("HTTP_FAILED")
                        result = ChatResult.model_validate(response.json())
                        turn = await database.db.turns.find_one({"message_id": result.message_id})
                        trace = (turn or {}).get("agent_trace") or {}
                        observed: dict[str, object] = {
                            "case": case,
                            "customerText": text,
                            "reply": result.reply,
                            "actions": [
                                a.model_dump(mode="json", by_alias=True) for a in result.actions
                            ],
                            "agentTrace": trace,
                            "passed": False,
                        }
                        cases.append(observed)
                        print(f"Live case {case}: {trace.get('outcome', 'unknown')}", flush=True)
                        if trace.get("outcome") in {"provider_failure", "state_rejected"}:
                            raise LiveActionCheckFailure("AGENT_FALLBACK")
                        return result, observed

                    result, case = await ask(
                        "course_search",
                        "I want data science courses with a maximum budget of 30000 INR.",
                    )
                    if not all(
                        value in result.reply for value in ("DS101", "DS202", "25000", "28000")
                    ):
                        raise LiveActionCheckFailure("SEARCH_FAILED")
                    case["passed"] = True
                    result, case = await ask(
                        "second_course_weekend",
                        "I choose the second one. I prefer weekends. "
                        "Please show its fee and availability.",
                    )
                    if not all(
                        value in result.reply
                        for value in ("DS202", "28000", "weekend", "available")
                    ):
                        raise LiveActionCheckFailure("REFERENCE_FAILED")
                    case["passed"] = True
                    result, case = await ask(
                        "callback_without_contact", "Please arrange a callback about this course."
                    )
                    if (
                        await database.db.leads.count_documents({})
                        or "phone" not in result.reply.lower()
                        or "name" not in result.reply.lower()
                    ):
                        raise LiveActionCheckFailure("PREMATURE_LEAD_OR_MISSING_CONTACT")
                    case["passed"] = True
                    result, case = await ask(
                        "contact_completes_dependent_chain",
                        "My name is Rahul and my phone number is 9876543210.",
                    )
                    lead = await database.db.leads.find_one({"session_id": session["sessionId"]})
                    tools = [a.result.tool for a in result.actions if a.result.status == "success"]
                    if lead is None or "createLead" not in tools or "requestCallback" not in tools:
                        raise LiveActionCheckFailure("DEPENDENT_CHAIN_FAILED")
                    if tools.index("createLead") >= tools.index("requestCallback"):
                        raise LiveActionCheckFailure("ORDER_FAILED")
                    customer = lead["customer"]
                    if (
                        customer["name"] != "Rahul"
                        or customer["phone"] != "+919876543210"
                        or customer["course_id"] != "DS202"
                        or customer["budget"] != 30000
                        or customer["schedule"] != "weekend"
                        or not lead.get("callback_request_id")
                    ):
                        raise LiveActionCheckFailure("SAVED_FIELDS_FAILED")
                    callback_id = lead["callback_request_id"]
                    event_id = lead["automation"]["event_id"]
                    if lead["lead_id"] not in result.reply or callback_id not in result.reply:
                        raise LiveActionCheckFailure("RECEIPT_FAILED")
                    case["passed"] = True
                    result, case = await ask(
                        "repeated_callback_intent",
                        "Please request the callback again for this same enquiry.",
                    )
                    again = await database.db.leads.find_one({"session_id": session["sessionId"]})
                    assert again is not None
                    if (
                        await database.db.leads.count_documents({}) != 1
                        or again["callback_request_id"] != callback_id
                        or again["automation"]["event_id"] != event_id
                    ):
                        raise LiveActionCheckFailure("DUPLICATE_EFFECT")
                    case["passed"] = True
                    result, case = await ask(
                        "historical_snapshot_correction",
                        "Actually my phone is 9876543211. Please note this correction.",
                    )
                    current = await database.db.sessions.find_one(
                        {"session_id": session["sessionId"]}
                    )
                    saved = await database.db.leads.find_one({"session_id": session["sessionId"]})
                    assert current is not None and saved is not None
                    if (
                        current["state"]["fields"]["phone"] != "+919876543211"
                        or saved["customer"]["phone"] != "+919876543210"
                        or "not updated" not in result.reply
                    ):
                        raise LiveActionCheckFailure("HISTORICAL_SNAPSHOT_FAILED")
                    if (
                        saved["automation"]["status"] != "pending"
                        or saved["automation"]["summary"] is not None
                        or saved["notification"] is not None
                    ):
                        raise LiveActionCheckFailure("UNEXPECTED_AUTOMATION")
                    case["passed"] = True
                    report.update(
                        liveVerified=True,
                        leadCount=1,
                        leadId=saved["lead_id"],
                        callbackRequestId=callback_id,
                        eventId=event_id,
                        automationStatus="pending",
                        savedCustomer=saved["customer"],
                        activePhone=current["state"]["fields"]["phone"],
                        applicationDataChanged=False,
                        realNotificationSent=False,
                    )
        except LiveActionCheckFailure as error:
            report["failureCode"] = str(error)
        finally:
            assert database.db.name == name and name.startswith("ai_engagement_m10_probe_")
            await database.client.drop_database(name)
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--pace-seconds", type=float, default=30)
    args = parser.parse_args()
    if not args.live or not 0 <= args.pace_seconds <= 60:
        parser.error("Use --live and a pace between 0 and 60 seconds")
    try:
        async with asyncio.timeout(900):
            report = await qualify(load_settings(), args.pace_seconds)
    except Exception:
        report = {"liveVerified": False, "failureCode": "PROBE_FAILED"}
    target = REPOSITORY_ROOT / "docs/evidence/M10-live-results.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["liveVerified"]:
        raise SystemExit(
            f"M10 qualification incomplete: {report.get('failureCode')}. Review safe report."
        )
    print(f"PASS: live M10 report written to {target}; owned probe database removed.")


if __name__ == "__main__":
    asyncio.run(main())
