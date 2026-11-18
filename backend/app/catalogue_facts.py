"""Literal facts in unambiguous catalogue requests need no model inference."""

import re

from pydantic import ValidationError

from app.budgets import normalize_budget
from app.contracts import CustomerChanges, CustomerEvidence, CustomerField, StateProposal
from app.generation import EvidencedProposal

_REQUEST = re.compile(
    r"\s*(?:i\s+(?:want|need)|i(?:'m| am)\s+looking\s+for|"
    r"(?:please\s+)?(?:find|show|suggest|recommend)(?:\s+me)?)\s+"
    r"(?P<interest>data[\s-]+science|python)\s+courses?\s+"
    r"(?:with\s+(?:a\s+)?(?:(?:maximum|max)\s+)?budget\s+(?:of\s+|is\s+)?|"
    r"(?:under|within|up\s+to)\s+)(?P<amount>.+?)\s*",
    re.I,
)
_SCHEDULE = re.compile(
    r"\s+(?:on\s+|for\s+)?(?P<schedule>weekday|weekend)s?"
    r"(?:\s+(?:batches|classes))?\s*$",
    re.I,
)


def literal_catalogue_proposal(turn_id: str, text: str) -> EvidencedProposal | None:
    # Full-message matching deliberately excludes corrections, mixed questions,
    # quoted instructions, contact collection, writes and unsupported currencies.
    request = _REQUEST.fullmatch(text.strip().removesuffix(".").removesuffix("?"))
    if request is None:
        return None
    amount = request["amount"].strip()
    schedule = _SCHEDULE.search(amount)
    if schedule is not None:
        amount = amount[: schedule.start()].strip()
    try:
        values: dict[str, object] = {
            "budget": normalize_budget(amount),
            "stated_interest": request["interest"],
        }
        evidence: dict[CustomerField, CustomerEvidence] = {
            "budget": CustomerEvidence(turn_id=turn_id, excerpt=amount),
            "stated_interest": CustomerEvidence(turn_id=turn_id, excerpt=request["interest"]),
        }
        if schedule is not None:
            values["schedule"] = schedule["schedule"].lower()
            evidence["schedule"] = CustomerEvidence(turn_id=turn_id, excerpt=schedule[0].strip())
        return EvidencedProposal(
            proposal=StateProposal(set_fields=CustomerChanges.model_validate(values)),
            evidence=evidence,
        )
    except (ValueError, ValidationError):
        return None
