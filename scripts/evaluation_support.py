"""Evaluation-only scoring and value-free decision observations; never imported by the app."""

import re
from collections.abc import Awaitable, Callable
from typing import Any

INTERNAL_TOOLS = {"readKnowledge", "resolveCourseReference"}
SAFE_FAILURES = {
    "NOT_ALLOWED",
    "INVALID_ARGUMENTS",
    "QUALIFICATION_REQUIRED",
    "ALREADY_OBSERVED",
    "USE_VALIDATED_PREFERENCES",
    "NOT_FOUND",
    "UNAVAILABLE",
    "OUTCOME_UNKNOWN",
    "IN_PROGRESS",
}
STOP_REASONS = {
    "greeting": "greeting_returned",
    "clarify": "clarification_returned",
    "evidence": "checked_response_returned",
    "unavailable": "no_supported_answer_returned",
    "provider_failure": "provider_failure_returned",
    "state_rejected": "state_validation_rejected",
    "social": "social_reply_returned",
}


def safe_operation(
    name: str, arguments: dict[str, Any], schema: Any, allowed: set[str], result: Any
) -> dict[str, Any]:
    """Record field names and validation only; never argument/result values or IDs."""
    if name not in allowed or schema is None:
        return {
            "operation": "unregistered_operation",
            "argumentFields": [],
            "schemaValid": False,
            "result": "not_registered",
        }
    fields = set(schema.model_fields)
    for field in schema.model_fields.values():
        if isinstance(field.alias, str):
            fields.add(field.alias)
        choices = getattr(field.validation_alias, "choices", [])
        fields.update(value for value in choices if isinstance(value, str))
    try:
        schema.model_validate(arguments)
        valid = True
    except Exception:
        valid = False
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, str) or status not in {"success", "failure", "found", "no_evidence"}:
        status = "returned"
    code = result.get("code") if isinstance(result, dict) else None
    return {
        "operation": name,
        "argumentFields": sorted(
            key if key in fields else "unrecognized_field" for key in arguments
        ),
        "schemaValid": valid,
        "result": status,
        "failure": code
        if isinstance(code, str) and code in SAFE_FAILURES
        else ("other_failure" if code else None),
    }


def safe_stop(trace: dict[str, Any]) -> str:
    """Observed stop category, not an explanation of hidden model reasoning."""
    return STOP_REASONS.get(trace.get("outcome"), "no_completed_decision_trace")


async def observe_call(
    function: Callable[..., Awaitable[Any]],
    call_args: tuple[Any, ...],
    name: str,
    arguments: dict[str, Any],
    schema: Any,
    registered: set[str],
    events: list[dict[str, Any]],
) -> Any:
    """Forward the actual result/exception unchanged; tracing must not alter execution."""
    result: Any = {"status": "failure"}
    try:
        result = await function(*call_args)
        return result
    finally:
        try:
            events.append(safe_operation(name, arguments, schema, registered, result))
        except Exception:
            events.append({"traceCapture": "unavailable"})


def score_step(
    step: dict[str, Any],
    body: dict[str, Any],
    state: dict[str, Any],
    lead: dict[str, Any] | None,
    business: str,
) -> dict[str, bool]:
    reply = body.get("reply", "")
    sources = body.get("sources", [])
    results = [a.get("result", {}) for a in body.get("actions", [])]
    successful = [r for r in results if r.get("status") == "success"]
    tools = {r.get("tool") for r in successful}
    checks = {
        "completed_reply": bool(reply) and body.get("status") == "completed",
        "source_business_isolation": all(s.get("businessId") == business for s in sources),
    }
    unavailable = bool(
        re.search(
            r"don't have|couldn't complete|could not complete|unavailable|"
            r"not (?:established|specified|documented)|does not establish|"
            r"isn't (?:established|specified)|can't confirm",
            reply,
            re.I,
        )
    )
    if step["answerable"]:
        checks["no_false_refusal"] = not unavailable
    else:
        checks["honest_refusal"] = unavailable
    allowed_sources = set(step.get("sources", []))
    if step["route"] in {"knowledge", "mixed"}:
        checks["required_source"] = bool(
            allowed_sources.intersection(s.get("documentId") for s in sources)
        )
    checks["no_unexpected_citation"] = all(s.get("documentId") in allowed_sources for s in sources)
    if "sourceVersion" in step:
        checks["current_document_version"] = all(
            s.get("documentVersion") == step["sourceVersion"] for s in sources
        )
    checks["required_answer_facts"] = all(
        re.search(pattern, reply, re.I) is not None for pattern in step.get("patterns", [])
    )
    checks["no_forbidden_answer"] = not any(
        re.search(pattern, reply, re.I) for pattern in step.get("forbiddenPatterns", [])
    )
    checks["required_tools"] = set(step.get("requiredTools", [])) <= tools
    checks["no_forbidden_execution"] = not tools.intersection(step.get("forbiddenTools", []))
    for key in ("budget", "schedule"):
        if key in step:
            checks["stored_" + key] = state.get(key) == step[key]
    if "courseIds" in step:
        searches = [r for r in successful if r.get("tool") == "searchCourses"]
        checks["matching_search_records"] = bool(searches) and {
            c["courseId"] for c in searches[-1]["data"]["courses"]
        } == set(step["courseIds"])
    if "lead" in step:
        checks["lead_state"] = (lead is not None) == step["lead"]
    if "callback" in step:
        checks["callback_state"] = (
            bool(lead and lead.get("callback_request_id")) == step["callback"]
        )
    if step.get("noWrite"):
        checks["no_unrequested_write"] = not tools.intersection({"createLead", "requestCallback"})
    return checks


def retrieval_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    policies = [r for r in observations if r["expectedDocuments"]]
    return {
        "queries": len(observations),
        "answerablePolicyQueries": len(policies),
        "topKRecall": sum(r["topKHit"] for r in policies) / len(policies) if policies else None,
        "thresholdAdmittedRecall": sum(r["admittedHit"] for r in policies) / len(policies)
        if policies
        else None,
        "note": (
            "Retrieval coverage only; citation, answer correctness and refusals scored separately."
        ),
    }
