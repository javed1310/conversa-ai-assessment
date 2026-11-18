"""M07 uses intercepted HTTP only. Real capability checks are an explicit CLI."""

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.contracts import CourseDetailsArguments, LeadInput
from app.gemini import FunctionSpec, GeminiClient, ProviderFailure, ToolOutput
from app.generation import EvidenceItem, GenerationService
from app.main import create_app
from app.settings import Settings

SECRET = "synthetic-key-not-real"
TOOL = FunctionSpec("getCourseDetails", "Read one course", CourseDetailsArguments)


def configuration(**updates):
    return Settings(_env_file=None, gemini_api_key=SECRET, **updates)


def envelope(parts, reason="STOP"):
    return {"candidates": [{"finishReason": reason, "content": {"role": "model", "parts": parts}}]}


def structured(data):
    return envelope([{"text": json.dumps(data)}])


def client_for(body):
    return GeminiClient(
        configuration(),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        ),
    )


async def test_lifecycle_and_request_privacy():
    requests = []

    def handler(request):
        assert request.headers["x-goog-api-key"] == SECRET
        assert SECRET not in str(request.url) and "authorization" not in request.headers
        assert str(request.url).endswith("gemini-3.5-flash-lite:generateContent")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope([{"text": "Hello"}]))

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with GeminiClient(configuration(), http_client=transport_client) as provider:
        result = await provider.start("Trusted system", "Hi", ())
        assert result.turn.text == "Hello"
    assert transport_client.is_closed
    await provider.close()
    with pytest.raises(ProviderFailure, match="Generation operation unavailable") as error:
        await provider.start("system", "hi", ())
    assert error.value.code == "CLIENT_CLOSED" and SECRET not in str(error.value)
    assert requests[0]["systemInstruction"] == {"parts": [{"text": "Trusted system"}]}


async def test_application_owns_provider_without_live_calls():
    app = create_app(Settings(_env_file=None))
    async with LifespanManager(app):
        provider = app.state.provider
        assert isinstance(provider, GeminiClient) and not provider._client.is_closed
    assert provider._client.is_closed and app.state.provider is None


@pytest.mark.parametrize("provider_id", [None, "provider-call-1"])
async def test_call_continuation_preserves_all_parts_and_matches_ids(provider_id):
    call = {"name": "getCourseDetails", "args": {"courseID": "DS202"}}
    if provider_id is not None:
        call["id"] = provider_id
    parts = [
        {"text": "private thought", "thought": True, "thoughtSignature": "opaque-first"},
        {"functionCall": call, "thoughtSignature": "opaque-call"},
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        body = envelope(parts) if len(requests) == 1 else envelope([{"text": "Result received"}])
        return httpx.Response(200, json=body)

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as provider:
        convo = await provider.start("system", "DS202 details", (TOOL,))
        assert "private thought" not in convo.turn.text
        parsed = convo.turn.calls[0]
        assert parsed.arguments == {"courseID": "DS202"}
        output = ToolOutput(parsed.call_id, json.dumps({"courseID": "DS202", "fee": 25000}))
        continued = await provider.continue_with(convo, (output,), system="system", tools=(TOOL,))
        assert continued.turn.text == "Result received" and continued.rounds == 2
    assert requests[1]["contents"][1] == {"role": "model", "parts": parts}
    response = requests[1]["contents"][2]["parts"][0]["functionResponse"]
    assert response["name"] == "getCourseDetails"
    assert response.get("id") == provider_id
    assert response["response"]["fee"] == 25000


@pytest.mark.parametrize(
    "body,code",
    [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "REFUSED"),
        (envelope([], "MAX_TOKENS"), "INCOMPLETE_OUTPUT"),
        (envelope([], "SAFETY"), "REFUSED"),
        ({"candidates": []}, "INVALID_RESPONSE"),
        ({"candidates": [None]}, "INVALID_RESPONSE"),
        (envelope([{"text": " "}]), "EMPTY_OUTPUT"),
        (envelope([{"inlineData": {}}]), "UNEXPECTED_PART"),
        (envelope([{"text": "x", "functionCall": {}}]), "INVALID_RESPONSE"),
        (envelope([{"text": "x", "thoughtSignature": 3}]), "INVALID_RESPONSE"),
        (envelope([{"functionCall": {"name": "shell", "args": {}}}]), "INVALID_TOOL_CALL"),
        (
            envelope([{"functionCall": {"name": "getCourseDetails", "args": "{}"}}]),
            "INVALID_TOOL_CALL",
        ),
        (
            envelope([{"functionCall": {"name": "getCourseDetails", "args": {"businessId": "x"}}}]),
            "INVALID_TOOL_ARGUMENTS",
        ),
    ],
)
async def test_invalid_model_response_never_executes_tools(body, code):
    async with client_for(body) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("system", "question", (TOOL,))
        assert error.value.code == code


