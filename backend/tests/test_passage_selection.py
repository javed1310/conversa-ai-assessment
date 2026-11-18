import pytest

from app.agent_context import Evidence, TurnWork
from app.contracts import SourceRef
from app.facts import policy_reply, verified_policy_quote
from app.knowledge import DocumentInput, KnowledgeService
from app.response_safety import include_beginner_alternative
from app.settings import REPOSITORY_ROOT
from tests.test_agent import agent_env as agent_env
from tests.test_knowledge import TestOnlyEmbedder

ANSWER = "Advanced Python (PY202) requires basic programming knowledge."


def test_specific_heading_wins_over_shared_python_word():
    text = (REPOSITORY_ROOT / "businesses/training-institute/knowledge/eligibility.md").read_text(
        encoding="utf-8"
    )
    result = policy_reply(text, "Does Advanced Python require programming experience?")
    assert ANSWER in result
    assert "Python Basics" not in result


async def ingest_courses(env):
    service = KnowledgeService(
        env.storage, env.registry.get("training-institute"), TestOnlyEmbedder(), env.settings
    )
    for name, text in [
        ("eligibility-selection", "Python Basics has no prerequisites.\n\n" + ANSWER),
        ("overview-selection", "The Python path includes Python Basics and Advanced Python."),
    ]:
        await service.ingest(
            DocumentInput(document_id=name, title=name, format="text", content=text)
        )


async def test_answer_uses_one_exact_sentence_not_all_retrieved_docs(agent_env):
    env = agent_env
    await ingest_courses(env)
    result = await env.send(
        "Does Advanced Python require programming experience?",
        steps=[[("readKnowledge", {"query": "Python prerequisites"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1", "source-2"],
            "clarification": "none",
            "policyPassages": [{"evidenceKey": "source-1", "quote": ANSWER}],
        },
    )
    assert result.reply == "For Advanced Python (PY202), you'll need basic programming knowledge."
    assert len(result.sources) == 1
    assert result.sources[0].document_id == "eligibility-selection"


async def test_missing_passages_never_dump_documents(agent_env):
    env = agent_env
    await ingest_courses(env)
    result = await env.send(
        "Does Advanced Python require programming experience?",
        steps=[[("readKnowledge", {"query": "Python prerequisites"})]],
        selection={"kind": "evidence", "evidenceKeys": ["source-1"], "clarification": "none"},
    )
    assert "don't have verified information" in result.reply
    assert "Python Basics" not in result.reply and not result.sources


async def test_invented_quote_is_rejected(agent_env):
    env = agent_env
    await ingest_courses(env)
    result = await env.send(
        "Does Advanced Python require programming experience?",
        steps=[[("readKnowledge", {"query": "Python prerequisites"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1"],
            "clarification": "none",
            "policyPassages": [{"evidenceKey": "source-1", "quote": "No experience is required."}],
        },
    )
    assert "No experience is required" not in result.reply and not result.sources


@pytest.mark.parametrize("marker", ["BOTH", "both", "only if", "subject to", "unless"])
def test_coupled_condition_cannot_be_cut_out(marker):
    text = f"Refund is permitted {marker} conditions hold. A second condition also applies."
    assert verified_policy_quote(text.split(".")[0] + ".", text) == text
    assert verified_policy_quote(text, text) == text


def test_unconditional_complete_sentence_can_be_selected():
    assert (
        verified_policy_quote(ANSWER, ANSWER + "\n\nOther courses have different requirements.")
        == ANSWER
    )


async def test_beginner_recommendation_keeps_retrieved_option_despite_short_draft(agent_env):
    env = agent_env
    await ingest_courses(env)
    result = await env.send(
        "Having never programmed, would PY202 be a sensible starting point?",
        steps=[[("readKnowledge", {"query": "Python prerequisites"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-1"],
            "clarification": "none",
            "policyPassages": [{"evidenceKey": "source-1", "quote": ANSWER}],
            "conversationalDraft": {
                "sentences": [
                    {
                        "text": "Advanced Python requires basic programming knowledge.",
                        "evidenceKeys": ["source-1"],
                    }
                ]
            },
        },
    )
    assert "basic programming knowledge" in result.reply
    assert "Python Basics has no prerequisites." in result.reply
    assert [s.document_id for s in result.sources] == ["eligibility-selection"]


async def test_mixed_question_keeps_requested_verified_fee_when_selection_omits_it(agent_env):
    env = agent_env
    await ingest_courses(env)
    result = await env.send(
        "For PY202, what does it cost now and what prior knowledge do I need?",
        steps=[
            [
                ("getCourseDetails", {"courseID": "PY202"}),
                ("readKnowledge", {"query": "Python prerequisites"}),
            ]
        ],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["source-2"],
            "clarification": "none",
            "courseUse": "none",
            "policyPassages": [{"evidenceKey": "source-2", "quote": ANSWER}],
            "conversationalDraft": {
                "sentences": [
                    {
                        "text": "Advanced Python requires basic programming knowledge.",
                        "evidenceKeys": ["source-2"],
                    }
                ]
            },
        },
    )
    assert "35000" in result.reply and "basic programming knowledge" in result.reply
    assert "weekdays" not in result.reply
    assert any(s.document_id == "eligibility-selection" for s in result.sources)


async def test_fee_completeness_does_not_use_a_different_courses_record(agent_env):
    result = await agent_env.send(
        "For PY101, what does it cost?",
        steps=[[("getCourseDetails", {"courseID": "PY202"})]],
        selection={
            "kind": "evidence",
            "evidenceKeys": ["tool-1"],
            "clarification": "none",
            "courseUse": "none",
        },
    )
    assert "35000" not in result.reply and "Advanced Python costs" not in result.reply


@pytest.mark.parametrize(
    "selected, text",
    [
        (False, "Python Basics has no prerequisites."),
        (True, "Advanced Python requires experience. No alternative is documented."),
    ],
)
def test_beginner_completion_never_uses_unselected_or_undocumented_alternatives(selected, text):
    source = SourceRef(
        business_id="training-institute",
        knowledge_base_id="academy-documents",
        document_id="eligibility",
        document_version=1,
        title="Eligibility",
        chunk_id="chunk-1",
        excerpt=text,
    )
    work = TurnWork(evidence=[Evidence(key="source-1", text=text, source=source)])
    passages = {"source-1": ["Selected prerequisite."]} if selected else {}
    assert not include_beginner_alternative(
        passages, work, "Having never programmed, is an advanced course a sensible start?"
    )
    assert not any("Python Basics" in p for quotes in passages.values() for p in quotes)
