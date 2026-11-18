"""Shared source/selection validation used by every business response strategy."""

import re

from app.agent_context import TurnWork
from app.facts import contains_action_status_claim, verified_policy_quote
from app.gemini import ProviderFailure
from app.generation import ReplySelection


def selected_passages(selection: ReplySelection, work: TurnWork) -> dict[str, list[str]]:
    available = {item.key: item for item in work.evidence}
    if (
        len(set(selection.evidence_keys)) != len(selection.evidence_keys)
        or not set(selection.evidence_keys) <= available.keys()
    ):
        raise ProviderFailure("INVALID_REFERENCE")
    passages: dict[str, list[str]] = {}
    for passage in selection.policy_passages:
        item = available.get(passage.evidence_key)
        if item is None or item.source is None or item.key not in selection.evidence_keys:
            raise ProviderFailure("INVALID_REFERENCE")
        quote = verified_policy_quote(passage.quote, item.text)
        if quote is None:
            raise ProviderFailure("INVALID_REFERENCE")
        if contains_action_status_claim(quote):
            work.failures.append("UNTRUSTED_ACTION_CLAIM")
            continue
        passages.setdefault(item.key, []).append(quote)
    return passages


def include_beginner_alternative(
    passages: dict[str, list[str]], work: TurnWork, question: str
) -> bool:
    """Keep a documented no-prerequisite option in an explicit beginner recommendation.

    Only already-selected, retrieved sources are eligible. Never infer an option
    from a catalogue name, an unrelated document or a hardcoded course record.
    """
    if not (
        re.search(
            r"\b(?:never|not|no|without)\b.{0,35}"
            r"\b(?:programmed|programming|coding|code|experience)\b",
            question,
            re.I,
        )
        and re.search(
            r"\b(?:start|starting|begin|beginner|sensible|suitable|should|do)\b", question, re.I
        )
    ):
        return False
    preserved = False
    for item in work.evidence:
        if item.source is None or item.key not in passages:
            continue
        for match in re.finditer(
            r"(?m)^[ \t]*([^#\n.!?]+ has no (?:programming )?prerequisites\.)", item.text
        ):
            quote = verified_policy_quote(match[1], item.text)
            if quote is None or contains_action_status_claim(quote):
                continue
            if not any(quote in selected for selected in passages[item.key]):
                passages[item.key].append(quote)
            preserved = True
    return preserved