@pytest.mark.parametrize(
    "status,expected,retries",
    [
        (401, "ACCESS_DENIED", 1),
        (403, "ACCESS_DENIED", 1),
        (404, "MODEL_UNAVAILABLE", 1),
        (400, "INVALID_REQUEST", 1),
        (307, "INVALID_REQUEST", 1),
        (429, "RATE_LIMIT", 2),
        (503, "UNAVAILABLE", 2),
    ],
)
async def test_http_failures_safe_and_bounded(status, expected, retries):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        return httpx.Response(status, text="private provider details " + SECRET)

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("system", "hi", ())
        assert error.value.code == expected and count == retries
        assert SECRET not in str(error.value) and "private" not in str(error.value)


async def test_timeout_and_cancellation():
    count = 0

    async def timeout(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout(SECRET, request=request)

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    ) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("s", "hi", ())
        assert error.value.code == "TIMEOUT" and count == 2

    async def cancel(_):
        raise asyncio.CancelledError()

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(cancel))
    ) as provider:
        with pytest.raises(asyncio.CancelledError):
            await provider.start("s", "hi", ())


async def test_missing_key_no_request():
    async with GeminiClient(Settings(_env_file=None)) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("system", "hi", ())
        assert error.value.code == "MISSING_ACCESS"


async def test_output_mismatch_and_round_budget():
    body = envelope([{"functionCall": {"name": "getCourseDetails", "args": {"courseID": "DS202"}}}])
    async with client_for(body) as provider:
        convo = await provider.start("system", "details", (TOOL,))
        with pytest.raises(ProviderFailure) as error:
            await provider.continue_with(convo, (), system="s", tools=(TOOL,))
        assert error.value.code == "OUTPUT_MISMATCH"
        provider.settings = configuration(agent_max_rounds=1)
        with pytest.raises(ProviderFailure) as error:
            await provider.continue_with(convo, (), system="s", tools=(TOOL,))
        assert error.value.code == "ROUND_BUDGET_EXCEEDED"


async def test_customer_provenance_server_owned_turn_and_phone_normalization():
    draft = {
        "changes": [
            {"field": "name", "operation": "set", "value": "Aarav", "excerpt": "I am Aarav"},
            {"field": "phone", "operation": "set", "value": "9876543210", "excerpt": "9876543210"},
            {"field": "budget", "operation": "set", "value": "20000", "excerpt": "budget 20000"},
        ]
    }
    async with client_for(structured(draft)) as provider:
        result = await GenerationService(provider).propose_state(
            "actual-turn-1", "I am Aarav, phone 9876543210, budget 20000"
        )
    assert result.proposal.set_fields.phone == "+919876543210"
    assert result.proposal.set_fields.budget == 20000
    assert {item.turn_id for item in result.evidence.values()} == {"actual-turn-1"}


@pytest.mark.parametrize(
    "change,code",
    [
        (
            {"field": "name", "operation": "set", "value": "Other", "excerpt": "Hi"},
            "INVALID_PROVENANCE",
        ),
        (
            {"field": "name", "operation": "set", "value": "Other", "excerpt": "Other"},
            "INVALID_PROVENANCE",
        ),
        (
            {"field": "phone", "operation": "set", "value": "123", "excerpt": "123"},
            "INVALID_STRUCTURED_OUTPUT",
        ),
        (
            {"field": "name", "operation": "set", "value": None, "excerpt": "Hi"},
            "INVALID_STRUCTURED_OUTPUT",
        ),
    ],
)
async def test_state_invalid_values_and_provenance(change, code):
    async with client_for(structured({"changes": [change]})) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).propose_state("turn-1", "Hi 123")
        assert error.value.code == code


