"""Drafts are optional, evidence-bounded, and independently reviewed before display."""

import json

import httpx
import pytest

from app.agent_context import TurnWork
from app.contracts import ChatResult
from app.gemini import GeminiClient
from app.generation import GenerationService, ReplySelection
from app.reply_composer import ReplyComposer, numbers
from tests.test_agent import agent_env as agent_env
from tests.test_agent import response
from tests.test_generation import configuration, structured


def selection(text, keys=("source-1",)):
    return ReplySelection.model_validate(
        {
            "kind": "evidence",
            "evidenceKeys": ["source-1"],
            "clarification": "none",
            "conversationalDraft": {"sentences": [{"text": text, "evidenceKeys": list(keys)}]},
        }
    )


@pytest.mark.parametrize(
    "rejected", [None, "grounded", "relevant", "preservesConditions", "noActionClaims"]
)
async def test_each_review_condition_controls_display(rejected):
    requests = []
    verdict = dict.fromkeys(["grounded", "relevant", "preservesConditions", "noActionClaims"], True)
    if rejected:
        verdict[rejected] = False

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=structured(verdict))

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as provider:
        composer = ReplyComposer(GenerationService(provider))
        fallback = ChatResult(message_id="m1", session_id="s1", reply="Verified policy fallback")
        work = TurnWork(
            response_evidence={"source-1": "Refund within seven days AND no class attended."}
        )
        draft = selection(
            "You can request a refund within 7 days, provided you haven't attended the first class."
        )
        result = await composer.compose(fallback, draft, work, "Can I get a refund?")
        assert result.reply == (
            draft.conversational_draft.sentences[0].text if rejected is None else fallback.reply
        )
        assert result.sources == fallback.sources and result.actions == fallback.actions
        assert len(requests) == 1
        body = json.loads(requests[0]["contents"][0]["parts"][0]["text"])
        assert [item["key"] for item in body["evidence"]] == ["source-1"]


@pytest.mark.parametrize(
    "text,keys",
    [
        ("The fee is INR 99.", ("source-1",)),
        ("Your callback is scheduled.", ("source-1",)),
        ("The policy is on https://invented.invalid.", ("source-1",)),
        ("Ask about ZZ999.", ("source-1",)),
        ("A supported answer.", ("foreign-source",)),
        ("# Policy heading", ("source-1",)),
        ("This assessment has no enrollment integration.", ("source-1",)),
        ("This prototype cannot enroll you.", ("source-1",)),
        ("A supported answer.", ("source-1", "source-1")),
    ],
)
async def test_invalid_reference_or_fact_never_reaches_review(text, keys):
    def unexpected(request):
        raise AssertionError("Unsafe draft reached the provider")

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
    ) as provider:
        fallback = ChatResult(message_id="m1", session_id="s1", reply="Verified fallback")
        result = await ReplyComposer(GenerationService(provider)).compose(
            fallback,
            selection(text, keys),
            TurnWork(response_evidence={"source-1": "Fee INR 12000."}),
            "Question",
        )
        assert result == fallback


async def test_review_outage_preserves_successful_checked_answer():
    async with GeminiClient(
        configuration(provider_attempts=1),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))),
    ) as provider:
        fallback = ChatResult(message_id="m1", session_id="s1", reply="Fee INR 12000.")
        result = await ReplyComposer(GenerationService(provider)).compose(
            fallback,
            selection("It costs INR 12,000."),
            TurnWork(response_evidence={"source-1": "Fee INR 12000."}),
            "What is the fee?",
        )
        assert result == fallback


@pytest.mark.parametrize("candidate", [{"sentences": []}, {"sentences": [{"text": ""}]}, "bad"])
def test_malformed_optional_draft_keeps_valid_selection(candidate):
    result = ReplySelection.model_validate(
        {
            "kind": "evidence",
            "evidenceKeys": [],
            "clarification": "none",
            "conversationalDraft": candidate,
        }
    )
    assert result.kind == "evidence" and result.conversational_draft is None


async def test_real_engine_uses_reviewed_policy_answer_and_preserves_source(agent_env, monkeypatch):
    env = agent_env
    original = env.model.handle

    def handle(request):
        body = json.loads(request.content)
        if body["generationConfig"].get("responseJsonSchema", {}).get("title") == "ReplyReview":
            return httpx.Response(
                200,
                json=response(
                    [
                        {
                            "text": json.dumps(
                                {
                                    "grounded": True,
                                    "relevant": True,
                                    "preservesConditions": True,
                                    "noActionClaims": True,
                                }
                            )
                        }
                    ]
                ),
            )
        return original(request)

    chosen = {
        "kind": "evidence",
        "evidenceKeys": ["source-1"],
        "clarification": "none",
        "policyPassages": [
            {
                "evidenceKey": "source-1",
                "quote": "Refund requires a request within seven days "
                "AND no first class attendance.",
            }
        ],
        "conversationalDraft": {
            "sentences": [
                {
                    "text": "You can request a refund within seven days, "
                    "as long as you haven't attended the first class.",
                    "evidenceKeys": ["source-1"],
                }
            ]
        },
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(env.engine.provider, "_client", client)
        result = await env.send(
            "If my plans change, can I get a refund?",
            steps=[[("readKnowledge", {"query": "refund"})]],
            selection=chosen,
        )
    assert result.reply.startswith("You can request")
    assert len(result.sources) == 1 and not result.actions


def test_numeric_normalization_handles_formatting_and_small_number_words():
    assert numbers("INR 12,000.0 within seven days") == {"12000", "7"}
