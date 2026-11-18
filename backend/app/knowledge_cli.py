"""Register offline metadata or explicitly run real ingestion/live retrieval qualification."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

from app.businesses import BusinessRegistry
from app.database import Database, open_database
from app.embeddings import EmbeddingFailure, GeminiEmbedder
from app.knowledge import DocumentInput, KnowledgeFailure, KnowledgeService, RetrievalQuery
from app.settings import REPOSITORY_ROOT, Settings, load_settings

FIXTURES = (
    ("training-institute", "course-overview", "Course overview", "course-overview.md"),
    ("training-institute", "eligibility", "Course eligibility", "eligibility.md"),
    ("training-institute", "refund", "Refund policy", "refund.md"),
    ("training-institute", "faq", "Frequently asked questions", "faq.md"),
    ("fitness-studio", "fitness-policy", "Fitness studio policy", "policy.md"),
)


def fixture_inputs(settings: Settings, version: int = 2) -> tuple[tuple[str, DocumentInput], ...]:
    return tuple(
        (
            business,
            DocumentInput(
                document_id=identifier,
                version=version,
                title=title,
                format="markdown",
                content=(
                    settings.business_config_dir / business / "knowledge" / filename
                ).read_text(encoding="utf-8"),
            ),
        )
        for business, identifier, title, filename in FIXTURES
    )


async def load_fixtures(
    database: Database,
    provider: GeminiEmbedder,
    settings: Settings,
    register_only: bool = False,
    version: int = 2,
) -> list[dict[str, object]]:
    registry = BusinessRegistry.load(settings.business_config_dir)
    results = []
    for business_id, document in fixture_inputs(settings, version):
        business = registry.get(business_id)
        assert business is not None
        service = KnowledgeService(database, business, provider, settings)
        saved = (
            await service.register(document) if register_only else await service.ingest(document)
        )
        results.append(
            {
                "businessId": business_id,
                "documentId": saved.document_id,
                "version": saved.version,
                "status": saved.status,
                "expectedChunks": saved.expected_chunks,
                "contentHash": saved.content_hash,
                "embeddingModel": saved.embedding_model,
                "dimensions": saved.dimensions,
                "embeddingProfile": saved.embedding_profile,
            }
        )
    return results


async def qualify(
    provider: GeminiEmbedder, settings: Settings, version: int = 2
) -> dict[str, object]:
    # Keep the new-document experiment separate from the application's seed knowledge.
    # Explicit Settings construction retains SecretStr validation for the isolated URI.
    probe = Settings(
        _env_file=None,
        mongodb_uri="mongodb://127.0.0.1:27017/ai_engagement_m06_live_probe",
        business_config_dir=settings.business_config_dir,
        gemini_embedding_model=settings.gemini_embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        chunk_tokens=settings.chunk_tokens,
        chunk_overlap=settings.chunk_overlap,
        rag_top_k=settings.rag_top_k,
        rag_max_chunks=settings.rag_max_chunks,
    )
    registry = BusinessRegistry.load(settings.business_config_dir)
    cases = (
        (
            "training-institute",
            "Does Advanced Python require basic programming knowledge?",
            "eligibility",
        ),
        ("training-institute", "Can a complete beginner take Python Basics?", "eligibility"),
        ("training-institute", "Can I receive a refund after attending the first class?", "refund"),
        (
            "fitness-studio",
            "What should I bring for an introductory trial visit?",
            "fitness-policy",
        ),
    )
    unknown = (
        "What is the baggage allowance on a flight to Japan?",
        "How do I replace a car fuel injector?",
    )
    async with open_database(probe) as database:
        documents = await load_fixtures(database, provider, probe, version=version)
        observations = []
        positives, negatives = [], []
        for business_id, query, expected in cases:
            business = registry.get(business_id)
            assert business is not None
            ranked = await KnowledgeService(
                database, business, provider, probe
            ).rank_for_calibration(RetrievalQuery(query=query))
            relevant = [item.score for item in ranked if item.source.document_id == expected]
            if not relevant:
                raise KnowledgeFailure("CALIBRATION_FAILED")
            positives.append(max(relevant))
            observations.append(
                {
                    "businessId": business_id,
                    "question": query,
                    "expected": expected,
                    "ranked": [item.model_dump(mode="json", by_alias=True) for item in ranked],
                }
            )
        for business in registry.configurations():
            for query in unknown:
                ranked = await KnowledgeService(
                    database, business, provider, probe
                ).rank_for_calibration(RetrievalQuery(query=query))
                negatives.append(max((item.score for item in ranked), default=-1.0))
        if min(positives) <= max(negatives):
            raise KnowledgeFailure("CALIBRATION_NEEDS_REVIEW")
        cutoff = (min(positives) + max(negatives)) / 2
        calibrated = probe.model_copy(update={"rag_min_score": cutoff})
        checks = []
        for business_id, question, expected in (
            ("training-institute", "What are both conditions for a refund?", "refund"),
            ("training-institute", "I have never coded. Can I join Python Basics?", "eligibility"),
            ("fitness-studio", "Can I try the fitness studio before membership?", "fitness-policy"),
        ):
            business = registry.get(business_id)
            assert business is not None
            result = await KnowledgeService(database, business, provider, calibrated).retrieve(
                RetrievalQuery(query=question)
            )
            actual = [item.source.document_id for item in result.matches]
            checks.append(
                {"question": question, "expectedDocument": expected, "actualDocuments": actual}
            )
            if expected not in actual:
                raise KnowledgeFailure("HOLDOUT_FAILED")
        for business in registry.configurations():
            result = await KnowledgeService(database, business, provider, calibrated).retrieve(
                RetrievalQuery(query="Which spacecraft is orbiting Neptune today?")
            )
            if result.matches:
                raise KnowledgeFailure("UNKNOWN_HOLDOUT_FAILED")
        institute = registry.get("training-institute")
        fitness = registry.get("fitness-studio")
        assert institute and fitness
        added = DocumentInput(
            version=version,
            document_id="added-library-locker",
            title="Added fictional library locker policy",
            format="text",
            content=(
                "The assessment institute's library locker service uses the phrase copper lantern "
                "orchard. Library locker collection is available on Saturdays by prior arrangement."
            ),
        )
        service = KnowledgeService(database, institute, provider, calibrated)
        await service.ingest(added)
        added_query = RetrievalQuery(
            query="When can I collect my copper lantern orchard library locker?"
        )
        added_result = await service.retrieve(added_query)
        foreign = await KnowledgeService(database, fitness, provider, calibrated).retrieve(
            added_query
        )
        if not any(item.source.document_id == added.document_id for item in added_result.matches):
            raise KnowledgeFailure("NEW_DOCUMENT_FAILED")
        if any(item.source.business_id != fitness.business_id for item in foreign.matches):
            raise KnowledgeFailure("ISOLATION_FAILED")
        return {
            "liveVerified": True,
            "provider": "gemini",
            "embeddingProfile": provider.profile,
            "model": provider.model,
            "dimensions": provider.dimensions,
            "recommendedRagMinScore": cutoff,
            "documents": documents,
            "calibration": observations,
            "holdout": checks,
            "addedDocument": added_result.model_dump(mode="json", by_alias=True),
            "foreignBusinessResult": foreign.model_dump(mode="json", by_alias=True),
            "generatedAt": datetime.now(UTC).isoformat(),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("register", "ingest", "qualify"))
    parser.add_argument("--version", type=int, default=2)
    args = parser.parse_args()
    settings = load_settings()
    provider = GeminiEmbedder(settings)
    try:
        if args.mode == "qualify":
            report = await qualify(provider, settings, args.version)
            target = REPOSITORY_ROOT / "docs/results/knowledge-qualification.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"PASS: live qualification written to {target}; review recommendedRagMinScore.")
        else:
            async with open_database(settings) as database:
                result = await load_fixtures(
                    database, provider, settings, args.mode == "register", version=args.version
                )
                report = {
                    "mode": args.mode,
                    "documents": result,
                    "liveCallsMadeByThisCommand": args.mode != "register",
                }
                if args.mode == "register":
                    target = REPOSITORY_ROOT / "docs/results/knowledge-registration.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report, indent=2))
    except (EmbeddingFailure, KnowledgeFailure) as error:
        raise SystemExit(
            f"Knowledge check not completed: {error.code}. No live success claimed."
        ) from None
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
