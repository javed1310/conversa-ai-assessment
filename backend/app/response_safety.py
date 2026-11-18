"""Shared source/selection validation used by every business response strategy."""

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
