"""Conservative current-fact authority boundary, independent of planner selection."""

import re


def requests_course_search(text: str) -> bool:
    """Recognize explicit catalog requests, never merely a selected course in context."""
    if re.search(
        r"\b(guarantee\w*|placements?|jobs?|syllabus|instructors?|trainers?|"
        r"eligibility|prerequisites?|refund|policy|policies)\b",
        text,
        re.I,
    ):
        return False
    catalog = re.search(r"\b(courses?|batches|options)\b", text, re.I)
    action = re.search(
        r"\b(search|find|show|recommend|suggest|looking for|want|need)\b", text, re.I
    )
    budget = re.search(r"\b(budget|under|within|maximum|max|up to|less than)\b", text, re.I)
    search = re.search(r"\b(search|find|show)\b", text, re.I)
    return bool((catalog and (action or budget)) or (search and budget))


def contains_action_status_claim(text: str) -> bool:
    """Documents cannot attest to an actual customer's completed business operation."""
    operation = (
        r"(?:callback|call|lead|enquiry|inquiry|request|booking|appointment|payment|refund|"
        r"notification|summary|enrolment|enrollment|seat)"
    )
    status = (
        r"(?:saved|created|scheduled|booked|completed|confirmed|reserved|sent|delivered|"
        r"processed|paid|approved|enrolled|requested|generated|recorded|arranged|submitted|cancelled)"
    )
    return bool(
        re.search(
            r"\b(?:your|my|our)\b[^.!?\n]{0,80}\b"
            + operation
            + r"\b[^.!?\n]{0,40}\b(?:has|have|was|were|is|are)\s+"
            + r"(?:(?:been|not|already|successfully)\s+)*"
            + status
            + r"\b|\byou(?:'re| are| have been)\s+(?:(?:now|already|successfully)\s+)*"
            + status
            + r"\b|"
            + r"\b(?:i|we|i've|we've)\s+(?:(?:have|already|successfully|just)\s+)*"
            + status
            + r"\b|"
            + r"(?:^|[.!?]\s+)(?:(?:the|a)\s+)?"
            + operation
            + r"\s+(?:has been|was|is)\s+(?:successfully\s+)?"
            + status
            + r"\b",
            text.replace("’", "'"),
            re.I,
        )
    )


def requests_current_facts(text: str) -> bool:
    if re.search(
        r"\b(fees?|prices?|costs?|pricing|tuition|afford|seats?)\b|\bhow\s+much\b", text, re.I
    ):
        return True
    # Trial/library policy availability is not live course/member inventory.
    subject = re.search(
        r"\b(courses?|classes|batches|memberships?|plans?|it|that|one)\b|"
        r"\b[A-Z]{2,8}\d{2,6}\b",
        text,
        re.I,
    )
    return bool(
        subject
        and re.search(r"\b(availability|available|schedules?|timings?|duration)\b", text, re.I)
    )


def contains_current_fact_claim(text: str) -> bool:
    # Source references remain inspectable, but currency/current-status claims cannot
    # be promoted into the assistant's factual answer even on a mistaken planner path.
    return bool(
        re.search(
            r"(?:\bINR\b|\brupees?\b|\u20b9|\$)\s*[\w\d]|"
            r"\b(?:fee|price|cost|tuition)\s+(?:is|are|:|=)|"
            r"\bavailability\s*:\s*(?:available|full)|"
            r"\bschedule\s+(?:is|are|:|=)|"
            r"\b[A-Z]{2,8}\d{2,6}\b[^.\n]*\b(?:full|available)\b",
            text,
            re.I,
        )
    )


