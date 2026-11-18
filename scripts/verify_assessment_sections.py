"""Actual Gemini assessment handoff/policies; fictional owned database; keys stay private."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

Path(".cache").mkdir(exist_ok=True)
sys.path.insert(0, "backend")
import httpx
from app.database import open_database
from app.knowledge import KnowledgeService
from app.knowledge_cli import fixture_inputs
from app.main import create_app
from app.seed import seed
from app.settings import load_settings
from pydantic import SecretStr


async def main():
    name = "ai_sections_audit_" + uuid4().hex
    settings = load_settings().model_copy(
        update={
            "mongodb_uri": SecretStr("mongodb://127.0.0.1:27017/" + name),
            "automation_enabled": True,
            "automation_poll_ms": 500,
        }
    )
    report = {
        "scope": "Literal assignment conversation and policy checks; actual Gemini; new fictional fixtures; disposable database only",
        "turns": [],
        "checks": [],
    }
    from app.generation import GenerationService
    from app.knowledge import KnowledgeReader

    diagnostics = {"retrievals": [], "selections": []}
    original_read = KnowledgeReader.read
    original_select = GenerationService.select_reply

    async def observed_read(reader, context, query):
        result = await original_read(reader, context, query)
        diagnostics["retrievals"].append(
            {
                "query": query.query,
                "status": result.status,
                "matches": [
                    {"document": m.source.document_id, "score": m.score}
                    for m in result.matches
                ],
            }
        )
        return result

    async def observed_select(service, data):
        result = await original_select(service, data)
        diagnostics["selections"].append(
            {
                "question": data.get("currentCustomerText"),
                "evidence": data.get("evidence"),
                "selection": result.model_dump(mode="json", by_alias=True),
            }
        )
        return result

    KnowledgeReader.read = observed_read
    GenerationService.select_reply = observed_select
    report["diagnostics"] = diagnostics
    async with open_database(settings) as db:
        try:
            await seed(db, settings.business_config_dir)
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                service = KnowledgeService(
                    db,
                    app.state.business_registry.get("training-institute"),
                    app.state.embedder,
                    settings,
                )
                for owner, document in fixture_inputs(settings):
                    if owner == "training-institute":
                        await service.ingest(document.model_copy(update={"version": 2}))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url="http://probe",
                    timeout=110,
                ) as client:
                    session = (
                        await client.post(
                            "/api/v1/businesses/training-institute/sessions", json={}
                        )
                    ).json()
                    last = 0

                    async def ask(text):
                        nonlocal last
                        delay = 30 - (time.monotonic() - last)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        r = await client.post(
                            "/api/v1/businesses/training-institute/sessions/"
                            + session["sessionId"]
                            + "/messages",
                            headers={"X-Session-Token": session["sessionToken"]},
                            json={
                                "messageId": str(uuid4()),
                                "content": text,
                                "timestamp": datetime.now(UTC).isoformat(),
                            },
                        )
                        last = time.monotonic()
                        body = r.json()
                        trace = {}
                        if r.status_code == 200:
                            turn = await db.db.turns.find_one(
                                {"message_id": body["messageId"]}
                            )
                            trace = (turn or {}).get("agent_trace") or {}
                        report["turns"].append(
                            {
                                "input": text,
                                "httpStatus": r.status_code,
                                "response": body,
                                "trace": trace,
                            }
                        )
                        print(
                            "Turn",
                            len(report["turns"]),
                            trace.get("outcome"),
                            flush=True,
                        )
                        return body

                    def check(identifier, expected, passed):
                        report["checks"].append(
                            {
                                "id": identifier,
                                "expected": expected,
                                "passed": bool(passed),
                            }
                        )
                        print(identifier, "PASS" if passed else "FAIL", flush=True)

                    search = await ask(
                        "I am looking for a data science course under INR 30000."
                    )
                    check(
                        "S05-search",
                        "Search returns DS101 and DS202 within budget",
                        all(
                            code in search.get("reply", "")
                            for code in ["DS101", "DS202"]
                        ),
                    )
                    details = await ask("Is the second one available on weekends?")
                    actions = details.get("actions", [])
                    check(
                        "S05-ordinal",
                        "Second item resolves to a fresh DS202 details operation",
                        any(
                            a["result"]["status"] == "success"
                            and a["result"]["tool"] == "getCourseDetails"
                            and a["result"]["data"].get("course", {}).get("courseId")
                            == "DS202"
                            for a in actions
                        ),
                    )
                    contact = await ask("Great, I am interested. Can someone call me?")
                    check(
                        "S05-contacts",
                        "Asks name and phone before writes",
                        not any(
                            a["result"]["status"] == "success"
                            and a["result"]["tool"] in {"createLead", "requestCallback"}
                            for a in contact.get("actions", [])
                        )
                        and "name" in contact.get("reply", "").lower()
                        and any(
                            w in contact.get("reply", "").lower()
                            for w in ["phone", "mobile", "number"]
                        ),
                    )
                    saved = await ask("Rahul, 9876543210.")
                    lead = await db.db.leads.find_one(
                        {
                            "business_id": "training-institute",
                            "session_id": session["sessionId"],
                        }
                    )
                    report["savedLead"] = (
                        {k: lead.get(k) for k in ["customer", "callback_request_id"]}
                        if lead
                        else None
                    )
                    check(
                        "S05-lead-course",
                        "Saved enquiry retains DS202 rather than only generic data-science interest",
                        lead and lead["customer"].get("course_id") == "DS202",
                    )
                    check(
                        "S05-callback",
                        "Callback references the actual saved lead",
                        lead
                        and lead.get("callback_request_id")
                        and any(
                            a["result"]["status"] == "success"
                            and a["result"]["tool"] == "requestCallback"
                            and a["result"]["data"].get("leadId") == lead.get("lead_id")
                            for a in saved.get("actions", [])
                        ),
                    )
                    await ask(
                        "Can I join Advanced Python if I have never programmed before?"
                    )
                    p = report["turns"][-1]["response"]
                    check(
                        "S03-eligibility",
                        "Sourced basic-programming prerequisite",
                        "basic programming" in p.get("reply", "").lower()
                        and any(
                            s.get("documentId") == "eligibility"
                            for s in p.get("sources", [])
                        ),
                    )
                    await ask(
                        "Can I get a refund if I paid within seven days but already attended the first class?"
                    )
                    p = report["turns"][-1]["response"]
                    t = p.get("reply", "").lower()
                    check(
                        "S03-refund",
                        "Preserves both refund conditions with refund source",
                        any(w in t for w in ["seven", "7"])
                        and "first class" in t
                        and any(
                            s.get("documentId") == "refund"
                            for s in p.get("sources", [])
                        ),
                    )
        finally:
            assert db.db.name == name and name.startswith("ai_sections_audit_")
            await db.client.drop_database(name)
            report["ownedDatabaseRemoved"] = True
            report["passed"] = len(report["checks"]) == 7 and all(
                c["passed"] for c in report["checks"]
            )
            Path("docs/evidence/SECTIONS_FIX-after-live.json").write_text(
                json.dumps(report, indent=2, default=str) + "\n"
            )
    print("Literal example / policy probe finished:", report["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
