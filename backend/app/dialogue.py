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


def enrollment_process_candidate(text: str) -> bool:
    """A cheap relevance gate, not authority to classify intent or perform actions."""
    return bool(
        not re.search(
            r"[\"`]|\b(no|not|never|don't|refund|fees?|price|availability|eligibility|"
            r"prerequisites?|callback|call me|my name|my phone|budget|ignore|override)\b|\d{6,}",
            text.replace("\u2019", "'"),
            re.I,
        )
        and re.search(
            r"\b(enroll?(?:ing)?|enrolment|enrollment|join|joining|registration|admission)\b",
            text,
            re.I,
        )
        and re.search(r"\b(process|procedure|steps?|how|what)\b", text, re.I)
    )


def asks_enrollment_next_step(text: str) -> bool:
    """A process question offers an enquiry; it never authorizes a write."""
    return (
        re.fullmatch(
            r"\s*(?:if\s+i\s+(?:want|would like)\s+to\s+(?:enrol|enroll|join)"
            r"(?:\s+(?:it|this course|that course))?\s*[,;]?\s*"
            r"what\s+(?:should|would)\s+be\s+(?:the|my)\s+next\s+steps?"
            r"|what\s+(?:is|are)\s+(?:the|my)\s+next\s+steps?\s+"
            r"(?:to|for)\s+(?:enrol|enroll|enrolling|enrolment|enrollment|joining)"
            r"(?:\s+(?:it|this course|that course))?)\s*[?.!]*\s*",
            text,
            re.I,
        )
        is not None
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


def post_enquiry_question_candidate(text: str) -> bool:
    """Only route read-only follow-ups; corrections and business questions stay separate."""
    return bool(
        len(text) <= 1000
        and not re.search(
            r"[\"`]|\b(and|also|but|refund|policy|fees?|price|budget|schedule|weekends?|"
            r"weekdays?|availability|eligibility|prerequisites?|my name|my phone|instead|"
            r"another|different|ignore|override)\b|\d{6,}",
            text,
            re.I,
        )
        and re.search(r"\b(next|now|what|how|will|when|callback|call|contact)\b", text, re.I)
    )


def asks_after_enquiry_next_step(text: str) -> bool:
    return (
        re.fullmatch(
            r"\s*(?:(?:okay|ok|great|thanks)\s*[,!.]?\s*)?(?:now\s+)?"
            r"(?:what\s+should\s+i\s+do(?:\s+now)?|what(?:'s| is)\s+next|"
            r"what\s+(?:are|is)\s+(?:my|the)\s+next\s+steps?)\s*[?.!]*\s*",
            text,
            re.I,
        )
        is not None
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
            r"\bwill\s+(?:(?:i|we)\s+)?(?:get|receive)\s+(?:a\s+)?(?:call|callback)\b|"
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
