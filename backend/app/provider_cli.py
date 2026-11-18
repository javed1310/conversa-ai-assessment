"""Explicit live Gemini capability qualification. No lead/callback/notification writes."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.businesses import BusinessRegistry
from app.contracts import Contract, CourseDetailsArguments, Identifier, LeadInput
from app.database import DatabaseStartupError, open_database
from app.embeddings import EmbeddingFailure, GeminiEmbedder
from app.gemini import FunctionSpec, GeminiClient, ProviderFailure, ToolOutput, encode
from app.generation import EvidenceItem, GenerationService
from app.repositories import RecordNotFound, Repository
from app.settings import REPOSITORY_ROOT, Settings, load_settings


class ReceiptArguments(Contract):
    receipt_id: Identifier


async def qualify(settings: Settings) -> dict[str, object]:
    async with GeminiClient(settings) as provider:
        service = GenerationService(provider)
        greeting = await provider.start("Reply with a brief greeting.", "Hello", ())
        if not greeting.turn.text.strip() or greeting.turn.calls:
            raise ProviderFailure("GREETING_CHECK_FAILED")
        proposal = await service.propose_state(
            "m07-customer-probe",
            "My name is Aarav. My phone is 9876543210. My budget is 20000.",
        )
        fields = proposal.proposal.set_fields
        if fields.name != "Aarav" or fields.phone != "+919876543210" or fields.budget != 20000:
            raise ProviderFailure("STATE_CHECK_FAILED")
        empty = await service.propose_state("m07-empty-probe", "Hello, what courses do you offer?")
        if empty.proposal.set_fields.model_fields_set or empty.proposal.clear_fields:
            raise ProviderFailure("EMPTY_STATE_CHECK_FAILED")
        registry = BusinessRegistry.load(settings.business_config_dir)
        business = registry.get("training-institute")
        assert business is not None
        tools = (
            FunctionSpec(
                "getCourseDetails",
                "Read course details and receive a receipt",
                CourseDetailsArguments,
            ),
            FunctionSpec(
                "acknowledgeReceipt", "Acknowledge the exact returned receiptId", ReceiptArguments
            ),
        )
        system = (
            "This is a read-only capability probe. First call getCourseDetails for DS202. "
            "After receiving its actual receiptId, call acknowledgeReceipt with that exact ID. "
            "Do not invent a receipt. Then give a brief final reply. Do not repeat completed calls."
        )
        convo = await provider.start(
            system, "Please perform the capability probe.", tools, force_tool=True
        )
        receipt = "probe-" + uuid4().hex
        read_done = False
        acknowledged = False
        traces = []
        async with open_database(settings) as database:
            repo = Repository(database, business)
            for _ in range(settings.agent_max_rounds):
                if not convo.turn.calls:
                    break
                outputs = []
                # Only reads and in-memory acknowledgements; no business writes.
                for call in convo.turn.calls:
                    result: dict[str, object]
                    if call.name == "getCourseDetails" and not read_done:
                        arguments = CourseDetailsArguments.model_validate(call.arguments)
                        if arguments.course_id != "DS202":
                            raise ProviderFailure("TOOL_CHECK_FAILED")
                        course = await repo.course(arguments.course_id)
                        result = {
                            "course": course.model_dump(mode="json", by_alias=True),
                            "receiptId": receipt,
                        }
                        read_done = True
                    elif call.name == "acknowledgeReceipt" and read_done and not acknowledged:
                        args = ReceiptArguments.model_validate(call.arguments)
                        if args.receipt_id != receipt:
                            raise ProviderFailure("DEPENDENCY_CHECK_FAILED")
                        result = {"acknowledged": True}
                        acknowledged = True
                    else:
                        raise ProviderFailure("DEPENDENCY_CHECK_FAILED")
                    traces.append({"name": call.name, "validated": True})
                    outputs.append(ToolOutput(call.call_id, encode(result)))
                convo = await provider.continue_with(
                    convo, tuple(outputs), system=system, tools=tools
                )
        if not read_done or not acknowledged or convo.turn.calls or not convo.turn.text.strip():
            raise ProviderFailure("DEPENDENCY_CHECK_FAILED")
        policy = (
            "A refund requires a request within seven days of payment AND no attendance "
            "at the first class. Both conditions must hold."
        )
        answer = await service.answer(
            "What are both refund conditions?", (EvidenceItem(key="refund-policy", text=policy),)
        )
        if not answer.claims or not any("refund-policy" in c.evidence_keys for c in answer.claims):
            raise ProviderFailure("ANSWER_CHECK_FAILED")
        summary = await service.summarize(
            lead_id="m07-probe-not-a-saved-lead",
            customer=LeadInput(
                name="Aarav", phone="9876543210", budget=20000, stated_interest="Python"
            ),
            history=("Customer: I want to learn Python with a budget of 20000.",),
        )
        embedder = GeminiEmbedder(settings)
        try:
            vectors = await embedder.embed(
                ("Gemini capability check for course policies.",), task="query"
            )
        finally:
            await embedder.close()
        return {
            "liveVerified": True,
            "generatedAt": datetime.now(UTC).isoformat(),
            "provider": "gemini",
            "chatModel": settings.gemini_chat_model,
            "summaryModel": settings.gemini_summary_model,
            "embeddingModel": settings.gemini_embedding_model,
            "embeddingDimensions": len(vectors[0]),
            "greeting": greeting.turn.text,
            "stateFieldsChecked": ["name", "phone", "budget"],
            "emptyStateChecked": True,
            "toolTrace": traces,
            "dependentReceiptChecked": acknowledged,
            "continuationRounds": convo.rounds,
            "answer": answer.model_dump(mode="json", by_alias=True),
            "summary": summary.model_dump(mode="json", by_alias=True),
            "limits": "Read-only/in-memory probe; no saved lead/callback or agent integration.",
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly authorize actual API requests"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live only for explicit capability qualification")
    target = REPOSITORY_ROOT / "docs/evidence/M07-live-results.json"
    try:
        async with asyncio.timeout(180):
            report = await qualify(load_settings())
    except (ProviderFailure, EmbeddingFailure) as error:
        target.write_text(
            encode({"liveVerified": False, "code": error.code}) + "\n", encoding="utf-8"
        )
        raise SystemExit(
            f"Capability check not completed: {error.code}. No success claimed."
        ) from None
    except (TimeoutError, ValidationError, PyMongoError, RecordNotFound, DatabaseStartupError):
        target.write_text(
            encode({"liveVerified": False, "code": "PROBE_FAILED"}) + "\n", encoding="utf-8"
        )
        raise SystemExit(
            "Capability check not completed: PROBE_FAILED. No success claimed."
        ) from None
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: actual Gemini capability report written to {target}.")


if __name__ == "__main__":
    asyncio.run(main())
