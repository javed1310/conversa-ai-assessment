"""Explicit restart probe in a dedicated synthetic database, never customer data."""

import asyncio
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from app.businesses import BusinessRegistry
from app.contracts import ExecutionContext, LeadInput, NormalizedMessage
from app.database import open_database
from app.repositories import Repository
from app.seed import seed
from app.settings import REPOSITORY_ROOT, Settings


async def main() -> None:
    marker = os.environ.get("M03_PROBE_MARKER", "")
    if not marker or not marker.replace("-", "").isalnum() or len(marker) > 80:
        raise SystemExit("Set M03_PROBE_MARKER to a unique alphanumeric marker.")
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in {"write", "read"}:
        raise SystemExit("Usage: python -m app.m03_probe write|read")
    settings = Settings(
        _env_file=None, mongodb_uri="mongodb://127.0.0.1:27017/ai_engagement_m03_probe"
    )
    async with open_database(settings) as database:
        config = BusinessRegistry.load(REPOSITORY_ROOT / "businesses").get("training-institute")
        assert config is not None
        repo = Repository(database, config)
        if mode == "write":
            await seed(database, REPOSITORY_ROOT / "businesses")
            session = await repo.create_session("web", marker)
            ctx = ExecutionContext(
                request_id=uuid4(),
                business_id=session.business_id,
                channel="web",
                sender_id=marker,
                session_id=session.session_id,
                turn_id="pending",
            )
            message = NormalizedMessage(
                message_id=marker,
                business_id=session.business_id,
                channel="web",
                sender_id=marker,
                session_id=session.session_id,
                content="Synthetic restart verification enquiry",
                timestamp=datetime.now(UTC),
            )
            turn = await repo.begin_turn(ctx, message)
            ctx = ctx.model_copy(update={"turn_id": turn.turn_id})
            await repo.create_lead(
                ctx,
                LeadInput(name="Synthetic Restart Probe", phone="9876543210", course_id="DS202"),
            )
        records = await database.db.sessions.find({"sender_id": marker}).to_list()
        assert len(records) == 1, "Probe marker missing or repeated"
        ctx = ExecutionContext(
            request_id=uuid4(),
            business_id="training-institute",
            channel="web",
            sender_id=marker,
            session_id=records[0]["session_id"],
            turn_id="pending",
        )
        assert len(await repo.courses()) == 4
        assert len(await repo.history(ctx)) == 1
        lead = await repo.lead_for_session(ctx)
        assert lead.customer.course_id == "DS202" and lead.automation.status == "pending"
        assert lead.automation_input[0].content == "Synthetic restart verification enquiry"
        print(
            f"PASS ({mode}): courses, owning session/turn, "
            "qualified lead and pending event persisted."
        )


if __name__ == "__main__":
    asyncio.run(main())
