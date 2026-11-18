"""Immutable input/dialogue facts only; never cache mutable state or write authority."""

import re

from app.contracts import Content, Contract, Identifier
from app.dialogue import NextStep, affirmation


class TurnInput(Contract):
    turn_id: Identifier
    message_id: Identifier
    sequence: int
    input_text: Content


class PendingDialogue(Contract):
    discussed_course_id: Identifier | None = None
    failure_codes: tuple[str, ...] = ()
    next_step: NextStep = "none"
    course_id: Identifier | None = None


class TurnDialogue(Contract):
    turn: TurnInput
    pending: PendingDialogue

    @property
    def plain_acknowledgement(self) -> bool:
        return (
            self.pending.next_step in {"offer_enquiry", "collect_enquiry"}
            and self.pending.course_id is not None
            and (
                affirmation(self.turn.input_text)
                or bool(
                    re.fullmatch(
                        r"\s*(?:no|no thanks|not now|maybe later|cancel|never mind|nevermind)"
                        r"\s*[.!]?\s*",
                        self.turn.input_text,
                        re.I,
                    )
                )
            )
        )
