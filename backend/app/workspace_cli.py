"""Prepare only an owned M13 browser database with fictional seeds and qualified real vectors."""

import argparse
import asyncio
import re

from pydantic import SecretStr

from app.database import open_database
from app.seed import seed
from app.settings import load_settings


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"ai_engagement_m13_browser_[0-9]{10,20}", args.database):
        parser.error("Only an owned M13 browser database may be prepared")
    settings = load_settings()
    probe = settings.model_copy(
        update={"mongodb_uri": SecretStr(f"mongodb://127.0.0.1:27017/{args.database}")}
    )
    async with open_database(settings) as application:
        profile = {
            "embedding_model": settings.gemini_embedding_model,
            "embedding_profile": "gemini-retrieval-document-query-l2-v1",
        }
        documents = await application.db.documents.find(
            profile | {"status": "ready"}, {"_id": 0}
        ).to_list(None)
        chunks = await application.db.chunks.find(profile, {"_id": 0}).to_list(None)
    if len(documents) < 5 or len(chunks) < 5:
        raise SystemExit("Ready qualified knowledge required")
    async with open_database(probe) as database:
        if await database.db.sessions.count_documents(
            {}
        ) or await database.db.documents.count_documents({}):
            raise SystemExit("Browser preparation requires an empty owned database")
        await seed(database, settings.business_config_dir)
        await database.db.documents.insert_many(documents)
        await database.db.chunks.insert_many(chunks)
    print("PASS: owned M13 browser fixtures prepared; original application data unchanged.")


if __name__ == "__main__":
    asyncio.run(main())
