"""Explicit, validated fixture seeding. Existing records are preserved on repeat runs."""

import asyncio
import json
from pathlib import Path

from pydantic import TypeAdapter
from pymongo.errors import DuplicateKeyError

from app.businesses import BusinessRegistry
from app.database import Database, open_database
from app.persistence_models import Course, Membership
from app.repositories import stored
from app.settings import load_settings


async def seed(database: Database, root: Path) -> dict[str, int]:
    registry = BusinessRegistry.load(root)
    courses: list[Course] = TypeAdapter(list[Course]).validate_python(
        json.loads((root / "training-institute/data/courses.json").read_text(encoding="utf-8"))
    )
    memberships: list[Membership] = TypeAdapter(list[Membership]).validate_python(
        json.loads((root / "fitness-studio/data/memberships.json").read_text(encoding="utf-8"))
    )
    # Validate the entire batch before making any writes. Unknown/cross-owned fixtures fail.
    if len({item.course_id for item in courses}) != len(courses) or len(
        {item.membership_id for item in memberships}
    ) != len(memberships):
        raise ValueError("Duplicate seed identity.")
    all_records: list[Course | Membership] = [*courses, *memberships]
    for item in all_records:
        owner = "training-institute" if isinstance(item, Course) else "fitness-studio"
        if item.business_id != owner or registry.get(owner) is None:
            raise ValueError("Seed record has an invalid business owner.")
    batches: tuple[tuple[str, list[Course | Membership], str], ...] = (
        ("courses", [*courses], "course_id"),
        ("memberships", [*memberships], "membership_id"),
    )
    for collection, records, id_field in batches:
        for record in records:
            document = stored(record)
            key = {"business_id": record.business_id, id_field: document[id_field]}
            try:
                await database.db[collection].update_one(
                    key, {"$setOnInsert": document}, upsert=True
                )
            except DuplicateKeyError:
                # A racing upsert is safe only when the intended identity now exists.
                if await database.db[collection].find_one(key) is None:
                    raise
    return {
        "courses": await database.db.courses.count_documents({"business_id": "training-institute"}),
        "memberships": await database.db.memberships.count_documents(
            {"business_id": "fitness-studio"}
        ),
    }


async def main() -> None:
    settings = load_settings()
    async with open_database(settings) as database:
        counts = await seed(database, settings.business_config_dir)
        print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
