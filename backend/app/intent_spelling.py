"""Bounded spelling hints for intent words, never normalized customer facts."""

import re


def one_edit(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        different = [
            i for i, pair in enumerate(zip(left, right, strict=True)) if pair[0] != pair[1]
        ]
        if len(different) <= 1:
            return True
        return (
            len(different) == 2
            and different[1] == different[0] + 1
            and left[different[0]] == right[different[1]]
            and left[different[1]] == right[different[0]]
        )
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    return any(longer[:i] + longer[i + 1 :] == shorter for i in range(len(longer)))


def enquiry_spelling(text: str) -> str:
    vocabulary = ("enquire", "enquiry", "inquire", "inquiry")

    def replace(match: re.Match[str]) -> str:
        word = match[0].casefold()
        if word in vocabulary or len(word) < 5:
            return match[0]
        return "enquire" if any(one_edit(word, target) for target in vocabulary) else match[0]

    return re.sub(r"[a-zA-Z]+", replace, text)


def reference_spelling(text: str) -> str:
    # Whole selection phrases only. Questions, negation, quotes and mixed messages
    # do not enter this hint path; their original text stays authoritative.
    match = re.fullmatch(r"\s*(?:the\s+)?([a-z]{4,8})(?:\s+([a-z]{3,6}))?\s*([.!]?)\s*", text, re.I)
    if match is None:
        return text
    ordinal, noun = match[1].casefold(), (match[2] or "").casefold()
    targets = [w for w in ("first", "second", "third", "fourth") if one_edit(ordinal, w)]
    if len(targets) != 1:
        return text
    if noun and noun not in {"one", "course", "option"}:
        # Short words require a substitution/transposition, not deletion of a
        # meaningful word such as 'none', 'once' or 'not'.
        if (
            len(noun) != 3
            or noun in {"not", "now", "any", "owe", "ore"}
            or not one_edit(noun, "one")
        ):
            return text
        noun = "one"
    return targets[0] + (" " + noun if noun else "") + match[3]
