"""International normalization and complete-message evidence across state and real writes."""

import pytest
from pydantic import ValidationError

from app.contracts import CustomerChanges, CustomerEvidence, LeadInput, StateProposal
from app.customer_values import contact_line, supports_value
from app.gateway import GatewayFailure
from app.gemini import ProviderFailure
from app.generation import EvidencedProposal, GenerationService
from app.phones import normalize_phone
from app.state import StateFailure, StateService
from app.tools import ToolCall
from tests.test_agent import agent_env as agent_env
from tests.test_agent import change, prepare_enquiry_offer
from tests.test_generation import client_for, structured
from tests.test_state import next_turn, proposal
from tests.test_state import state_env as state_env


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("98765-43210", "+919876543210"),
        ("+91 9876543210", "+919876543210"),
        ("6000000000", "+916000000000"),
        ("+1 (202) 555-0199", "+12025550199"),
        ("+1.202.555.0199", "+12025550199"),
        ("+44 20 8366 1177", "+442083661177"),
        ("+61 412 345 678", "+61412345678"),
    ],
)
def test_all_entry_points_share_canonical_format(raw, expected):
    assert normalize_phone(raw) == expected
    assert LeadInput(name="Alice Morgan", phone=raw, course_id="DS202").phone == expected
    assert supports_value("phone", expected, f"My phone is {raw}.")
    contacts = contact_line(f"Alice Morgan {raw}")
    assert contacts is not None and normalize_phone(contacts[1]) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "123",
        "+999123456789",
        "2025550199",
        "02083661177",
        "+12001230101",
        "+120255501990000000",
        "+12025550199 ext 4",
        "+1 (202 555-0199",
        "+1 202) 555-0199",
        "+1 ((202)) 555-0199",
        "1-800-FLOWERS",
        "00 44 20 8366 1177",
    ],
)
def test_invalid_or_ambiguous_phone_does_not_qualify(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)
    with pytest.raises(ValidationError):
        LeadInput(name="Alice Morgan", phone=raw, course_id="DS202")
    assert contact_line(f"Alice Morgan {raw}") is None


@pytest.mark.parametrize(
    "text",
    [
        "+44919876543210",
        "+1 9876543210",
        "9876543210999",
        "abc9876543210xyz",
    ],
)
def test_invalid_prefix_or_longer_number_cannot_support_indian_suffix(text):
    assert not supports_value("phone", "+919876543210", text)


async def test_model_excerpt_cannot_hide_an_invalid_country_prefix():
    client = client_for(
        structured(
            {
                "changes": [
                    {
                        "field": "phone",
                        "operation": "set",
                        "value": "+919876543210",
                        "excerpt": "9876543210",
                    }
                ]
            }
        )
    )
    async with client:
        with pytest.raises(ProviderFailure) as error:
            await GenerationService(client).propose_state("turn-1", "My number is +44919876543210.")
    assert error.value.code == "INVALID_PROVENANCE"


def test_complete_run_checks_do_not_backtrack_to_a_valid_prefix():
    assert not supports_value("phone", "+12025550199", "+1 202 555 0199 123X")
    assert not supports_value("phone", "+919876543210", "+ 9876543210")
    assert supports_value("phone", "+12025550199", "Phone: +12025550199. Budget: 30000.")


async def test_state_rechecks_complete_original_phone_evidence(state_env):
    env = state_env
    context = await next_turn(env, "My phone is +44919876543210.")
    with pytest.raises(StateFailure) as error:
        await env.service.apply(
            context, proposal(context, {"phone": "+919876543210"}, excerpt="9876543210")
        )
    assert error.value.code == "INVALID_PROVENANCE"
    assert (await env.repo.session(context)).state.fields.phone is None


async def test_write_qualification_rechecks_phone_after_corrupted_provenance(state_env):
    env = state_env
    text = "Alice Morgan 9876543210 wants DS202"
    context = await next_turn(env, text)
    await env.service.apply(
        context,
        EvidencedProposal(
            proposal=StateProposal(
                set_fields=CustomerChanges(
                    name="Alice Morgan", phone="9876543210", course_id="DS202"
                )
            ),
            evidence={
                field: CustomerEvidence(turn_id=context.turn_id, excerpt=excerpt)
                for field, excerpt in (
                    ("name", "Alice Morgan"),
                    ("phone", "9876543210"),
                    ("course_id", "DS202"),
                )
            },
        ),
    )
    # Test-only corruption in an owned database; no API can edit source messages this way.
    await env.repo.db.turns.update_one(
        {"turn_id": context.turn_id},
        {"$set": {"input_text": "Alice Morgan +44919876543210 wants DS202"}},
    )
    result = await env.executor.execute(
        context,
        ToolCall(
            call_id="phone-proof",
            name="createLead",
            arguments={"name": "Alice Morgan", "phone": "+919876543210", "courseId": "DS202"},
        ),
    )
    assert result.result.status == "failure" and result.result.code == "INVALID_PROVENANCE"
    assert not result.handler_invoked
    assert await env.repo.db.leads.count_documents({}) == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 (202) 555-0199", "+12025550199"),
        ("+44 20 8366 1177", "+442083661177"),
        ("+61 412 345 678", "+61412345678"),
    ],
)
async def test_international_contact_saves_lead_callback_and_replays_once(agent_env, raw, expected):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send("Please call me.")
    result = await env.send(f"Alice Morgan {raw}")
    assert [a.result.tool for a in result.actions] == ["createLead", "requestCallback"]
    assert all(a.result.status == "success" for a in result.actions)
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["phone"] == expected and lead["customer"]["course_id"] == "DS202"
    assert lead["callback_request_id"]
    body = env.payloads[result.message_id]
    replay = await env.gateway.handle(env.context, body)
    assert replay.payload == result
    assert await env.storage.db.leads.count_documents({}) == 1
    assert await env.storage.db.tool_executions.count_documents({"tool": "requestCallback"}) == 1


