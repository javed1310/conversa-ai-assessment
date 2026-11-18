"""Export reproducible contracts for documentation and later frontend type generation."""

import argparse
import json

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from app import contracts
from app.channels import Delivery, MockMessage, MockReply, SessionStart, SessionStarted, WebMessage
from app.generation import (
    AnswerDraft,
    ChoiceReview,
    ConversationalDraft,
    CourseQuestionSelection,
    DiscussedChoice,
    EvidencedProposal,
    EvidenceItem,
    PolicySelection,
    ReplyReview,
    ReplySelection,
    StateDraft,
    SummaryDraft,
    SummaryPlan,
)
from app.knowledge import (
    DocumentInput,
    DocumentRecord,
    RetrievalMatch,
    RetrievalQuery,
    RetrievalResult,
)
from app.main import create_app
from app.persistence_models import Course, LeadRecord, Membership, Turn
from app.settings import REPOSITORY_ROOT
from app.state import Qualification
from app.tools import (
    CallbackReceipt,
    CourseDetailsResult,
    CourseSearchResult,
    LeadReceipt,
    MembershipArguments,
    MembershipResult,
    ToolCall,
)
from app.typescript import export_types


def artifacts() -> dict[str, str]:
    models: list[type[BaseModel]] = [
        StateDraft,
        EvidencedProposal,
        EvidenceItem,
        AnswerDraft,
        ConversationalDraft,
        ChoiceReview,
        DiscussedChoice,
        CourseQuestionSelection,
        ReplyReview,
        SummaryDraft,
        SummaryPlan,
        ReplySelection,
        PolicySelection,
        Qualification,
        DocumentInput,
        DocumentRecord,
        RetrievalQuery,
        RetrievalMatch,
        RetrievalResult,
        SessionStart,
        SessionStarted,
        WebMessage,
        MockMessage,
        MockReply,
        Delivery,
        ToolCall,
        MembershipArguments,
        CourseSearchResult,
        CourseDetailsResult,
        MembershipResult,
        contracts.ActionResult,
        LeadReceipt,
        CallbackReceipt,
        Course,
        Membership,
        Turn,
        LeadRecord,
        contracts.NormalizedMessage,
        contracts.ExecutionContext,
        contracts.SearchCoursesArguments,
        contracts.CourseDetailsArguments,
        contracts.LeadInput,
        contracts.CallbackArguments,
        contracts.CustomerState,
        contracts.StateProposal,
        contracts.Session,
        contracts.LeadSnapshot,
        contracts.Summary,
        contracts.ChatResult,
        contracts.ProcessingResult,
        contracts.ErrorEnvelope,
    ]
    references, definitions = models_json_schema(
        [(model, "validation") for model in models],
        by_alias=True,
        title="Assessment domain contracts",
    )
    domain = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **definitions,
        "anyOf": list(references.values()),
    }
    # Factory construction generates metadata only: no lifespan, database or model calls.
    openapi = create_app().openapi()
    return {
        "domain.schema.json": json.dumps(domain, indent=2, sort_keys=True) + "\n",
        "openapi.json": json.dumps(openapi, indent=2, sort_keys=True) + "\n",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed exports are stale")
    args = parser.parse_args()
    folder = REPOSITORY_ROOT / "docs/contracts"
    for name, content in artifacts().items():
        target = folder / name
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Stale contract export: {name}")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    types = REPOSITORY_ROOT / "frontend/lib/contracts.ts"
    content = export_types(create_app().openapi())
    if args.check:
        if not types.exists() or types.read_text(encoding="utf-8") != content:
            raise SystemExit("Stale frontend OpenAPI types")
    else:
        types.parent.mkdir(parents=True, exist_ok=True)
        types.write_text(content, encoding="utf-8")
    supported = {
        "$ref",
        "anyOf",
        "oneOf",
        "const",
        "enum",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "title",
        "description",
        "default",
        "discriminator",
    }

    def check_schema(node: object) -> None:
        if not isinstance(node, dict):
            return
        if set(node) - supported:
            raise SystemExit("Frontend validator needs support for new schema keywords")
        if node.get("format") not in {None, "uuid", "date-time"}:
            raise SystemExit("Frontend validator needs support for a new format")
        for child in node.get("properties", {}).values():
            check_schema(child)
        for key in ("items", "additionalProperties"):
            check_schema(node.get(key))
        for key in ("anyOf", "oneOf"):
            for child in node.get(key, []):
                check_schema(child)

    for schema in create_app().openapi()["components"]["schemas"].values():
        check_schema(schema)
    schemas = REPOSITORY_ROOT / "frontend/lib/response-schemas.ts"
    content = "// Generated from backend OpenAPI. Do not edit.\n"
    content += (
        "export const responseSchemas = "
        + json.dumps(
            create_app().openapi()["components"]["schemas"],
            sort_keys=True,
            separators=(",", ":"),
        )
        + " as const;\n"
    )
    if args.check:
        if not schemas.exists() or schemas.read_text(encoding="utf-8") != content:
            raise SystemExit("Stale frontend response schemas")
    else:
        schemas.write_text(content, encoding="utf-8")
    print("Contract exports match." if args.check else "Contract exports written.")


if __name__ == "__main__":
    main()
