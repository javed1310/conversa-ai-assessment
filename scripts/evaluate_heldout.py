"""Measure a frozen candidate with real Gemini; no production settings or routing changes."""

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
from app.knowledge import DocumentInput, KnowledgeService, RetrievalQuery  # noqa: E402
from app.knowledge_cli import fixture_inputs  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed import seed  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.training_behavior import ReferenceArguments  # noqa: E402
from evaluation_support import observe_call, retrieval_metrics, safe_stop, score_step  # noqa: E402
from pydantic import SecretStr  # noqa: E402


def fingerprint(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def dataset_digest(path):
    return hashlib.sha256(
        json.dumps(json.loads(path.read_text()), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def main(args):
    dataset_path = ROOT / "evaluation/heldout_cases.json"
    manifest = json.loads((ROOT / "evaluation/baseline_manifest.json").read_text())
    data = json.loads(dataset_path.read_text())
    if dataset_digest(dataset_path) != manifest["heldoutSha256"]:
        raise RuntimeError("Frozen dataset changed; do not silently reuse its baseline label")
    runtime_hash = fingerprint((ROOT / "backend/app").glob("*.py"))
    corpus_hash = fingerprint(
        p for p in (ROOT / "businesses").rglob("*") if p.suffix in {".md", ".json"}
    )
    if corpus_hash != manifest["corpusFingerprint"]:
        raise RuntimeError("Corpus differs from the frozen baseline")
    if runtime_hash != manifest["runtimeFingerprint"] and not args.candidate_label:
        raise RuntimeError(
            "Runtime differs from the frozen baseline; create a labelled candidate run"
        )
    if args.case_ids:
        selected = set(args.case_ids)
        if selected - {case["id"] for case in data["cases"]}:
            raise RuntimeError("Unknown case identifier")
        data["cases"] = [case for case in data["cases"] if case["id"] in selected]
    settings = Settings(_env_file=args.env_file, business_config_dir=ROOT / "businesses")
    if not settings.gemini_api_key:
        raise RuntimeError("A private Gemini key and quota are required")
    if settings.rag_min_score is None:
        raise RuntimeError(
            "Configure the documented baseline RAG_MIN_SCORE; this evaluator does not tune it"
        )
    database_name = "ai_heldout_" + uuid4().hex
    settings = settings.model_copy(
        update={
            "mongodb_uri": SecretStr("mongodb://127.0.0.1:27017/" + database_name),
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    report = {
        "startedAt": datetime.now(UTC).isoformat(),
        "baselineCommit": data["baselineCommit"],
        "runType": "regression_candidate" if args.candidate_label else "frozen_baseline",
        "candidateLabel": args.candidate_label,
        "note": "Cases used for fixes are regression evidence, not fresh held-out evidence.",
        "runtimeFingerprint": runtime_hash,
        "datasetSha256": manifest["heldoutSha256"],
        "chatModel": settings.gemini_chat_model,
        "embeddingModel": settings.gemini_embedding_model,
        "threshold": settings.rag_min_score,
        "plannedTurns": sum(len(case["steps"]) for case in data["cases"]),
        "cases": [],
        "retrieval": [],
        "applicationRecordsRead": False,
        "runtimeModified": False,
        "humanSemanticReview": (
            "Required. Pattern/receipt/source checks alone are not a universal accuracy score."
        ),
        "traceMeaning": (
            "Observed operation, schema validation and stop category; not hidden model reasoning. "
            "Argument values, result bodies, identifiers, excerpts and credentials are excluded."
        ),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    prefix = "regression-candidate" if args.candidate_label else "heldout-baseline"
    output = ROOT / f".cache/{prefix}-{stamp}.json"
    output.parent.mkdir(exist_ok=True)

    def save():
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    save()
    print(f"Private report: {output}", flush=True)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        db = app.state.database
        try:
            await seed(db, settings.business_config_dir)
            for business, document in fixture_inputs(settings):
                await KnowledgeService(
                    db, app.state.business_registry.get(business), app.state.embedder, settings
                ).ingest(document)
            engine = app.state.gateway.engine
            schemas = {
                name: definition.arguments for name, definition in engine.tools._definitions.items()
            }
            schemas.update(readKnowledge=RetrievalQuery, resolveCourseReference=ReferenceArguments)
            attempts, retrieved = [], []
            original_operation, original_read = engine._operate, engine.knowledge.read

            async def observe_operation(context, call, service, work):
                return await observe_call(
                    original_operation,
                    (context, call, service, work),
                    call.name,
                    call.arguments,
                    schemas.get(call.name),
                    set(schemas),
                    attempts,
                )

            async def observe_read(context, query):
                result = await original_read(context, query)
                retrieved.append(
                    {
                        "status": result.status,
                        "matches": [
                            {
                                "document": m.source.document_id,
                                "version": m.source.document_version,
                                "score": round(m.score, 6),
                            }
                            for m in result.matches
                        ],
                    }
                )
                return result

            engine._operate, engine.knowledge.read = observe_operation, observe_read
            last = 0.0
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://heldout"
            ) as client:
                for case in data["cases"]:
                    if case.get("setup"):
                        version = 1 if case["setup"] == "calculator-v1" else 2
                        deadline = "4 PM" if version == 1 else "3 PM"
                        document = DocumentInput(
                            document_id="calculator-policy",
                            version=version,
                            title="Classroom calculator lending",
                            format="text",
                            content=(
                                "Classroom calculators may be borrowed during a session. "
                                f"Return each borrowed calculator by {deadline} on the same day, "
                                "showing a learner card."
                            ),
                        )
                        await KnowledgeService(
                            db,
                            app.state.business_registry.get("training-institute"),
                            app.state.embedder,
                            settings,
                        ).ingest(document)
                    response = await client.post(
                        f"/api/v1/businesses/{case['business']}/sessions", json={}
                    )
                    response.raise_for_status()
                    session = response.json()
                    for index, step in enumerate(case["steps"]):
                        await asyncio.sleep(max(0, args.pace_seconds - (time.monotonic() - last)))
                        attempts.clear()
                        retrieved.clear()
                        message = str(uuid4())
                        started = time.monotonic()
                        response = await client.post(
                            f"/api/v1/businesses/{case['business']}/sessions/{session['sessionId']}/messages",
                            headers={"X-Session-Token": session["sessionToken"]},
                            json={
                                "messageId": message,
                                "content": step["question"],
                                "timestamp": datetime.now(UTC).isoformat(),
                            },
                        )
                        last = time.monotonic()
                        body = response.json()
                        owner = {
                            "business_id": case["business"],
                            "session_id": session["sessionId"],
                        }
                        record = await db.db.sessions.find_one(owner)
                        turn = await db.db.turns.find_one(owner | {"message_id": message})
                        lead = await db.db.leads.find_one(owner)
                        trace = (turn or {}).get("agent_trace", {})
                        checks = score_step(
                            step, body, record["state"]["fields"], lead, case["business"]
                        )
                        checks["no_duplicate_leads"] = await db.db.leads.count_documents(owner) <= 1
                        checks["requested_operation_family"] = step["route"] not in {
                            "knowledge",
                            "mixed",
                        } or any(a["operation"] == "readKnowledge" for a in attempts)
                        result = {
                            "case": case["id"],
                            "step": index + 1,
                            "question": step["question"],
                            "expected": step,
                            "seconds": round(last - started, 3),
                            "httpStatus": response.status_code,
                            "actual": body,
                            "checks": checks,
                            "automaticPass": all(checks.values()),
                            "decisionTrace": {
                                "attempts": list(attempts),
                                "retrieval": list(retrieved),
                                "observedStop": safe_stop(trace),
                            },
                        }
                        report["cases"].append(result)
                        save()
                        print(
                            f"{'PASS' if result['automaticPass'] else 'CHECK'} "
                            f"{case['id']} step {index + 1} ({result['seconds']}s)",
                            flush=True,
                        )
                        # Direct original-query ranking measures retrieval independently
                        # of the model's rewritten query and subsequent answer selection.
                        if step["route"] in {"knowledge", "mixed", "unknown"}:
                            service = KnowledgeService(
                                db,
                                app.state.business_registry.get(case["business"]),
                                app.state.embedder,
                                settings,
                            )
                            ranked = await service.rank_for_calibration(
                                RetrievalQuery(query=step["question"])
                            )
                            top = ranked[: settings.rag_top_k]
                            expected = step["sources"] if step["answerable"] else []
                            report["retrieval"].append(
                                {
                                    "case": case["id"],
                                    "expectedDocuments": expected,
                                    "topKHit": any(m.source.document_id in expected for m in top),
                                    "admittedHit": any(
                                        m.source.document_id in expected
                                        and m.score >= settings.rag_min_score
                                        for m in top
                                    ),
                                    "candidates": [
                                        {
                                            "document": m.source.document_id,
                                            "version": m.source.document_version,
                                            "score": round(m.score, 6),
                                            "admitted": m.score >= settings.rag_min_score,
                                        }
                                        for m in top
                                    ],
                                }
                            )
                            save()
                        if any("DAILY_QUOTA" in code for code in trace.get("failure_codes", [])):
                            report["interruptionReason"] = "provider_daily_quota_exhausted"
                            raise RuntimeError(
                                "Provider daily quota exhausted; remaining cases not run"
                            )
        except Exception as error:
            report["executionError"] = type(error).__name__
            print("Evaluation interrupted:", type(error).__name__, flush=True)
        finally:
            assert db.db.name == database_name and database_name.startswith("ai_heldout_")
            await db.client.drop_database(database_name)
            report["ownedDatabaseRemoved"] = True
            report["finishedAt"] = datetime.now(UTC).isoformat()
            report["completedTurns"] = len(report["cases"])
            report["attemptedTurns"] = len(report["cases"])
            report["completedReplies"] = sum(
                c["checks"]["completed_reply"] for c in report["cases"]
            )
            report["executionComplete"] = (
                not report.get("executionError")
                and report["attemptedTurns"] == report["plannedTurns"]
            )
            report["automaticPasses"] = sum(c["automaticPass"] for c in report["cases"])
            report["retrievalMetrics"] = retrieval_metrics(report["retrieval"])
            if report["cases"]:
                report["medianTurnSeconds"] = statistics.median(
                    c["seconds"] for c in report["cases"]
                )
            save()
    return 0 if not report.get("executionError") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--pace-seconds", type=int, choices=[15, 30, 60], default=15)
    parser.add_argument("--candidate-label", help="Explicit label for changed application code")
    parser.add_argument("--case-ids", nargs="+", help="Run only these case IDs; report subset size")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
