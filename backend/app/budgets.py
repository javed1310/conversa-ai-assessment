"""One amount grammar for customer evidence and model-proposed INR budgets."""

import math
import re
from decimal import Decimal

_FORM = (
    r"(?:₹\s*|inr\s*|rs\.?\s*)?"
    r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(k|thousand|lakh)?"
    r"(?:\s*(?:inr|rupees))?"
)
_VALUE = re.compile(_FORM, re.I)
_IN_TEXT = re.compile(r"(?<![\w.,+-])" + _FORM + r"(?![\w,]|\.[0-9])", re.I)
_MULTIPLIERS = {"": 1, "k": 1000, "thousand": 1000, "lakh": 100000}


def normalize_budget(value: str) -> float:
    match = _VALUE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Provide a numeric INR budget with a supported unit")
    amount = float(Decimal(match[1].replace(",", "")) * _MULTIPLIERS[(match[2] or "").casefold()])
    if not math.isfinite(amount):
        raise ValueError("Provide a finite budget")
    return amount


def budget_is_supported(value: object, text: str) -> bool:
    if isinstance(value, bool):
        return False
    for match in _IN_TEXT.finditer(text):
        try:
            if normalize_budget(match[0]) == value:
                return True
        except ValueError:
            continue
    return False
