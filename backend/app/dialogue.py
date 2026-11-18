"""Server-owned dialogue transitions; acknowledgements never authorize a callback."""

import re
from typing import Literal

NextStep = Literal["none", "offer_enquiry", "collect_enquiry", "offer_callback", "clarify_course"]


def safe_discussion_choice(text: str) -> bool:
    """Independent exclusions for a reviewed proposal; this is not an intent classifier."""
    return not bool(
        re.search(
            r"[\"`]|\b(no|not|never|don't|if|unless|maybe|example|quote|quoted|"
            r"actually|instead|different|another|budget|refund|policy|eligibility|prerequisite|"
            r"ignore|override|instructions)\b|\b(?:what|why|when|where)\b",
            text.replace("\u2019", "'"),
            re.I,
        )
    )


def starts_discussed_enquiry(text: str) -> bool:
    """A narrow request to proceed with this course, never payment/enrolment execution."""
    if re.search(
        r"[\"`]|\b(no|not|don't|never|refund|policy|price|fee|another|different|"
        r"my name|my phone|and|also|but|eligibility|prerequisites)\b|\d{6,}",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(r"\b(this course|it)\b", text, re.I)
        and re.search(
            r"\bhow\s+(?:can|do|should)\s+i\b.*\b(choose|start|join|enrol|enroll|proceed)\b",
            text,
            re.I,
        )
    )


def expresses_discussed_interest(text: str) -> bool:
    """Positive deictic interest, optionally followed by an explicit callback request.

    A question about a course, a mere acknowledgement, quoted/conditional interest,
    another named course or a mixed correction cannot select the discussion focus.
    """
    return bool(
        re.fullmatch(
            r"\s*(?:(?:great|okay|ok|yes|perfect)\s*[,!.]?\s*)?"
            r"i(?:\s+am|'m|\u2019m)\s+(?:really\s+)?interested"
            r"(?:\s+in\s+(?:it|this(?:\s+(?:course|one))?|that(?:\s+(?:course|one))?))?"
            r"\s*[.!?,]?\s*"
            r"(?:(?:(?:can|could)\s+(?:someone|somebody|you|a\s+counsell?or)\s+"
            r"(?:please\s+)?call\s+me|(?:please\s+)?call\s+me)\s*[.!?]?)?\s*",
            text,
            re.I,
        )
    )


def callback_status_question(text: str) -> bool:
    """A narrow status question, not an instruction to call or a mixed request."""
    if re.search(
        r"\b(and|also|but|please|request a|arrange|book|refund|policy|fees?|budget|"
        r"my name|my phone|instead|cancel|don't|do not)\b|\d{6,}",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"\bwill\s+(?:i|we)\s+(?:get|receive)\s+(?:a\s+)?(?:call|callback)\b|"
            r"\bwill\s+(?:someone|somebody|you|a counselor|a counsellor)\s+call\s+me\b|"
            r"\b(?:has|is|was)\s+(?:my|the)\s+callback\s+(?:been\s+)?"
            r"(?:requested|recorded|scheduled|confirmed)\b",
            text,
            re.I,
        )
    )


def affirmation(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:yes|yeah|yep|sure|okay|ok|absolutely|please do|go ahead)"
            r"(?:\s*,?\s*(?:please|go ahead|that sounds good))?\s*[.!]?\s*",
            text,
            re.I,
        )
    )


def decline(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:no|no thanks|not now|maybe later|cancel|never mind|nevermind)\s*[.!]?\s*",
            text,
            re.I,
        )
        or re.search(
            r"\b(?:do not|don't|don’t|never)\s+(?:(?:want|wish)\s+to\s+)?"
            r"(?:save|create|submit|continue|proceed)\b|"
            r"\b(?:cancel|stop|withdraw)\s+(?:my\s+|the\s+)?(?:enquiry|inquiry|request)\b",
            text,
            re.I,
        )
    )


def bare_course_selection(text: str) -> bool:
    return bool(
        re.fullmatch(
            r"\s*(?:the\s+)?(?:first|second|third|fourth|1st|2nd|3rd|4th)"
            r"(?:\s+(?:one|course|option))?\s*[.!]?\s*",
            text,
            re.I,
        )
    )
