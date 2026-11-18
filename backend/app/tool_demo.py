"""Run real read tools and export observed evidence in an isolated synthetic demo DB."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from app.businesses import BusinessRegistry
from app.contracts import ExecutionContext, NormalizedMessage, ToolFailure, ToolSuccess
from app.database import open_database
from app.repositories import Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings
from app.tools import CourseSearchResult, ToolCall, ToolExecutor, build_tool_registry


async def main() -> None:
    settings = Settings(
        _env_file=None, mongodb_uri="mongodb://127.0.0.1:27017/ai_engagement_m04_demo"
    )
    businesses = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    evidence = []
    async with open_database(settings) as database:
        await seed(database, REPOSITORY_ROOT / "businesses")
        executor = ToolExecutor(database, businesses, build_tool_registry())
        contexts = {}
        for business in businesses.configurations():
            repo = Repository(database, business)
            session = await repo.create_session("web", "synthetic-demo")
            context = ExecutionContext(
                request_id=uuid4(),
                business_id=business.business_id,
                channel="web",
                sender_id=session.sender_id,
                session_id=session.session_id,
                turn_id="pending",
            )
            turn = await repo.begin_turn(
                context,
                NormalizedMessage(
                    message_id=str(uuid4()),
                    business_id=business.business_id,
                    channel="web",
                    sender_id=session.sender_id,
                    session_id=session.session_id,
                    content="Synthetic read-tool demonstration",
                    timestamp=datetime.now(UTC),
                ),
            )
            contexts[business.business_id] = context.model_copy(update={"turn_id": turn.turn_id})
        for budget, expected in ((30000, ["DS101", "DS202"]), (20000, [])):
            trace = await executor.execute(
                contexts["training-institute"],
                ToolCall(
                    call_id=str(uuid4()),
                    name="searchCourses",
                    arguments={"category": "data-science", "budget": budget},
                ),
            )
            assert isinstance(trace.result, ToolSuccess)
            actual = [
                item.course_id
                for item in CourseSearchResult.model_validate(trace.result.data).courses
            ]
            assert actual == expected
            evidence.append(
                {
                    "case": f"data-science budget {budget}",
                    "expected": expected,
                    "actual": actual,
                    "trace": trace.model_dump(mode="json", by_alias=True),
                }
            )
        for business_id, tool, arguments, expected_status in (
            ("training-institute", "getCourseDetails", {"courseID": "DS202"}, "success"),
            ("training-institute", "getCourseDetails", {"courseID": "PY202"}, "success"),
            ("fitness-studio", "getMembershipDetails", {}, "success"),
            ("fitness-studio", "getCourseDetails", {"courseID": "DS202"}, "NOT_ALLOWED"),
        ):
            trace = await executor.execute(
                contexts[business_id],
                ToolCall(call_id=str(uuid4()), name=tool, arguments=arguments),
            )
            actual_status = (
                trace.result.code if isinstance(trace.result, ToolFailure) else trace.result.status
            )
            assert actual_status == expected_status
            evidence.append(
                {
                    "case": f"{business_id} / {tool}",
                    "expected": expected_status,
                    "actual": actual_status,
                    "trace": trace.model_dump(mode="json", by_alias=True),
                }
            )
    target = REPOSITORY_ROOT / "docs/evidence/M04-tool-results.json"
    target.write_text(
        json.dumps(
            {
                "generatedAt": datetime.now(UTC).isoformat(),
                "database": "ai_engagement_m04_demo",
                "cases": evidence,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"PASS: {len(evidence)} real tool demonstrations; evidence: {target}")


if __name__ == "__main__":
    asyncio.run(main())