async def test_structured_schema_and_safe_invalid_reference():
    body = structured(
        {"message": "Unavailable", "claims": [{"text": "Invented", "evidenceKeys": ["fake"]}]}
    )
    async with client_for(body) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).answer(
                "refund?", (EvidenceItem(key="real", text="policy"),)
            )
        assert error.value.code == "INVALID_REFERENCE"


@pytest.mark.parametrize(
    "text", ["not JSON", '{"message": "x"}', '{"message":"x","claims":[],"leadId":"fake"}']
)
async def test_invalid_structured_json(text):
    async with client_for(envelope([{"text": text}])) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).answer("hello", ())
        assert error.value.code == "INVALID_STRUCTURED_OUTPUT"


async def test_summary_identity_is_server_owned_and_uses_summary_model():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=structured(
                {
                    "summary": "Customer wants Python.",
                    "intent": "Python enquiry",
                    "recommendedAction": "Counselor follow-up",
                }
            ),
        )

    async with GeminiClient(
        configuration(gemini_summary_model="gemini-summary-test"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as provider:
        result = await GenerationService(provider).summarize(
            lead_id="actual-lead",
            customer=LeadInput(name="Aarav", phone="9876543210", stated_interest="Python"),
            history=("I want Python",),
        )
    assert result.lead_id == "actual-lead" and result.customer_name == "Aarav"
    assert result.qualification_status == "qualified"
    assert str(requests[0].url).endswith("gemini-summary-test:generateContent")
    config = json.loads(requests[0].content)["generationConfig"]
    assert config["responseMimeType"] == "application/json" and "responseJsonSchema" in config


async def test_retry_recovers_once_without_replaying_business_operations():
    attempts = []

    def handler(request):
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            return httpx.Response(503, text=SECRET)
        return httpx.Response(200, json=envelope([{"text": "Recovered"}]))

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as provider:
        result = await provider.start("system", "hi", ())
        assert result.turn.text == "Recovered"
    assert len(attempts) == 2 and attempts[0] == attempts[1]


async def test_tool_budget_spans_continuation_rounds():
    body = envelope([{"functionCall": {"name": "getCourseDetails", "args": {"courseID": "DS202"}}}])
    async with client_for(body) as provider:
        provider.settings = configuration(agent_max_tool_calls=1)
        convo = await provider.start("s", "details", (TOOL,))
        with pytest.raises(ProviderFailure) as error:
            await provider.continue_with(
                convo, (ToolOutput(convo.turn.calls[0].call_id, "{}"),), system="s", tools=(TOOL,)
            )
        assert error.value.code == "TOOL_BUDGET_EXCEEDED"


async def test_parallel_results_reordered_to_original_calls():
    parts = [
        {"functionCall": {"id": "one", "name": "getCourseDetails", "args": {"courseID": "DS202"}}},
        {"functionCall": {"id": "two", "name": "getCourseDetails", "args": {"courseID": "PY101"}}},
    ]
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, json=envelope(parts if len(captured) == 1 else [{"text": "Done"}])
        )

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as provider:
        convo = await provider.start("s", "details", (TOOL,))
        await provider.continue_with(
            convo,
            (ToolOutput("two", '{"value":2}'), ToolOutput("one", '{"value":1}')),
            system="s",
            tools=(TOOL,),
        )
    parts = captured[1]["contents"][-1]["parts"]
    assert [p["functionResponse"]["id"] for p in parts] == ["one", "two"]
    assert [p["functionResponse"]["response"]["value"] for p in parts] == [1, 2]


async def test_duplicate_provider_call_ids_rejected():
    part = {
        "functionCall": {"id": "same", "name": "getCourseDetails", "args": {"courseID": "DS202"}}
    }
    async with client_for(envelope([part, part])) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("s", "details", (TOOL,))
        assert error.value.code == "INVALID_TOOL_CALL"


@pytest.mark.parametrize(
    "bad_body",
    [[], None, {"error": SECRET}, {"promptFeedback": []}, envelope([{"text": "hi"}], reason=[])],
)
async def test_malformed_envelopes_are_normalized(bad_body):
    async with client_for(bad_body) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("s", "hi", ())
        assert error.value.code == "INVALID_RESPONSE"


async def test_bounded_inputs_reject_before_http():
    async with client_for(envelope([{"text": "should not be returned"}])) as provider:
        with pytest.raises(ProviderFailure) as error:
            await provider.start("s" * 131073, "hi", ())
        assert error.value.code == "INPUT_TOO_LARGE"
        with pytest.raises(ProviderFailure) as error:
            await provider.start("s", "x" * 8001, ())
        assert error.value.code == "INVALID_INPUT"


async def test_clear_is_explicit_and_duplicate_updates_fail():
    change = {"field": "phone", "operation": "clear", "value": None, "excerpt": "Remove my phone"}
    async with client_for(structured({"changes": [change]})) as provider:
        result = await GenerationService(provider).propose_state("turn-1", "Remove my phone")
        assert result.proposal.clear_fields == ("phone",)
        assert not result.proposal.set_fields.model_fields_set
    async with client_for(structured({"changes": [change, change]})) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).propose_state("turn-1", "Remove my phone")
        assert error.value.code == "INVALID_PROVENANCE"


