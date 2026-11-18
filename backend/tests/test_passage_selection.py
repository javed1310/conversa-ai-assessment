import pytest

from app.facts import policy_reply, verified_policy_quote
from app.knowledge import DocumentInput, KnowledgeService
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
