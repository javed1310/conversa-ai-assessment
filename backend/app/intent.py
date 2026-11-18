"""Shared conservative callback authorization policy; quotations are never consent."""

import re
from typing import Literal


def callback_intent(text: str) -> Literal["request", "revoke", "none"]:
    normalized = text.replace("\u2018", "'").replace("\u2019", "'")
    normalized = normalized.replace("\u201c", '"').replace("\u201d", '"')
    normalized = re.sub(r'"[^"\n]*"|(?<!\w)\'[^\'\n]*\'(?!\w)|`[^`\n]*`', "", normalized)
    result: Literal["request", "revoke", "none"] = "none"
    for clause in re.split(r"[.!?;,\n]+|\b(?:and|but|however|instead)\b", normalized, flags=re.I):
        clause = clause.strip().casefold()
        clause = re.sub(r"\bi'd\b", "i would", clause)
        if re.search(r"\b(phrase|example|meaning|said|says|wrote|quoted|quote)\b", clause):
            continue
        if re.search(
            r"\b(?:do not|don't|never)\s+(?:(?:want|wish|need)\s+(?:you\s+)?to\s+)?cancel\b",
            clause,
        ):
            continue
        # A reminder requests a call; negation of calling itself must still win.
        clause = re.sub(r"\b(?:do not|don't)\s+forget\s+to\s+", "", clause)
        # Negation must target calling/callbacks, not unrelated experience or phone features.
        revoked = re.search(
            r"\b(?:do not|don't|never|stop)\s+(?:ever\s+|please\s+)?(?:call|phone|contact)\b|"
            r"\b(?:do not|don't|no longer)\s+(?:want|need|request)\s+"
            r"(?:a\s+|the\s+)?(?:call|callback)\b|"
            r"\b(?:do not|don't|never)\s+(?:arrange|request|book|schedule)\s+"
            r"(?:(?:a|my|the)\s+)?callback\b|"
            r"\bcancel\s+(?:(?:my|the|a|existing)\s+)?(?:callback|call|contact request)\b|"
            r"\bno\s+(?:(?:more|further)\s+)?(?:callbacks?|calls?|phone calls?)\b",
            clause,
        )
        revoked = revoked or re.search(
            r"\b(?:do not|don't|no longer)\s+(?:want|wish|need|think|believe|request)\b"
            r"(?:\s+[\w']+){0,8}\s+(?:call|phone|contact)\s+me\b|"
            r"\b(?:rather|prefer)\b[^.!?;]*\b(?:didn't|did not|not)\s+"
            r"(?:to\s+)?(?:call|phone|contact)\s+me\b|"
            r"\b(?:not|never)\s+(?:call|phone|contact)\s+me\b",
            clause,
        )
        if revoked:
            result = "revoke"
            continue
        if re.match(r"^(what|how|why|explain|describe)\b", clause):
            continue
        if re.search(
            r"\bwill\s+(?:someone|somebody|you|a counselor|a counsellor)\s+call\s+me\b",
            clause,
        ):
            continue  # Asking whether a call will happen is not permission to create one.
        requested = re.search(
            r"\b(?:call|phone|contact)\s+me\b|\bgive\s+me\s+a\s+call\b|"
            r"\bI\s+(?:would like|want|need|request)\s+(?:a\s+|the\s+)?callback\b|"
            r"\bplease\s+(?:(?:arrange|request|book)\s+)?(?:a\s+|the\s+)?callback\b|"
            r"\b(?:can|could|would)\s+you\s+(?:please\s+)?(?:arrange|request)\s+(?:a\s+)?callback\b|"
            r"^(?:please\s+)?(?:request|arrange|book|schedule|want|need)\s+"
            r"(?:(?:a|the|my)\s+)?callback\b|"
            r"^(?:please\s+)?call\s+(?:me\s+)?back\b",
            clause,
            re.I,
        )
        if requested:
            result = "request"
    return result


def callback_request(text: str) -> bool:
    return callback_intent(text) == "request"


def callback_revoked(text: str) -> bool:
    return callback_intent(text) == "revoke"
