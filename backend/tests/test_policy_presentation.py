from app.facts import policy_reply
from app.settings import REPOSITORY_ROOT


def test_trial_answer_is_plain_conversational_and_relevant():
    content = (REPOSITORY_ROOT / "businesses/fitness-studio/knowledge/policy.md").read_text(
        encoding="utf-8"
    )
    reply = policy_reply(content, "How does an introductory trial visit work?")
    assert reply.startswith("You can have one introductory trial visit per person")
    assert "confirming a suitable time" in reply
    assert "You should bring clean indoor training shoes and a water bottle" in reply
    assert "I can't book a trial visit or verify" in reply
    assert "#" not in reply and "Membership information" not in reply
    assert "fictional fitness" not in reply and "medical advice" not in reply


def test_refund_answer_preserves_both_conditions_and_exclusions():
    content = (REPOSITORY_ROOT / "businesses/training-institute/knowledge/refund.md").read_text(
        encoding="utf-8"
    )
    reply = policy_reply(content, "What is the refund policy?")
    assert "BOTH conditions" in reply and "within seven days" in reply
    assert "has not attended the first class" in reply
    assert "after seven days does not qualify" in reply
    assert "cannot confirm that money was refunded" in reply
    assert "#" not in reply


def test_current_prices_are_still_excluded():
    reply = policy_reply("# Policy\n\nFee is INR 999.\n\nBring water.", "What should I bring?")
    assert "999" not in reply and "Bring water" in reply


def test_headingless_documents_preserve_conditions():
    text = "Refund requires seven days AND no first class attendance."
    assert policy_reply(text, "refund?") == text


def test_policy_limits_do_not_erase_unrelated_documented_access_rules():
    text = (
        "Practice library access: Learners may visit Saturdays from 10 AM to noon. "
        "Bring your learner ID. This policy establishes no fees or course seat availability."
    )
    reply = policy_reply(text, "When can I access the practice library and what should I bring?")
    assert "Saturdays" in reply and "learner ID" in reply


def test_price_assertions_remain_blocked_without_currency_symbols():
    assert not policy_reply("Course tuition is twenty thousand. Courses are free.", "Course fees?")
    assert not policy_reply("Prices start at twenty thousand.", "Course fees?")
    assert not policy_reply("No fees apply.", "Course fees?")


def test_prerequisite_answer_is_conversational_without_agreeing_to_wrong_premise():
    reply = policy_reply(
        "Advanced Python (PY202) requires basic programming knowledge.",
        "Does Advanced Python require advanced programming experience?",
    )
    assert reply == "For Advanced Python (PY202), you'll need basic programming knowledge."
    assert not reply.startswith("Yes")


def test_internal_assessment_language_becomes_customer_capability_limit():
    source = (
        "An enquiry is not enrolment or a seat reservation. "
        "This assessment has no payment or enrolment integration."
    )
    reply = policy_reply(source, "Is an enquiry enrollment?")
    assert "An enquiry is not enrolment or a seat reservation" in reply
    assert "I can't take payments or complete enrollment here" in reply
    assert "assessment" not in reply.lower()
    assert "This assessment" in source


def test_real_business_assessment_rules_are_not_rewritten_as_assistant_capabilities():
    source = "This assessment does not require prior coding knowledge."
    assert policy_reply(source, "What are the assessment prerequisites?") == source


def test_fitness_internal_scope_is_explained_as_capabilities():
    reply = policy_reply(
        "This assessment does not create fitness leads, callbacks, payments or enrolments.",
        "Can you arrange membership enrollment?",
    )
    assert "assessment" not in reply.lower()
    assert "can't save fitness enquiries" in reply
    assert "take payments or complete enrollment" in reply
