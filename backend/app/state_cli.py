"""Explicit M08 live extraction + real Mongo state qualification in an owned probe database."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from pymongo.errors import PyMongoError

from app.businesses import BusinessRegistry
from app.contracts import ExecutionContext, NormalizedMessage
from app.database import DatabaseStartupError, open_database
from app.gemini import GeminiClient, ProviderFailure
from app.generation import GenerationService
from app.repositories import RecordNotFound, Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings, load_settings
from app.state import StateFailure, StateService, qualification
from app.tools import ToolCall, ToolExecutor, build_tool_registry


async def qualify(settings: Settings) -> dict[str, object]:
    name = "ai_engagement_m08_probe_" + uuid4().hex
    probe = Settings(
        _env_file=None,
        mongodb_uri=f"mongodb://127.0.0.1:27017/{name}",
        business_config_dir=settings.business_config_dir,
    )
    registry = BusinessRegistry.load(settings.business_config_dir)
    business = registry.get("training-institute")
    assert business is not None
    async with open_database(probe) as database, GeminiClient(settings) as provider:
        try:
            await seed(database, settings.business_config_dir)
            repo = Repository(database, business)
            session = await repo.create_session("web", "fictional-m08-probe")
            context = ExecutionContext(
                request_id=uuid4(),
                business_id=business.business_id,
                channel="web",
                sender_id=session.sender_id,
                session_id=session.session_id,
                turn_id="pending",
            )
            service = StateService(repo)
            generation = GenerationService(provider)

            async def turn(text: str, extract: bool = True) -> ExecutionContext:
                message = NormalizedMessage(
                    message_id=str(uuid4()),
                    business_id=business.business_id,
                    channel="web",
                    sender_id=context.sender_id,
                    session_id=context.session_id,
                    content=text,
                    timestamp=datetime.now(UTC),
                )
                saved = await repo.begin_turn(context, message)
                execution = context.model_copy(update={"turn_id": saved.turn_id})
                if extract:
                    proposal = await generation.propose_state(saved.turn_id, saved.input_text)
                    await service.apply(execution, proposal)
                return execution

            first = await turn(
                "My name is Aarav. My phone is 9876543210. "
                "My interest is data science. My budget is 30000."
            )
            initial = await service.view(first)
            if (
                initial.fields.phone != "+919876543210"
                or not qualification(initial).can_create_lead
            ):
                raise StateFailure("INITIAL_QUALIFICATION_FAILED")
            executor = ToolExecutor(database, registry, build_tool_registry())
            search = await executor.execute(
                first,
                ToolCall(
                    call_id="m08-search",
                    name="searchCourses",
                    arguments={"category": "data-science", "budget": initial.fields.budget},
                ),
            )
            if search.result.status != "success":
                raise StateFailure("SEARCH_FAILED")
            await service.record_displayed(first, "m08-search", ("DS101", "DS202"))
            selection = await turn("I want the second one.", extract=False)
            await service.select_reference(selection, "the second one")
            correction = await turn("Change my budget to 20000.")
            corrected = await service.view(correction)
            if corrected.fields.course_id != "DS202" or corrected.fields.budget != 20000:
                raise StateFailure("CORRECTION_FAILED")
            if "budget" not in corrected.selection_conflicts:
                raise StateFailure("CONFLICT_NOT_DETECTED")
            updated_search = await executor.execute(
                correction,
                ToolCall(
                    call_id="m08-corrected-search",
                    name="searchCourses",
                    arguments={"category": "data-science", "budget": corrected.fields.budget},
                ),
            )
            if (
                updated_search.result.status != "success"
                or updated_search.result.data.get("courses") != []
            ):
                raise StateFailure("CORRECTED_SEARCH_FAILED")
            cleared = await turn("Please remove my phone number.")
            final = await service.view(cleared)
            if final.fields.phone is not None or "phone" not in qualification(final).missing:
                raise StateFailure("CLEAR_FAILED")
            other = await repo.create_session("web", "different-fictional-probe")
            foreign_context = context.model_copy(
                update={"session_id": other.session_id, "sender_id": other.sender_id}
            )
            if (await service.view(foreign_context)).fields.name is not None:
                raise StateFailure("ISOLATION_FAILED")
            revision = (await repo.session(cleared)).state_revision
            if (await StateService(repo).resume(cleared)).state_revision != revision:
                raise StateFailure("REPLAY_FAILED")
            return {
                "liveVerified": True,
                "generatedAt": datetime.now(UTC).isoformat(),
                "model": settings.gemini_chat_model,
                "provider": "gemini",
                "initialContactAndInterestQualified": True,
                "selectedSecondCourse": "DS202",
                "correctedBudget": corrected.fields.budget,
                "selectedCoursePreserved": corrected.fields.course_id,
                "selectionConflicts": corrected.selection_conflicts,
                "correctedSearchEmpty": True,
                "phoneExplicitlyCleared": True,
                "independentSessionEmpty": True,
                "checkpointReplayStable": True,
                "stateRevision": revision,
                "applicationDataChanged": False,
                "scope": "Live extraction/state/tools probe; no agent or business writes.",
            }
        finally:
            assert database.db.name == name and name.startswith("ai_engagement_m08_probe_")
            await database.client.drop_database(name)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live only to explicitly run actual provider requests")
    target = REPOSITORY_ROOT / "docs/evidence/M08-live-results.json"
    try:
        async with asyncio.timeout(150):
            report = await qualify(load_settings())
    except (ProviderFailure, StateFailure) as error:
        target.write_text(
            json.dumps({"liveVerified": False, "code": error.code}) + "\n", encoding="utf-8"
        )
        raise SystemExit(f"M08 check not completed: {error.code}. No success claimed.") from None
    except (TimeoutError, PyMongoError, RecordNotFound, DatabaseStartupError):
        target.write_text('{"liveVerified": false, "code": "PROBE_FAILED"}\n', encoding="utf-8")
        raise SystemExit("M08 check not completed: PROBE_FAILED. No success claimed.") from None
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: live M08 qualification written to {target}; owned probe database removed.")


if __name__ == "__main__":
    asyncio.run(main())
