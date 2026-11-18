"""One phone policy for contracts, extraction, provenance and action qualification.

International numbers require an explicit +country code. Existing Indian mobile
syntax is retained; format/metadata validity never proves ownership or reachability.
"""

import re

import phonenumbers

PHONE_PROMPT = "phone number (include the country code if outside India)"

_FORMATTED = re.compile(r"\+?[0-9 ().-]+")
_CANDIDATES = re.compile(r"(?:\+ *)?[0-9(](?:[0-9 ()-]|\.(?=[0-9]))*")


def normalize_phone(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Provide a phone number with +country code outside India")
    text = value.strip()
    if not _FORMATTED.fullmatch(text):
        raise ValueError("Use digits and ordinary phone formatting; extensions are not supported")
    depth = 0
    for char in text:
        if char == "(":
            if depth:
                raise ValueError("Invalid phone parentheses")
            depth = 1
        elif char == ")":
            if not depth:
                raise ValueError("Invalid phone parentheses")
            depth = 0
    if depth:
        raise ValueError("Invalid phone parentheses")
    compact = re.sub(r"[ ().-]", "", text)
    domestic = compact.removeprefix("+91")
    if re.fullmatch(r"[6-9][0-9]{9}", domestic):
        # Preserve the supplied assessment's fictional Indian numbers and saved records.
        return "+91" + domestic
    if not re.fullmatch(r"\+[1-9][0-9]{1,14}", compact):
        raise ValueError("Include +country code; local numbers default to Indian mobile format")
    try:
        number = phonenumbers.parse(compact, None)
    except phonenumbers.NumberParseException:
        raise ValueError("Invalid international phone number") from None
    if not phonenumbers.is_possible_number(number) or not phonenumbers.is_valid_number(number):
        raise ValueError("Invalid international phone number")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def phone_is_supported(value: object, text: str) -> bool:
    """Match complete numeric runs; never salvage a valid suffix of an invalid number."""
    if not isinstance(value, str):
        return False
    try:
        expected = normalize_phone(value)
    except ValueError:
        return False
    for match in _CANDIDATES.finditer(text):
        candidate = match.group().rstrip(" ")
        start, end = match.start(), match.start() + len(candidate)
        # Check boundaries after matching the entire run, so regex backtracking cannot
        # salvage a valid prefix when a longer malformed run ends in a letter.
        if start and (text[start - 1].isalnum() or text[start - 1] in "_+"):
            continue
        if end < len(text) and (text[end].isalnum() or text[end] in "_+"):
            continue
        try:
            if normalize_phone(candidate) == expected:
                return True
        except ValueError:
            continue
    return False
