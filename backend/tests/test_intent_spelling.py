import pytest

from app.dialogue import bare_course_selection, starts_discussed_enquiry
from app.intent_spelling import reference_spelling
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change


@pytest.mark.parametrize("word", ["enquie", "enqure", "enqurie", "enquuire", "inqure", "enquire"])
def test_enquiry_single_edit_intent(word):
    assert starts_discussed_enquiry(f"i want to {word} about it")


@pytest.mark.parametrize("text", ["first ine", "frist one", "frst one", "secod one", "third oen"])
def test_reference_single_edit(text):
    assert bare_course_selection(text)


@pytest.mark.parametrize(
    "text",
    [
        "not first one",
        "first none",
        "first once",
        '"first ine"',
        "first ine and second one",
        "DS102",
        "9876543210",
        "my budget is 20000",
    ],
)
def test_ambiguous_or_customer_values_unchanged(text):
    assert reference_spelling(text) == text
    assert not bare_course_selection(text)


@pytest.mark.parametrize(
    "text",
    [
        "I don't want to enquie about it",
        '"I want to enquie about it"',
        "If I want to enquie about it, what happens?",
        "I want to enquie about it and call me",
    ],
)
def test_enquiry_spelling_does_not_remove_consent_guards(text):
    assert not starts_discussed_enquiry(text)


async def test_reported_typos_keep_original_evidence_and_use_owned_course(agent_env):
    env = agent_env
    await env.send(
        "Find data science under 30000",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
    )
    requests = len(env.model.requests)
    env.model.fail_state = True
    selected = await env.send("first ine")
    assert "Foundations" in selected.reply
    assert selected.actions[0].result.status == "success"
    response = await env.send("i want to enquie about it")
    assert "name" in response.reply and "phone" in response.reply
    assert len(env.model.requests) == requests
    record = await env.storage.db.sessions.find_one({"session_id": response.session_id})
    assert record["state"]["fields"]["course_id"] == "DS101"
    assert record["state"]["provenance"]["course_id"]["excerpt"] == "i want to enquie about it"
    assert await env.storage.db.leads.count_documents({}) == 0