async def test_phone_correction_uses_new_country_and_keeps_saved_snapshot(agent_env):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send("Please call me.")
    await env.send("Alice Morgan +1 (202) 555-0199")
    await env.send(
        "Actually my phone is +44 20 8366 1177.",
        changes=[change("phone", "+44 20 8366 1177", "+44 20 8366 1177")],
    )
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["phone"] == "+442083661177"
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["phone"] == "+12025550199"


async def test_invalid_phone_keeps_valid_name_and_requests_country_code(agent_env):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send("Please call me.")
    result = await env.send(
        "My name is Alice Morgan and my phone is 2025550199.",
        changes=[
            change("name", "Alice Morgan", "Alice Morgan"),
            change("phone", "2025550199", "2025550199"),
        ],
    )
    assert "country code" in result.reply and not result.actions
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["name"] == "Alice Morgan"
    assert session["state"]["fields"]["phone"] is None
    assert await env.storage.db.leads.count_documents({}) == 0
    await env.send(
        "+1 (202) 555-0199", changes=[change("phone", "+12025550199", "+1 (202) 555-0199")]
    )
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["name"] == "Alice Morgan"
    assert lead["customer"]["phone"] == "+12025550199" and lead["callback_request_id"]


async def test_unproven_phone_keeps_other_valid_facts_but_cannot_create_lead(agent_env):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send("Please call me.")
    result = await env.send(
        "Alice Morgan +44919876543210",
        changes=[
            change("name", "Alice Morgan", "Alice Morgan"),
            change("phone", "+919876543210", "9876543210"),
        ],
    )
    assert "country code" in result.reply and not result.actions
    session = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert session["state"]["fields"]["name"] == "Alice Morgan"
    assert session["state"]["fields"]["phone"] is None
    assert await env.storage.db.leads.count_documents({}) == 0


@pytest.mark.parametrize("course_hint", [False, True])
async def test_invalid_correction_does_not_use_previously_known_phone(agent_env, course_hint):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send(
        "My name is Alice Morgan and my phone is +12025550199.",
        changes=[
            change("name", "Alice Morgan", "Alice Morgan"),
            change("phone", "+12025550199", "+12025550199"),
        ],
    )
    assert await env.storage.db.leads.count_documents({}) == 0
    changes = [change("phone", "2025550199", "2025550199")]
    if course_hint:
        changes.append(change("course_id", "DS202", "DS202"))
    result = await env.send("DS202: My phone is 2025550199. Please call me.", changes=changes)
    assert "country code" in result.reply and not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0
    await env.send(
        "My phone is +442083661177.", changes=[change("phone", "+442083661177", "+442083661177")]
    )
    lead = await env.storage.db.leads.find_one({})
    assert lead["customer"]["phone"] == "+442083661177" and lead["callback_request_id"]


async def test_rejected_phone_diagnostic_survives_a_safe_retry(agent_env, monkeypatch):
    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send("Please call me.")
    original = StateService.apply
    first = True

    async def interrupt(service, context, patch):
        nonlocal first
        saved = await original(service, context, patch)
        if first and patch.phone_rejected:
            first = False
            raise ProviderFailure("UNAVAILABLE", True)
        return saved

    monkeypatch.setattr(StateService, "apply", interrupt)
    with pytest.raises(GatewayFailure):
        await env.send(
            "My name is Alice Morgan and my phone is 2025550199.",
            changes=[
                change("name", "Alice Morgan", "Alice Morgan"),
                change("phone", "2025550199", "2025550199"),
            ],
        )
    body = list(env.payloads.values())[-1]
    recovered = await env.gateway.handle(env.context, body)
    assert "country code" in recovered.payload.reply and not recovered.payload.actions
    assert await env.storage.db.leads.count_documents({}) == 0
    saved = await env.storage.db.sessions.find_one({"session_id": env.context.session_id})
    assert saved["state"]["fields"]["name"] == "Alice Morgan"


async def test_repair_cannot_drop_invalid_correction_and_reuse_old_phone(agent_env, monkeypatch):
    from app.generation import ProposedChange, StateDraft

    env = agent_env
    await prepare_enquiry_offer(env)
    await env.send(
        "Alice Morgan +12025550199",
        changes=[
            change("name", "Alice Morgan", "Alice Morgan"),
            change("phone", "+12025550199", "+12025550199"),
        ],
    )
    original = env.engine.generation._structured
    drafts = 0

    async def repair(model, system, data, **kwargs):
        nonlocal drafts
        if model is StateDraft:
            drafts += 1
            return StateDraft(
                changes=(
                    ProposedChange(
                        field="phone", operation="set", value="2025550199", excerpt="2025550199"
                    ),
                )
                if drafts == 1
                else ()
            )
        return await original(model, system, data, **kwargs)

    monkeypatch.setattr(env.engine.generation, "_structured", repair)
    result = await env.send("Actually my phone is 2025550199. Please call me.")
    assert drafts == 2 and "country code" in result.reply and not result.actions
    assert await env.storage.db.leads.count_documents({}) == 0
