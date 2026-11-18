"""Evaluate real Gemini conversations in a disposable, freshly embedded fixture database."""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.knowledge import DocumentInput, KnowledgeService  # noqa: E402
from app.knowledge_cli import fixture_inputs  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed import seed  # noqa: E402
from app.settings import Settings  # noqa: E402


async def evaluate(args):
    settings = Settings(_env_file=args.env_file, business_config_dir=ROOT / "businesses")
    if not settings.gemini_api_key or not settings.gemini_api_key.get_secret_value().strip():
        raise RuntimeError("A private Gemini key with generation/embedding quota is required.")
    name = "ai_dialogue_eval_" + uuid4().hex
    settings = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{name}"),
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "provider": "actual Gemini",
        "model": settings.gemini_chat_model,
        "embeddingModel": settings.gemini_embedding_model,
        "repeats": args.repeats,
        "cases": [],
        "fixtures": [],
        "applicationRecordsRead": False,
        "runtimeModified": False,
        "semanticReview": (
            "Required: inspect actual replies; assertions verify receipts/state/sources, "
            "not universal semantic accuracy."
        ),
    }
    output = ROOT / ".cache" / args.output
    output.parent.mkdir(exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        database = app.state.database
        try:
            await seed(database, settings.business_config_dir)
            for business, document in fixture_inputs(settings):
                service = KnowledgeService(
                    database,
                    app.state.business_registry.get(business),
                    app.state.embedder,
                    settings,
                )
                ready = await service.ingest(document)
                if ready.status != "ready":
                    raise RuntimeError("Fixture ingestion did not become ready")
                report["fixtures"].append(
                    {"business": business, "document": document.document_id, "ready": True}
                )
                save()
            last = 0.0
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://evaluation"
            ) as client:

                async def start(business="training-institute"):
                    response = await client.post(f"/api/v1/businesses/{business}/sessions", json={})
                    response.raise_for_status()
                    return response.json()

                async def ask(session, text, expected, check):
                    nonlocal last
                    await asyncio.sleep(max(0, args.pace_seconds - (time.monotonic() - last)))
                    started = time.monotonic()
                    response = await client.post(
                        f"/api/v1/businesses/{session['businessId']}/sessions/{session['sessionId']}/messages",
                        headers={"X-Session-Token": session["sessionToken"]},
                        json={
                            "messageId": str(uuid4()),
                            "content": text,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )
                    last = time.monotonic()
                    body = response.json()
                    record = await database.db.sessions.find_one(
                        {"business_id": session["businessId"], "session_id": session["sessionId"]}
                    )
                    lead = await database.db.leads.find_one(
                        {"business_id": session["businessId"], "session_id": session["sessionId"]}
                    )
                    checks = {}
                    if response.status_code == 200 and "reply" in body:
                        try:
                            checks = check(body, record, lead)
                        except (KeyError, TypeError, ValueError):
                            checks = {"expected_structure": False}
                    else:
                        checks = {"completed_response": False}
                    if body.get("reply", "").startswith(
                        "I don't have verified information"
                    ) and not re.search(
                        r"\b(refund|policy|eligibility|prerequisites?|experience)\b", text, re.I
                    ):
                        reads = [a.get("result", {}) for a in body.get("actions", [])]
                        if any(
                            result.get("status") == "success"
                            and (
                                (
                                    result.get("tool") == "searchCourses"
                                    and result.get("data", {}).get("courses")
                                )
                                or result.get("tool")
                                in {"getCourseDetails", "getMembershipDetails"}
                            )
                            for result in reads
                        ):
                            checks["checked_read_answered"] = False
                    case = {
                        "business": session["businessId"],
                        "question": text,
                        "expected": expected,
                        "seconds": round(last - started, 3),
                        "httpStatus": response.status_code,
                        "actual": body,
                        "observedState": {
                            "budget": field(record, "budget"),
                            "courseId": field(record, "course_id"),
                            "displayedCourses": [
                                item["course_id"]
                                for item in record["state"]["last_displayed_courses"]
                            ],
                        },
                        "checks": checks,
                        "passed": bool(checks) and all(checks.values()),
                    }
                    report["cases"].append(case)
                    save()
                    print(
                        f"{'PASS' if case['passed'] else 'FAIL'} {text}: {case['seconds']}s",
                        flush=True,
                    )
                    return body

                def success(body, tool):
                    return [
                        a["result"]
                        for a in body.get("actions", [])
                        if a.get("result", {}).get("tool") == tool
                        and a["result"].get("status") == "success"
                    ]

                def field(record, key):
                    return record["state"]["fields"].get(key)

                for repeat in range(args.repeats):
                    session = await start()
                    search = [
                        "I want data science courses with a maximum budget of 30000 INR.",
                        "suggest me data science course till 30000 budget",
                    ][repeat % 2]
                    await ask(
                        session,
                        search,
                        "Search current courses under 30000; retain both data science options.",
                        lambda b, r, lead: {
                            "checked_search": bool(success(b, "searchCourses")),
                            "budget": field(r, "budget") == 30000,
                            "both_options": {
                                item["course_id"] for item in r["state"]["last_displayed_courses"]
                            }
                            == {"DS101", "DS202"},
                        },
                    )
                    await ask(
                        session,
                        "second one",
                        "Resolve second displayed course to DS202.",
                        lambda b, r, lead: {
                            "selected_DS202": field(r, "course_id") == "DS202",
                            "details_tool": bool(success(b, "getCourseDetails")),
                        },
                    )
                    await ask(
                        session,
                        "are you sure it runs on weekeds",
                        "Verify the selected course's weekend schedule despite typo.",
                        lambda b, r, lead: {
                            "fresh_details": bool(success(b, "getCourseDetails")),
                            "weekend_answer": "weekend" in b["reply"].lower(),
                        },
                    )
                    await ask(
                        session,
                        "i want to enquire about it",
                        "Ask for missing contact details without saving a lead.",
                        lambda b, r, lead: {
                            "asks_for_details": "name" in b["reply"].lower()
                            and "phone" in b["reply"].lower(),
                            "no_lead": lead is None,
                        },
                    )
                    await ask(
                        session,
                        "My name is Evaluation Learner",
                        "Remember name and ask for missing phone.",
                        lambda b, r, lead: {
                            "name_saved": field(r, "name") == "Evaluation Learner",
                            "no_premature_lead": lead is None,
                        },
                    )
                    await ask(
                        session,
                        "9876543210",
                        "Save the qualified enquiry, without requesting an unsolicited callback.",
                        lambda b, r, lead: {
                            "saved_lead": bool(success(b, "createLead")) and lead is not None,
                            "no_callback": lead is not None
                            and lead.get("callback_request_id") is None,
                        },
                    )
                    await ask(
                        session,
                        "will i get a callback",
                        "Explain current status and offer callback without performing a write.",
                        lambda b, r, lead: {
                            "no_write": not b.get("actions"),
                            "not_requested": lead is not None
                            and lead.get("callback_request_id") is None,
                        },
                    )
                    await ask(
                        session,
                        "yes",
                        "Create a callback for the actual saved lead.",
                        lambda b, r, lead: {
                            "saved_callback": bool(success(b, "requestCallback"))
                            and bool(lead.get("callback_request_id"))
                        },
                    )
                    await ask(
                        session,
                        "okay",
                        "Acknowledge naturally without repeating writes.",
                        lambda b, r, lead: {
                            "no_tools": not b.get("actions"),
                            "no_repeated_reference": "reference" not in b["reply"].lower(),
                            "callback_retained": bool(lead.get("callback_request_id")),
                        },
                    )

                for question, document in [
                    ("Does Advanced Python require programming experience?", "eligibility"),
                    ("I've never written code. Could I begin with Python Basics?", "eligibility"),
                    ("Can I get my money back after attending the first class?", "refund"),
                    ("What if I ask for a refund eight days after paying?", "refund"),
                ]:
                    session = await start()
                    await ask(
                        session,
                        question,
                        (
                            f"Answer specifically from {document}, citing an owned source; "
                            "inspect answer meaning manually."
                        ),
                        lambda b, r, lead, doc=document: {
                            "expected_source": any(
                                s.get("documentId") == doc for s in b.get("sources", [])
                            ),
                            "no_lead": lead is None,
                        },
                    )
                session = await start()
                await ask(
                    session,
                    "What Python courses do you offer?",
                    "Browse or ask for missing search preferences; no invented course data.",
                    lambda b, r, lead: {
                        "no_write": lead is None,
                        "discovery_or_clarification": bool(success(b, "searchCourses"))
                        or "budget" in b["reply"].lower(),
                    },
                )
                await ask(
                    session,
                    "My maximum budget is 30000 INR",
                    "Use the stated budget with the previously supplied Python interest.",
                    lambda b, r, lead: {
                        "budget_saved": field(r, "budget") == 30000,
                        "search": bool(success(b, "searchCourses")),
                    },
                )
                await ask(
                    session,
                    "Actually, my budget is 20k",
                    "Update budget to 20000 and use the correction.",
                    lambda b, r, lead: {
                        "corrected_budget": field(r, "budget") == 20000,
                        "new_search": bool(success(b, "searchCourses")),
                    },
                )
                session = await start("fitness-studio")
                await ask(
                    session,
                    "What membership options are available?",
                    "Retrieve this business's membership records.",
                    lambda b, r, lead: {
                        "membership_tool": bool(success(b, "getMembershipDetails")),
                        "no_course_tools": not any(
                            a["result"]["tool"] in {"searchCourses", "getCourseDetails"}
                            for a in b.get("actions", [])
                        ),
                    },
                )
                await ask(
                    session,
                    "first one",
                    "Resolve the first displayed membership.",
                    lambda b, r, lead: {
                        "membership_details": bool(success(b, "getMembershipDetails")),
                        "monthly": "monthly" in b["reply"].lower(),
                    },
                )
                await ask(
                    session,
                    "Can I get a refund on a data science course?",
                    "Do not retrieve training documents in fitness context.",
                    lambda b, r, lead: {
                        "no_foreign_sources": all(
                            s.get("businessId") == "fitness-studio" for s in b.get("sources", [])
                        ),
                        "no_course_tools": not any(
                            a["result"]["tool"] in {"searchCourses", "getCourseDetails"}
                            for a in b.get("actions", [])
                        ),
                    },
                )
                document = DocumentInput(
                    document_id="evaluation-library-policy",
                    title="Practice library access",
                    format="markdown",
                    content=(
                        "Practice library access: Enrolled learners can use the fictional "
                        "institute's "
                        "practice library on Saturdays from 10 AM to noon. Bring your learner ID. "
                        "This policy establishes no fees or course seat availability."
                    ),
                )
                service = KnowledgeService(
                    database,
                    app.state.business_registry.get("training-institute"),
                    app.state.embedder,
                    settings,
                )
                await service.ingest(document)
                session = await start()
                await ask(
                    session,
                    "When can I access the practice library and what should I bring?",
                    "Retrieve and cite the newly ingested library policy.",
                    lambda b, r, lead: {
                        "new_source": any(
                            s.get("documentId") == document.document_id
                            for s in b.get("sources", [])
                        ),
                        "saturday": "saturday" in b["reply"].lower(),
                    },
                )
                session = await start("fitness-studio")
                await ask(
                    session,
                    "When can I access the practice library?",
                    "Do not retrieve the institute's newly added policy in fitness.",
                    lambda b, r, lead: {
                        "no_foreign_sources": all(
                            s.get("businessId") == "fitness-studio" for s in b.get("sources", [])
                        ),
                        "no_library_claim": "saturday" not in b["reply"].lower(),
                    },
                )
        except Exception as error:
            report["executionError"] = type(error).__name__
            print("Evaluation interrupted:", type(error).__name__, flush=True)
        finally:
            assert database.db.name == name and name.startswith("ai_dialogue_eval_")
            await database.client.drop_database(name)
            report["ownedDatabaseRemoved"] = True
            report["finishedAt"] = datetime.now(UTC).isoformat()
            cases = report["cases"]
            report["passedChecks"] = sum(c["passed"] for c in cases)
            report["totalCases"] = len(cases)
            if cases:
                report["medianTurnSeconds"] = statistics.median(c["seconds"] for c in cases)
            save()
    return (
        0
        if report.get("executionError") is None and all(c["passed"] for c in report["cases"])
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--output",
        choices=["live-dialogue-evaluation.json", "live-dialogue-fixed.json"],
        default="live-dialogue-evaluation.json",
    )
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--pace-seconds", type=int, choices=[15, 30, 60], default=15)
    raise SystemExit(asyncio.run(evaluate(parser.parse_args())))
