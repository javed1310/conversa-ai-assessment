"""Optional grounded paraphrases; verified facts, references and receipts stay server-owned."""

import re
from decimal import Decimal

from app.actions import WRITE_TOOLS
from app.agent_context import TurnWork
from app.contracts import ChatResult
from app.facts import contains_action_status_claim
from app.gemini import ProviderFailure
from app.generation import EvidenceItem, GenerationService, ReplySelection
from app.measurements import count
from app.provider_control import remaining


def numbers(text: str) -> set[str]:
    values = {
        format(Decimal(value.replace(",", "")).normalize(), "f")
        for value in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", text)
    }
    words = dict(
        zip(
            ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
            map(str, range(11)),
            strict=True,
        )
    )
    return values | {
        words[word] for word in re.findall(r"\b[a-z]+\b", text.lower()) if word in words
    }


class ReplyComposer:
    def __init__(self, generation: GenerationService) -> None:
        self.generation = generation

    async def compose(
        self, fallback: ChatResult, selection: ReplySelection, work: TurnWork, customer_text: str
    ) -> ChatResult:
        draft = selection.conversational_draft
        if draft is None or work.response_locked or work.failures or not work.response_evidence:
            return fallback
        if any(action.result.tool in WRITE_TOOLS for action in work.actions):
            return fallback  # Action outcomes always use real server-rendered receipts.
        if remaining() < 8:
            count("conversational_reply_deadline_fallback")
            return fallback
        candidate = "\n\n".join(sentence.text.strip() for sentence in draft.sentences)
        if (
            len(candidate) > 8000
            or contains_action_status_claim(candidate)
            or re.search(r"(?m)^\s*#{1,6}\s", candidate)
        ):
            count("conversational_reply_rejected")
            return fallback
        referenced: dict[str, str] = {}
        for sentence in draft.sentences:
            if len(set(sentence.evidence_keys)) != len(sentence.evidence_keys):
                return fallback
            if not set(sentence.evidence_keys) <= work.response_evidence.keys():
                return fallback
            facts = "\n".join(work.response_evidence[key] for key in sentence.evidence_keys)
            if not numbers(sentence.text) <= numbers(facts):
                count("conversational_reply_rejected")
                return fallback
            for value in re.findall(r"https?://\S+|www\.\S+|\b[A-Z]{2,8}\d{2,6}\b", sentence.text):
                if value not in facts:
                    count("conversational_reply_rejected")
                    return fallback
            referenced.update({key: work.response_evidence[key] for key in sentence.evidence_keys})
        try:
            review = await self.generation.review_reply(
                customer_text,
                draft,
                tuple(EvidenceItem(key=key, text=text) for key, text in referenced.items()),
                fallback.reply,
            )
        except ProviderFailure:
            count("conversational_reply_provider_fallback")
            return fallback
        if not all(
            (review.grounded, review.relevant, review.preserves_conditions, review.no_action_claims)
        ):
            count("conversational_reply_rejected")
            return fallback
        count("conversational_reply_accepted")
        return fallback.model_copy(update={"reply": candidate})