def policy_excerpt(text: str) -> str:
    """Keep policy paragraphs separate from prices/current inventory statements.

    The full immutable excerpt remains in Sources. Suppressed paragraphs are never
    replaced with model-generated price/status claims.
    """

    def asserts_price(paragraph: str) -> bool:
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            for term in re.finditer(
                r"\b(fees?|prices?|costs?|pricing|tuition|rupees?|free)\b", sentence, re.I
            ):
                # A disclaimer must qualify this price term, not an unrelated clause.
                prefix = sentence[: term.start()]
                suffix = sentence[term.end() :]
                unknown = re.search(
                    r"\b(?:establishes no|(?:does not|do not|cannot|can't)\s+"
                    r"(?:establish|specify|verify|confirm))\s+(?:current\s+)?$",
                    prefix,
                    re.I,
                ) or re.match(
                    r"\s+(?:(?:is|are)\s+)?(?:not|never)\s+"
                    r"(?:specified|established|documented|provided|verified)\b",
                    suffix,
                    re.I,
                )
                if not unknown:
                    return True
        return False

    return "\n\n".join(
        paragraph
        for paragraph in re.split(r"\n\s*\n", text)
        if not contains_current_fact_claim(paragraph)
        and not asserts_price(paragraph)
        and not re.search(
            r"\b(?:courses?|classes|memberships?|batches)\b[^.\n]*"
            r"\b(?:available|full|weekdays?|weekends?)\b",
            paragraph,
            re.I,
        )
    ).strip()


def policy_reply(text: str, question: str) -> str:
    """Present source text in plain language without inventing a paraphrase.

    Prefer sections explicitly named by the question. Preserve complete paragraphs
    (including conditions and limitations), and fall back to all policy paragraphs
    if no section matches. Sources retain the original Markdown unchanged.
    """
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if lines:
                sections.append((heading, lines))
            # A document title describes every section, not just its introduction.
            heading = match.group(1) if line.lstrip().startswith("##") else ""
            lines = []
        else:
            lines.append(line)
    if lines:
        sections.append((heading, lines))

    stop = {"the", "a", "an", "is", "are", "does", "how", "what", "and", "or", "policy"}

    def words(value: str) -> set[str]:
        return {word.rstrip("s") for word in re.findall(r"[a-z]+", value.lower())} - stop

    requested = words(question)
    scores = [len(words(title) & requested) for title, _ in sections]
    best = max(scores, default=0)
    matching = [
        section for section, score in zip(sections, scores, strict=True) if score == best and best
    ]
    selected = matching or sections
    content = "\n\n".join("\n".join(body) for _, body in selected)
    content = policy_excerpt(content)
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", content)
        if paragraph.strip()
        and not re.match(r"This (?:is a fictional|fictional .*assessment)", paragraph.strip(), re.I)
    ]
    answer = "\n\n".join(paragraphs)
    answer = re.sub(r"\*\*([^*]+)\*\*", r"\1", answer)
    answer = re.sub(r"`([^`]+)`", r"\1", answer)
    answer = re.sub(r"\bA customer should\b", "You should", answer)
    answer = re.sub(
        r"\bThis (?:assessment|prototype|demo) has no payment or enrolment integration\.",
        "I can't take payments or complete enrollment here.",
        answer,
        flags=re.I,
    )
    answer = answer.replace(
        "This assessment does not create fitness leads, callbacks, payments or enrolments.",
        "I can't save fitness enquiries, request callbacks, "
        "take payments or complete enrollment here.",
    )
    answer = re.sub(r"\bThis (?:prototype|demo) does not\b", "I can't", answer, flags=re.I)
    answer = re.sub(r"\bThis (?:prototype|demo)\b", "This assistant", answer, flags=re.I)
    answer = re.sub(
        r"\bOne (introductory trial visit) is available per person, subject to (.+?)\.",
        r"You can have one \1 per person, subject to \2.",
        answer,
    )
    prerequisite = re.fullmatch(
        r"([^\n.]+ \([A-Za-z0-9._:-]+\)) requires ([^.\n]+)\.", answer.strip()
    )
    if prerequisite:
        answer = f"For {prerequisite[1]}, you'll need {prerequisite[2]}."
    return answer.strip()


def verified_policy_quote(quote: str, source: str) -> str | None:
    """Validate exact spans and restore complete coupled-condition paragraphs."""
    if not quote.strip() or quote not in source:
        return None
    start = source.index(quote)
    end = start + len(quote)
    offset = 0
    for paragraph in re.split(r"\n\s*\n", source):
        position = source.index(paragraph, offset)
        offset = position + len(paragraph)
        if (
            re.search(r"\b(?i:both|unless|only when|only if|subject to)\b|\bAND\b", paragraph)
            and start < offset
            and end > position
        ):
            start = min(start, position)
            end = max(end, offset)
    return source[start:end].strip()
