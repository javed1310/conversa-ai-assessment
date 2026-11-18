"""Check course discovery through enquiry collection with Gemini in a disposable database."""

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
        "scope": "Six-turn discovery, acceptance, contact collection and saved enquiry; Gemini.",
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

                    prompts = [
                        "I want data science courses with a maximum budget of 30000 INR.",
                        "second one",
                        "i would prefer the weekend batch",
                        "yes",
                        "My name is Rahul.",
                        "My phone number is 9876543210.",
                    ]
                    replies = []
                    for index, prompt in enumerate(prompts):
                        result, case = await ask(str(index + 1), prompt)
                        replies.append(result.reply)
                        if index == 0:
                            assert all(
                                v in result.reply for v in ("DS101", "DS202", "25000", "28000")
                            )
                        elif index == 1:
                            assert all(
                                v in result.reply
                                for v in ("DS202", "28000", "weekend", "available")
                            )
                        elif index == 2:
                            assert "fits your preference" in result.reply
                            assert "Would you like to enquire" in result.reply
                            assert "28000" not in result.reply
                        elif index == 3:
                            assert "name" in result.reply and "mobile number" in result.reply
                            assert not result.actions and "28000" not in result.reply
                        elif index == 4:
                            assert (
                                "mobile number" in result.reply
                                and "share your name" not in result.reply
                            )
                        else:
                            assert "Your enquiry is saved" in result.reply
                        if index < 5:
                            assert await database.db.leads.count_documents({}) == 0
                        case["passed"] = True
                    current = await database.db.sessions.find_one(
                        {"session_id": session["sessionId"]}
                    )
                    assert current is not None
                    assert current["state"]["fields"]["schedule"] == "weekend"
                    assert await database.db.leads.count_documents({}) == 1
                    saved = await database.db.leads.find_one({})
                    assert saved is not None and saved["customer"]["course_id"] == "DS202"
                    assert saved.get("callback_request_id") is None
                    assert len(set(replies)) == 6
                    report.update(
                        liveVerified=True,
                        validatedSchedule="weekend",
                        leadCount=1,
                        callbackRequested=False,
                        applicationDataChanged=False,
                        realNotificationSent=False,
                    )
                    report["measurements"] = app.state.database.measurements.recent()
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
    target = REPOSITORY_ROOT / "docs/evidence/DIALOGUE_FLOW-live-results.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["liveVerified"]:
        raise SystemExit(
            f"Conversation check incomplete: {report.get('failureCode')}. Review safe report."
        )
    print(f"PASS: live conversation report written to {target}; owned probe database removed.")


if __name__ == "__main__":
    asyncio.run(main())
