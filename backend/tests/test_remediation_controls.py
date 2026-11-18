from dataclasses import replace

import httpx
import pytest

from app.facts import contains_action_status_claim
from app.gemini import GeminiClient, ProviderFailure
from app.generation import GenerationService
from app.intent import callback_intent
from app.knowledge import DocumentInput, KnowledgeService
from app.tools import ToolRegistry
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change
from tests.test_course_followup import selected
from tests.test_generation import configuration, structured
from tests.test_knowledge import TestOnlyEmbedder


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I don't want you to call me.", "revoke"),
        ("I would rather you didn't call me.", "revoke"),
        ("I don't think you should call me.", "revoke"),
        ("Could you not call me?", "revoke"),
        ("I'd like a callback.", "request"),
        ("I want to enquire and request a callback.", "request"),
        ("Please don't forget to call me.", "request"),
        ("I don't have experience. Please call me.", "request"),
        ("I don't want to cancel my callback.", "none"),
        ("Explain the phrase 'I'd like a callback'.", "none"),
    ],
)
def test_callback_clause_meaning(text, expected):
    assert callback_intent(text) == expected


@pytest.mark.parametrize(
    "text,blocked",
    [
        ("Your callback has been scheduled successfully.", True),
        ("Your enquiry was saved.", True),
        ("We have booked your appointment.", True),
        ("I've created a lead.", True),
        ("You're now enrolled.", True),
        ("Your summary has been generated.", True),
        ("The callback is scheduled.", True),
        ("I can't book a trial visit.", False),
        ("Once a lead has been created, staff review the enquiry.", False),
        ("A refund is permitted only if both conditions hold.", False),
        ("Requests must be made within seven days before the first class.", False),
    ],
)
def test_policy_authority_distinguishes_status_from_procedure(text, blocked):
    assert contains_action_status_claim(text) is blocked


@pytest.mark.parametrize("kind,keys", [("evidence", []), ("unavailable", [])])
async def test_requested_search_survives_incomplete_final_selection(agent_env, kind, keys):
    env = agent_env
    result = await env.send(
        "Find data science courses under 30000 INR.",
        changes=[change("budget", "30000", "30000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
        selection={"kind": kind, "evidenceKeys": keys, "clarification": "none"},
    )
    assert "DS101" in result.reply and "DS202" in result.reply


async def test_corrected_empty_search_is_presented_truthfully(agent_env):
    env = agent_env
    await selected(env)
    result = await env.send(
        "Actually my budget is 20000 INR. Search again with that budget.",
        changes=[change("budget", "20000", "20000")],
        steps=[[("searchCourses", {"category": "data-science", "budget": 20000})]],
        selection={"kind": "unavailable", "evidenceKeys": [], "clarification": "none"},
    )
    assert "No courses match" in result.reply


@pytest.mark.parametrize(
    "question", ["Is an overseas job guaranteed?", "I want courses with a guaranteed job."]
)
async def test_unrelated_successful_search_does_not_reintroduce_catalog_fallback(
    agent_env, question
):
    env = agent_env
    await selected(env)
    result = await env.send(
        question,
        steps=[[("searchCourses", {"category": "data-science", "budget": 30000})]],
        selection={"kind": "evidence", "evidenceKeys": ["tool-1"], "clarification": "none"},
    )
    assert "verified information" in result.reply
    assert "DS101" not in result.reply and "28000" not in result.reply


@pytest.mark.parametrize(
    "text", ["I'd like a callback.", "I want to enquire and request a callback."]
)
async def test_positive_callback_paraphrase_authorizes_actual_owned_record(agent_env, text):
    env = agent_env
    await env.send(
        "My name is Rahul, phone 9876543210. I want PY101. Save my enquiry.",
        changes=[
            change("name", "Rahul", "Rahul"),
            change("phone", "9876543210", "9876543210"),
            change("course_id", "PY101", "PY101"),
        ],
        steps=[[("createLead", {"name": "Rahul", "phone": "9876543210", "courseId": "PY101"})]],
    )
    lead = await env.storage.db.leads.find_one({})
    reply = await env.send(text, steps=[[("requestCallback", {"leadID": lead["lead_id"]})]])
    saved = await env.storage.db.leads.find_one({})
    assert saved["callback_request_id"]
    assert "A callback is requested" in reply.reply


def state_change(excerpt="Rahul", value="Rahul"):
    return {"field": "name", "operation": "set", "value": value, "excerpt": excerpt}


async def propose_with_responses(bodies, text="My name is Rahul."):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=structured(bodies[min(len(calls) - 1, len(bodies) - 1)]))

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as provider:
        result = await GenerationService(provider).propose_state("actual-turn", text)
    return result, calls


@pytest.mark.parametrize(
    "key,value",
    [
        ("socialReply", {"kind": "thanks", "excerpt": ""}),
        ("courseQuestion", {"courseId": "", "fields": ["fee"], "excerpt": ""}),
        ("enquiryRequest", {"courseId": "", "excerpt": ""}),
    ],
)
async def test_invalid_optional_cue_cannot_discard_valid_customer_facts(key, value):
    result, calls = await propose_with_responses([{"changes": [state_change()], key: value}])
    assert result.proposal.set_fields.name == "Rahul" and len(calls) == 1
    assert result.social_reply is None and result.course_question is None
    assert result.enquiry_request is None


async def test_invalid_essential_state_schema_gets_one_repair_before_provenance_validation():
    result, calls = await propose_with_responses(
        [
            {"changes": [state_change(excerpt="")]},
            {"changes": [state_change()]},
        ]
    )
    assert result.proposal.set_fields.name == "Rahul" and len(calls) == 2
    assert result.evidence["name"].turn_id == "actual-turn"


async def test_repeated_bad_state_is_bounded_and_diagnostics_omit_values():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200, json=structured({"changes": [state_change("", "private-value")]})
        )

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).propose_state("actual-turn", "My name is Rahul.")
    assert len(calls) == 2 and error.value.code == "INVALID_STRUCTURED_OUTPUT"
    assert "excerpt" in error.value.detail and "private-value" not in error.value.detail