async def test_unrelated_text_cannot_supply_provenance():
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=structured(
                {
                    "changes": [
                        {
                            "field": "phone",
                            "operation": "set",
                            "value": "9876543210",
                            "excerpt": "9876543210",
                        }
                    ]
                }
            ),
        )

    async with GeminiClient(
        configuration(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).propose_state("real-turn", "Tell me about courses")
        assert error.value.code == "INVALID_PROVENANCE"
    data = json.loads(observed[0]["contents"][0]["parts"][0]["text"])
    assert data == {"customerText": "Tell me about courses", "selectedCourseId": None}


async def test_valid_draft_with_registered_evidence_and_no_action_metadata():
    data = {
        "message": "Seven days and no class attendance.",
        "claims": [{"text": "Both conditions apply.", "evidenceKeys": ["policy"]}],
    }
    async with client_for(structured(data)) as provider:
        result = await GenerationService(provider).answer(
            "refund?", (EvidenceItem(key="policy", text="Both conditions apply."),)
        )
    assert result.claims[0].evidence_keys == ("policy",)
    assert set(result.model_dump()) == {"message", "claims"}


async def test_empty_stop_is_only_allowed_as_explicit_planner_completion():
    body = {"candidates": [{"finishReason": "STOP", "content": {}}]}
    async with client_for(body) as provider:
        with pytest.raises(ProviderFailure):
            await provider.start("s", "hello", ())
        turn = await provider.generate(
            system="planner",
            contents=[{"role": "user", "parts": [{"text": "tool results"}]}],
            allow_empty=True,
        )
        assert turn.text == "" and turn.calls == ()


async def test_counselor_plan_has_only_bounded_options_and_customer_quotes():
    plan = {
        "intent": "course_enquiry",
        "recommendedAction": "counselor_follow_up",
        "customerQuotes": ["I want Python"],
    }
    async with client_for(structured(plan)) as provider:
        result = await GenerationService(provider).plan_summary(
            {"customerHistory": ["I want Python"]}
        )
    assert result.customer_quotes == ("I want Python",)
    assert set(result.model_dump()) == {"intent", "recommended_action", "customer_quotes"}


async def test_counselor_plan_rejects_model_owned_identity_or_action_claims():
    plan = {
        "intent": "course_enquiry",
        "recommendedAction": "counselor_follow_up",
        "customerQuotes": [],
        "leadId": "invented",
        "callbackCompleted": True,
    }
    async with client_for(structured(plan)) as provider:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(provider).plan_summary({"customerHistory": ["I want Python"]})
        assert error.value.code == "INVALID_STRUCTURED_OUTPUT"
