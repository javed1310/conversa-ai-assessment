"""Fresh ten-scenario assessment audit using newly embedded fictional repo fixtures only."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.database import open_database
from app.generation import GenerationService
from app.knowledge import KnowledgeReader, KnowledgeService
from app.knowledge_cli import fixture_inputs
from app.main import create_app
from app.reliability_cli import qualify
from app.settings import load_settings
from pydantic import SecretStr


async def main(output="REASSESSMENT-live.json", pace_seconds=30):
    name = "ai_audit_source_" + uuid4().hex
    settings = load_settings().model_copy(
        update={
            "mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{name}"),
            "ai_chat_enabled": True,
            "automation_enabled": False,
        }
    )
    report = {
        "passed": False,
        "generatedAtUtc": datetime.now(UTC).isoformat(),
        "scope": "Actual Gemini; newly embedded fictional repository assessment fixtures; no existing application database read/copy",
        "applicationDataChanged": False,
        "paceSeconds": pace_seconds,
    }
    async with open_database(settings) as source:
        try:
            # The legacy qualifier reads its supplied settings database for ready
            # fixtures. Here that database is OUR newly created synthetic staging
            # database, never the real app's database. Its probe also cleans itself.
            app = create_app(settings)
            fixtures = []
            async with app.router.lifespan_context(app):
                for business_id, document in fixture_inputs(settings):
                    service = KnowledgeService(
                        source,
                        app.state.business_registry.get(business_id),
                        app.state.embedder,
                        settings,
                    )
                    ready = await service.ingest(document)
                    assert ready.status == "ready"
                    fixtures.append(
                        {
                            "business": business_id,
                            "document": document.document_id,
                            "version": ready.version,
                            "ready": True,
                        }
                    )
                    print(
                        "Embedded new fictional fixture:",
                        document.document_id,
                        flush=True,
                    )
            report["fixtures"] = fixtures
            selections = []
            retrievals = []
            original_select = GenerationService.select_reply
            original_read = KnowledgeReader.read

            async def trace_selection(service, data):
                result = await original_select(service, data)
                selections.append(
                    {
                        "question": data.get("currentCustomerText"),
                        "selection": result.model_dump(mode="json", by_alias=True),
                        "evidence": data.get("evidence"),
                    }
                )
                return result

            async def trace_retrieval(reader, context, query):
                result = await original_read(reader, context, query)
                retrievals.append(
                    {
                        "query": query.query,
                        "status": result.status,
                        "documents": [m.source.document_id for m in result.matches],
                    }
                )
                return result

            GenerationService.select_reply = trace_selection
            KnowledgeReader.read = trace_retrieval
            try:
                raw = await qualify(settings, pace_seconds)
            finally:
                GenerationService.select_reply = original_select
                KnowledgeReader.read = original_read
                report["selectionDiagnostics"] = selections
                report["retrievalDiagnostics"] = retrievals
            report.update(
                {
                    key: raw.get(key)
                    for key in (
                        "passed",
                        "provider",
                        "chatModel",
                        "summaryModel",
                        "embeddingModel",
                        "embeddingDimensions",
                        "ownedDatabaseRemoved",
                        "realExternalNotificationSent",
                    )
                }
            )
            report["cases"] = {
                identifier: {
                    "expected": case["expected"],
                    "status": case["status"],
                    "failureCode": case.get("failureCode"),
                    "requests": [
                        {
                            key: item[key]
                            for key in (
                                "httpStatus",
                                "durationSeconds",
                                "errorCode",
                                "agentTrace",
                                "response",
                                "measurement",
                            )
                            if key in item
                        }
                        for item in case["requests"]
                    ],
                    **{
                        key: case[key]
                        for key in ("counts", "summary", "savedBudget")
                        if key in case
                    },
                }
                for identifier, case in raw["cases"].items()
            }
        except Exception as error:
            report.update(passed=False, errorType=type(error).__name__)
            raise
        finally:
            assert source.db.name == name and name.startswith("ai_audit_source_")
            await source.client.drop_database(name)
            report["ownedStagingDatabaseRemoved"] = True
            report["finishedAtUtc"] = datetime.now(UTC).isoformat()
            (ROOT / "docs/evidence" / output).write_text(
                json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
            )
    assert report["passed"], (
        "Some assessment scenarios failed; inspect safe synthetic report"
    )
    print("PASS: all ten fresh assessment scenarios", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        choices=[
            "REASSESSMENT-live.json",
            "REMEDIATION-live.json",
            "SUBMISSION_FINAL-live.json",
            "SECTIONS_FIX-ten-final-live.json",
        ],
        default="REASSESSMENT-live.json",
    )
    parser.add_argument("--pace-seconds", type=int, choices=[15, 30, 60], default=30)
    args = parser.parse_args()
    asyncio.run(main(args.output, args.pace_seconds))
