"""Customer intent changes wording without weakening checked facts or preference validation."""

import pytest

from app.contracts import CustomerEvidence, CustomerFields, CustomerState
from app.persistence_models import Course
from app.presentation import course_options, course_reply


def course(**changes):
    values = dict(
        business_id="training-institute",
        course_id="DS202",
        name="Applied Data Analytics",
        category="data-science",
        fee=28000,
        schedule="weekend",
        availability="available",
    )
    return Course.model_validate(values | changes)


def state(schedule="weekend", conflicts=()):
    return CustomerState(
        fields=CustomerFields(schedule=schedule),
        provenance={
            "schedule": CustomerEvidence(turn_id="test-turn", excerpt=f"I prefer {schedule}s.")
        },
        selection_conflicts=conflicts,
    )


def test_search_details_and_preference_are_distinct():
    item = course()
    listing = course_options([course(course_id="DS101"), item])
    details = course_reply(
        item, "I want the second one. What are its fee, schedule and availability?", state()
    )
    acknowledgement = course_reply(item, "I prefer weekends.", state())
    assert "1." in listing and "2." in listing
    assert all(value in details for value in ["DS202", "28000", "weekend", "available"])
    assert "fits your preference" in acknowledgement
    assert "Would you like to enquire" in acknowledgement
    assert "28000" not in acknowledgement and "saved" not in acknowledgement
    assert len({listing, details, acknowledgement}) == 3


@pytest.mark.parametrize(
    "text",
    [
        "i would prefer the weekend batch",
        "I prefer the weekend batch.",
        "I would like the weekend classes.",
        "I want to attend the weekend sessions.",
        "I choose the weekend option.",
        "I can only attend weekend classes.",
        "I prefer classes on weekends.",
        "I would prefer the weekday batch.",
    ],
)
def test_schedule_preference_wording_variations(text):
    schedule = "weekday" if "weekday" in text else "weekend"
    answer = course_reply(course(schedule=schedule), text, state(schedule=schedule))
    assert "fits your preference" in answer
    assert "28000" not in answer


@pytest.mark.parametrize(
    "text",
    [
        "I would prefer the weekend batch. What is the fee?",
        "I would prefer the weekend batch and request a callback.",
        "I would not prefer the weekend batch.",
        "Would the weekend batch suit me?",
        "I prefer the weekend batch or weekday classes.",
    ],
)
def test_preference_with_other_intent_is_not_reduced_to_acknowledgement(text):
    assert "28000" in course_reply(course(), text, state())


@pytest.mark.parametrize(
    "text",
    [
        "I prefer weekends. What is the fee?",
        "I prefer weekends. Please save my enquiry.",
        "I do not prefer weekends.",
        "I prefer weekdays.",
    ],
)
def test_questions_actions_negation_and_mismatch_keep_checked_details(text):
    assert "28000" in course_reply(course(), text, state())


@pytest.mark.parametrize("conflict", ["budget", "schedule", "full", "missing_course"])
def test_conflicts_never_receive_positive_fit_claim(conflict):
    assert "fits your preference" not in course_reply(
        course(), "I prefer weekends.", state(conflicts=(conflict,))
    )


def test_full_course_or_unvalidated_schedule_never_receive_positive_fit_claim():
    assert "currently full" in course_reply(
        course(availability="full"), "I prefer weekends.", state()
    )
    assert "fits your preference" not in course_reply(
        course(), "I prefer weekends.", CustomerState()
    )
    assert "No courses match" in course_options([])