async def test_fabricated_contact_is_not_repaired_or_accepted():
    with pytest.raises(ProviderFailure) as error:
        await propose_with_responses([{"changes": [state_change("Invented", "Invented")]}])
    assert error.value.code == "INVALID_PROVENANCE"


def qualified_changes():
    return [
        change("name", "Rahul", "Rahul"),
        change("phone", "9876543210", "9876543210"),
        change("stated_interest", "data science training", "data science training"),
    ]


async def test_qualified_general_interest_callback_does_not_depend_on_tool_planner(agent_env):
    env = agent_env
    before = len(env.model.requests)
    result = await env.send(
        "My name is Rahul. My phone is 9876543210. I want data science training. "
        "Please save my enquiry and call me.",
        changes=qualified_changes(),
    )
    assert len(env.model.requests) == before + 1
    assert [a.result.tool for a in result.actions] == ["createLead", "requestCallback"]
    assert all(a.result.status == "success" for a in result.actions)
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["course_id"] is None
    assert lead["customer"]["stated_interest"] == "data science training"
    assert lead["callback_request_id"]


async def test_failed_qualified_lead_does_not_attempt_callback_without_planner(agent_env):
    env = agent_env

    async def fail_before_save(*args):
        raise RuntimeError("synthetic failure before persistence")

    registry = ToolRegistry(
        tuple(
            replace(tool, handler=fail_before_save) if tool.name == "createLead" else tool
            for tool in env.engine.tools._definitions.values()
        )
    )
    env.engine.tools = registry
    env.engine.executor.registry = registry
    result = await env.send(
        "My name is Rahul. My phone is 9876543210. I want data science training. "
        "Please save my enquiry and call me.",
        changes=qualified_changes(),
    )
    assert [a.result.tool for a in result.actions] == ["createLead"]
    assert result.actions[0].result.status == "failure"
    assert "Your enquiry is saved" not in result.reply
    assert await env.storage.db.leads.count_documents({}) == 0


async def test_callback_contact_collection_continues_without_reasking_known_details(agent_env):
    env = agent_env
    first = await env.send(
        "My name is Rahul and I want data science training. Please call me.",
        changes=[
            change("name", "Rahul", "Rahul"),
            change("stated_interest", "data science training", "data science training"),
        ],
    )
    assert "phone" in first.reply and "your name" not in first.reply and not first.actions
    final = await env.send(
        "My phone is 9876543210.", changes=[change("phone", "9876543210", "9876543210")]
    )
    assert [a.result.tool for a in final.actions] == ["createLead", "requestCallback"]
    assert "A callback is requested" in final.reply


async def test_document_selection_repairs_missing_quotes_once_without_tools():
    bodies = [
        {"kind": "evidence", "evidenceKeys": ["source-1"], "clarification": "none"},
        {
            "passages": [{"evidenceKey": "source-1", "quote": "Bring water."}],
        },
    ]
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=structured(bodies[min(len(calls) - 1, 1)]))

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as provider:
        selection = await GenerationService(provider).select_reply(
            {
                "currentCustomerText": "What should I bring?",
                "evidence": [{"key": "source-1", "text": "Bring water."}],
            }
        )
    assert len(calls) == 2 and selection.policy_passages[0].quote == "Bring water."


async def test_empty_rewritten_retrieval_retries_original_question_with_same_scope(agent_env):
    env = agent_env
    await KnowledgeService(
        env.storage, env.registry.get("training-institute"), TestOnlyEmbedder(), env.settings
    ).ingest(
        DocumentInput(
            document_id="python-prerequisite-recovery",
            title="Python prerequisite",
            format="text",
            content="Advanced Python (PY202) requires basic programming knowledge.",
        )
    )
    question = "Does Advanced Python require programming experience?"
    result = await env.send(question, steps=[[("readKnowledge", {"query": "unmatched topic"})]])
    assert "basic programming knowledge" in result.reply
    assert len(result.sources) == 1 and result.sources[0].business_id == "training-institute"
    assert result.sources[0].document_id == "python-prerequisite-recovery"
    assert env.engine.knowledge.provider.calls[-2:] == [("unmatched topic",), (question,)]


async def test_unsolicited_model_clear_cannot_erase_existing_data_or_block_valid_budget(agent_env):
    env = agent_env
    await env.send("My phone is 9876543210.", changes=[change("phone", "9876543210", "9876543210")])
    text = "My budget is 20000 INR."
    await env.send(
        text,
        changes=[
            change("budget", "20000", "20000"),
            {"field": "phone", "operation": "clear", "value": None, "excerpt": text},
        ],
    )
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["phone"] == "+919876543210"
    assert session["state"]["fields"]["budget"] == 20000
