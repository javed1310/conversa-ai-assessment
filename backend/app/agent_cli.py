"""Explicit live M09 web-API qualification in a uniquely owned probe database."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import httpx
from pydantic import SecretStr

from app.contracts import ChatResult
from app.database import open_database
from app.main import create_app
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings, load_settings


class LiveCheckFailure(RuntimeError):
    pass


async def qualify(settings: Settings, target_name: str, pace: float) -> dict[str, object]:
    probe = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{target_name}"),
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    report: dict[str, object] = {
        "liveVerified": False,
        "generatedAt": datetime.now(UTC).isoformat(),
        "provider": "gemini",
        "model": settings.gemini_chat_model,
        "embeddingModel": settings.gemini_embedding_model,
        "cases": [],
        "scope": "Actual web API, agent, Gemini, retrieval, state and reads; no business writes.",
    }
    cases: list[dict[str, object]] = []
    async with open_database(probe) as database:
        try:
            await seed(database, settings.business_config_dir)
            # Reuse the actual M06-qualified vectors, without re-embedding unchanged documents.
            async with open_database(settings) as application:
                documents = await application.db.documents.find(
                    {
                        "status": "ready",
                        "embedding_model": settings.gemini_embedding_model,
                        "embedding_profile": "gemini-retrieval-document-query-l2-v1",
                    },
                    {"_id": 0},
                ).to_list(None)
                chunks = await application.db.chunks.find(
                    {
                        "embedding_model": settings.gemini_embedding_model,
                        "embedding_profile": "gemini-retrieval-document-query-l2-v1",
                    },
                    {"_id": 0},
                ).to_list(None)
            if len(documents) < 5 or len(chunks) < 5:
                raise LiveCheckFailure("READY_KNOWLEDGE_REQUIRED")
            await database.db.documents.insert_many(documents)
            await database.db.chunks.insert_many(chunks)
            app = create_app(probe)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://m09-live"
                ) as client:
                    started = await client.post(
                        "/api/v1/businesses/training-institute/sessions", json={}
                    )
                    if started.status_code != 201 or not started.json().get("engineReady"):
                        raise LiveCheckFailure("ENGINE_NOT_READY")
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
                            error_body = response.json()
                            latest = await database.db.turns.find_one(
                                {"session_id": session["sessionId"]}, sort=[("sequence", -1)]
                            )
                            report["httpFailure"] = {
                                "case": case,
                                "status": response.status_code,
                                "code": error_body.get("error", {}).get("code", "UNKNOWN"),
                                "turnStatus": (latest or {}).get("status"),
                                "checkpointStages": [
                                    item["stage"]
                                    for item in (latest or {}).get("state_checkpoints", [])
                                ],
                            }
                            raise LiveCheckFailure("HTTP_REQUEST_FAILED")
                        result = ChatResult.model_validate(response.json())
                        turn = await database.db.turns.find_one({"message_id": result.message_id})
                        assert turn is not None
                        trace = turn.get("agent_trace") or {}
                        record: dict[str, object] = {
                            "case": case,
                            "customerText": text,
                            "reply": result.reply,
                            "sources": [
                                s.model_dump(mode="json", by_alias=True) for s in result.sources
                            ],
                            "actions": [
                                a.model_dump(mode="json", by_alias=True) for a in result.actions
                            ],
                            "agentTrace": trace,
                            "passed": False,
                        }
                        cases.append(record)
                        report["cases"] = cases
                        print(f"Live case {case}: {trace.get('outcome', 'unknown')}", flush=True)
                        if trace.get("outcome") in {"provider_failure", "state_rejected"}:
                            raise LiveCheckFailure("AGENT_FALLBACK_REQUIRES_REVIEW")
                        return result, record

                    result, record = await ask("greeting", "Hello")
                    if (
                        result.actions
                        or result.sources
                        or cast(dict[str, object], record["agentTrace"]).get("operations")
                    ):
                        raise LiveCheckFailure("GREETING_OPERATIONS_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "eligibility", "Does Advanced Python require basic programming knowledge?"
                    )
                    if (
                        not any(s.document_id == "eligibility" for s in result.sources)
                        or "basic programming" not in result.reply.lower()
                    ):
                        raise LiveCheckFailure("ELIGIBILITY_FAILED")
                    record["passed"] = True
                    result, record = await ask("refund", "What are both conditions for a refund?")
                    if (
                        not any(s.document_id == "refund" for s in result.sources)
                        or "seven days" not in result.reply.lower()
                        or "first class" not in result.reply.lower()
                    ):
                        raise LiveCheckFailure("REFUND_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "search", "I want data science courses with a maximum budget of 30000 INR."
                    )
                    if not all(
                        value in result.reply for value in ("DS101", "DS202", "25000", "28000")
                    ):
                        raise LiveCheckFailure("SEARCH_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "second_reference",
                        "I want the second one. What are its fee, schedule and availability?",
                    )
                    if not all(
                        value in result.reply
                        for value in ("DS202", "28000", "weekend", "available")
                    ):
                        raise LiveCheckFailure("SECOND_REFERENCE_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "correction",
                        "Actually my budget is 20000 INR. Search again with that budget.",
                    )
                    state = await database.db.sessions.find_one(
                        {"session_id": session["sessionId"]}
                    )
                    assert state is not None
                    if (
                        state["state"]["fields"]["budget"] != 20000
                        or "No courses match" not in result.reply
                    ):
                        raise LiveCheckFailure("CORRECTION_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "current_availability",
                        "What are the current fee and availability for PY202?",
                    )
                    if "35000" not in result.reply or "currently full" not in result.reply:
                        raise LiveCheckFailure("AVAILABILITY_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "mixed_policy_and_current_facts",
                        "Does Python Basics have prerequisites, and what is its current fee "
                        "and schedule? Its course ID is PY101.",
                    )
                    if (
                        not result.sources
                        or not result.actions
                        or "12000" not in result.reply
                        or "weekend" not in result.reply
                    ):
                        raise LiveCheckFailure("MIXED_FAILED")
                    record["passed"] = True
                    result, record = await ask(
                        "unknown", "Which spacecraft is orbiting Neptune today?"
                    )
                    if result.sources or "verified information" not in result.reply:
                        raise LiveCheckFailure("UNKNOWN_FAILED")
                    record["passed"] = True
                    report["savedState"] = {
                        "budget": state["state"]["fields"]["budget"],
                        "selectedCourse": state["state"]["fields"]["course_id"],
                        "conflicts": state["state"]["selection_conflicts"],
                    }
                    if await database.db.leads.count_documents({}) != 0:
                        raise LiveCheckFailure("UNEXPECTED_WRITE")
                    report.update(
                        liveVerified=True,
                        leadCount=0,
                        applicationDataChanged=False,
                        reusedReadyDocuments=len(documents),
                        reusedRealChunks=len(chunks),
                    )
        except LiveCheckFailure as error:
            report["failureCode"] = str(error)
        finally:
            assert database.db.name == target_name and target_name.startswith(
                "ai_engagement_m09_probe_"
            )
            await database.client.drop_database(target_name)
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--pace-seconds", type=float, default=30)
    args = parser.parse_args()
    if not args.live or not 0 <= args.pace_seconds <= 60:
        parser.error("Use --live with a bounded pace of 0-60 seconds")
    name = "ai_engagement_m09_probe_" + uuid4().hex
    target = REPOSITORY_ROOT / "docs/evidence/M09-live-results.json"
    try:
        async with asyncio.timeout(900):
            report = await qualify(load_settings(), name, args.pace_seconds)
    except Exception:
        # Do not expose settings, HTTP bodies, customer tokens or provider exceptions.
        report = {"liveVerified": False, "failureCode": "PROBE_FAILED"}
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["liveVerified"]:
        raise SystemExit(
            f"M09 qualification incomplete: {report.get('failureCode')}. Review safe report."
        )
    print(f"PASS: live M09 API report written to {target}; owned probe database removed.")


if __name__ == "__main__":
    asyncio.run(main())
