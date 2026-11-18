"""Explicit live web/mock and second-business qualification, using a disposable database."""

import argparse
import asyncio
import json
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.contracts import ChatResult
from app.database import open_database
from app.main import create_app
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings, load_settings


class QualificationFailure(RuntimeError):
    pass


async def qualify(settings: Settings, pace: float) -> dict[str, object]:
    name = "ai_engagement_m12_probe_" + uuid4().hex
    keys = {
        business: SecretStr(secrets.token_urlsafe(32))
        for business in ("training-institute", "fitness-studio")
    }
    probe = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{name}"),
            "mock_channel_keys": keys,
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    cases: list[dict[str, object]] = []
    report: dict[str, object] = {
        "liveVerified": False,
        "generatedAt": datetime.now(UTC).isoformat(),
        "model": settings.gemini_chat_model,
        "embeddingModel": settings.gemini_embedding_model,
        "provider": "gemini",
        "cases": cases,
        "scope": "Actual web/mock HTTP, same Gemini engine, institute and fitness facts.",
    }
    async with open_database(probe) as database:
        try:
            await seed(database, settings.business_config_dir)
            async with open_database(settings) as application:
                profile = {
                    "embedding_model": settings.gemini_embedding_model,
                    "embedding_profile": "gemini-retrieval-document-query-l2-v1",
                }
                documents = await application.db.documents.find(
                    profile | {"status": "ready"}, {"_id": 0}
                ).to_list(None)
                chunks = await application.db.chunks.find(profile, {"_id": 0}).to_list(None)
            if len(documents) < 5 or len(chunks) < 5:
                raise QualificationFailure("READY_KNOWLEDGE_REQUIRED")
            await database.db.documents.insert_many(documents)
            await database.db.chunks.insert_many(chunks)
            app = create_app(probe)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://m12-live"
                ) as client:
                    sessions: set[str] = set()
                    for business in keys:
                        text = (
                            "What are the current fee and details for course PY101?"
                            if business == "training-institute"
                            else "What are the current membership fees and durations, "
                            "and what is the introductory trial visit policy?"
                        )
                        facts: list[dict[str, object]] = []
                        for channel in ("web", "mock"):
                            if cases:
                                await asyncio.sleep(pace)
                            identifier = str(uuid4())
                            timestamp = datetime.now(UTC).isoformat()
                            if channel == "web":
                                started = await client.post(
                                    f"/api/v1/businesses/{business}/sessions", json={}
                                )
                                if started.status_code != 201 or not started.json()["engineReady"]:
                                    raise QualificationFailure("ENGINE_NOT_READY")
                                session = started.json()
                                path = (
                                    f"/api/v1/businesses/{business}/sessions/"
                                    f"{session['sessionId']}/messages"
                                )
                                headers = {"X-Session-Token": session["sessionToken"]}
                                body = {
                                    "messageId": identifier,
                                    "content": text,
                                    "timestamp": timestamp,
                                }
                            else:
                                path = "/api/v1/mock/messages"
                                headers = {"X-Mock-Channel-Key": keys[business].get_secret_value()}
                                body = {
                                    "event_id": identifier,
                                    "author": "same-external-sender",
                                    "text": text,
                                    "timestamp": timestamp,
                                }
                            response = await client.post(path, headers=headers, json=body)
                            observed: dict[str, object] = {
                                "business": business,
                                "channel": channel,
                                "customerText": text,
                                "httpStatus": response.status_code,
                                "passed": False,
                            }
                            cases.append(observed)
                            if response.status_code != 200:
                                observed["errorCode"] = response.json().get("error", {}).get("code")
                                raise QualificationFailure("HTTP_FAILED")
                            outgoing = response.json()
                            if channel == "mock":
                                if (
                                    outgoing["recipient"] != body["author"]
                                    or outgoing["in_reply_to"] != identifier
                                ):
                                    raise QualificationFailure("ROUTING_FAILED")
                                outgoing = outgoing["result"]
                            result = ChatResult.model_validate(outgoing)
                            turn = await database.db.turns.find_one({"message_id": identifier})
                            trace = (turn or {}).get("agent_trace") or {}
                            observed.update(
                                reply=result.reply,
                                agentTrace=trace,
                                sources=[
                                    s.model_dump(mode="json", by_alias=True) for s in result.sources
                                ],
                                actions=[
                                    a.model_dump(mode="json", by_alias=True) for a in result.actions
                                ],
                            )
                            print(
                                f"Live {business}/{channel}: {trace.get('outcome', 'unknown')}",
                                flush=True,
                            )
                            if trace.get("outcome") in {"provider_failure", "state_rejected"}:
                                raise QualificationFailure("AGENT_FALLBACK")
                            expected = (
                                "getCourseDetails"
                                if business == "training-institute"
                                else "getMembershipDetails"
                            )
                            successful = [
                                a
                                for a in result.actions
                                if a.result.tool == expected and a.result.status == "success"
                            ]
                            if not successful:
                                raise QualificationFailure("EXPECTED_TOOL_MISSING")
                            tool_result = successful[0].result
                            assert tool_result.status == "success"
                            facts.append(tool_result.data)
                            if business == "fitness-studio":
                                if (
                                    not result.sources
                                    or any(s.business_id != business for s in result.sources)
                                    or "trial" not in result.reply.lower()
                                    or "2000" not in result.reply
                                    or "5400" not in result.reply
                                ):
                                    raise QualificationFailure("FITNESS_FACTS_MISSING")
                            sessions.add(result.session_id)
                            if channel == "mock":
                                replay = await client.post(path, headers=headers, json=body)
                                if replay.status_code != 200 or replay.json()["result"] != outgoing:
                                    raise QualificationFailure("REPLAY_FAILED")
                            observed["passed"] = True
                        if facts[0] != facts[1]:
                            raise QualificationFailure("CHANNEL_FACTS_DIFFER")
                    if len(sessions) != 4 or await database.db.leads.count_documents({}) != 0:
                        raise QualificationFailure("ISOLATION_FAILED")
                    report.update(
                        liveVerified=True,
                        distinctSessions=4,
                        externalMappings=2,
                        completedTurns=4,
                        applicationDataChanged=False,
                        realExternalMessageSent=False,
                        automationEnabled=False,
                    )
        except QualificationFailure as error:
            report["failureCode"] = str(error)
        finally:
            assert database.db.name == name and name.startswith("ai_engagement_m12_probe_")
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
        async with asyncio.timeout(600):
            report = await qualify(load_settings(), args.pace_seconds)
    except Exception:
        report = {"liveVerified": False, "failureCode": "PROBE_FAILED"}
    target = REPOSITORY_ROOT / "docs/evidence/M12-live-results.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["liveVerified"]:
        raise SystemExit(
            f"M12 qualification incomplete: {report.get('failureCode')}; see safe report."
        )
    print("PASS: M12 live web/mock qualification; owned probe database removed.")


if __name__ == "__main__":
    asyncio.run(main())
