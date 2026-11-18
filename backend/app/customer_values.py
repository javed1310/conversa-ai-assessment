"""Conservative literal evidence matching for normalized customer values."""

import re

from app.phones import normalize_phone, phone_is_supported

FIELD_WORDS = {
    "name": r"name",
    "phone": r"phone|number|mobile",
    "budget": r"budget|spending limit",
    "schedule": r"schedule|weekday|weekend",
    "course_id": r"course|selection",
    "stated_interest": r"interest|preference",
}


def explicit_clear(field: str, excerpt: str) -> bool:
    lower = excerpt.casefold()
    if re.search(r"\b(do not|don't|never|not)\s+(remove|clear|forget|delete|unset)", lower):
        return False
    return bool(
        re.search(r"\b(remove|clear|forget|delete|unset)\b", lower)
        and re.search(r"\b(?:" + FIELD_WORDS[field] + r")\b", lower)
    )


def supports_value(field: str, value: object, excerpt: str) -> bool:
    if field == "phone":
        return phone_is_supported(value, excerpt)
    if field == "budget":
        for token, unit in re.findall(
            r"(?<![\w.])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(k|thousand|lakh)?\b",
            excerpt.casefold(),
        ):
            amount = (
                float(token.replace(",", ""))
                * {"": 1, "k": 1000, "thousand": 1000, "lakh": 100000}[unit]
            )
            if amount == value:
                return True
        return False
    if field == "schedule":
        categories = set()
        for clause in re.split(r"[.!?;,]|\b(?:and|but|instead)\b", excerpt.casefold()):
            if re.search(r"\b(no|not|never|can't|cannot|except)\b", clause):
                continue
            categories.update(re.findall(r"\b(weekday|weekend)s?\b", clause))
            days = set(
                re.findall(
                    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", clause
                )
            )
            if days & {"saturday", "sunday"}:
                categories.add("weekend")
            if days - {"saturday", "sunday"}:
                categories.add("weekday")
        return categories == {value}
    return re.search(r"(?<!\w)" + re.escape(str(value)) + r"(?!\w)", excerpt, re.I) is not None


def contact_line(text: str) -> tuple[str, str] | None:
    """Only a complete standalone name and phone, not prose or instructions."""
    if re.search(
        r"\b(no|not|never|don't|do|call|save|example|quote|phone|number|name|is|my|"
        r"instead|wrong|fake|remove|ignore|instructions)\b|[\"`!?]",
        text,
        re.I,
    ):
        return None
    match = re.fullmatch(
        r"\s*([^\W\d_][^\W\d_ .'-]*(?:[ .'-]+[^\W\d_]+){0,5})\s*[,;]?\s+"
        r"(\+?[0-9(][0-9 ().-]{1,61}[0-9)])\s*\.?\s*",
        text,
    )
    if match is None:
        return None
    try:
        normalize_phone(match[2])
    except ValueError:
        return None
    # Keep literal formatting as evidence; CustomerFields supplies canonical storage.
    return match[1].strip(), match[2]
