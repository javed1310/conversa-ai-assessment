"""Conversational wording built only from validated state and checked course records."""

import re

from app.contracts import CustomerState
from app.generation import CourseField
from app.persistence_models import Course


def acknowledges_preference(
    course: Course, text: str, state: CustomerState, *, acknowledge: bool = False
) -> bool:
    """Determine a server-owned speech act, independently of rendered document text."""
    preference_only = re.fullmatch(
        r"\s*(?:i\s+)?(?:prefer|would prefer|want|would like|choose|will choose|"
        r"can only attend)(?:\s+to attend)?\s+"
        r"(?:classes\s+(?:on\s+)?)?(?:the\s+)?(weekends?|weekdays?)"
        r"(?:\s+(?:batch|batches|classes|sessions|schedule|option))?\s*[.!]?\s*",
        text,
        re.I,
    )
    return bool(
        (preference_only or acknowledge)
        and state.fields.schedule == course.schedule
        and (not preference_only or preference_only.group(1).lower().rstrip("s") == course.schedule)
        and course.availability == "available"
        and not state.selection_conflicts
    )


def course_reply(
    course: Course,
    text: str,
    state: CustomerState,
    *,
    acknowledge: bool = False,
    fields: tuple[CourseField, ...] = (),
    confirmation: bool = False,
) -> str:
    if acknowledges_preference(course, text, state, acknowledge=acknowledge):
        return (
            f"That fits your preference—{course.name} runs on {course.schedule}s. "
            "Would you like to enquire about it?"
        )
    if fields:
        facts = {
            "fee": f"costs INR {course.fee:g}",
            "schedule": f"runs on {course.schedule}s",
            "availability": "is currently available"
            if course.availability == "available"
            else "is currently full",
        }
        detail = "; ".join(facts[key] for key in dict.fromkeys(fields))
        return (
            "I checked the current course record: " if confirmation else ""
        ) + f"{course.name} {detail}."
    availability = (
        "is currently available" if course.availability == "available" else "is currently full"
    )
    return (
        f"{course.name} ({course.course_id}) costs INR {course.fee:g}, "
        f"runs on {course.schedule}s, and {availability}."
    )


def course_options(courses: tuple[Course, ...] | list[Course]) -> str:
    if not courses:
        return "No courses match your current budget and schedule preferences."
    return "Here are the courses that match your preferences:\n" + "\n".join(
        f"{index}. {course.name} ({course.course_id}) — INR {course.fee:g}; "
        f"{course.schedule}; {course.availability}."
        for index, course in enumerate(courses, 1)
    )
