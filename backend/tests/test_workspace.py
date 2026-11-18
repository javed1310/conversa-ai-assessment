"""Real Mongo workspace projections, pagination, ingestion and shared agent evidence."""

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

from app.automation import AutomationWorker
from app.database import open_database
from app.inspection import encode_cursor
from app.main import create_app
from app.typescript import type_expression
from tests.test_action_agent import intake
from tests.test_automation import TestPlanner
from tests.test_knowledge import TestOnlyEmbedder

pytest_plugins = ["tests.test_agent"]
BASE = "/api/v1/businesses/training-institute"


@pytest_asyncio.fixture
async def workspace(agent_env, monkeypatch):
    env = agent_env
    monkeypatch.setattr("app.main.open_database", open_database)
    settings = env.settings.model_copy(
        update={
            "mongodb_uri": f"mongodb://127.0.0.1:27017/{env.storage.db.name}",
            "automation_enabled": False,
        }
    )
    # model_copy intentionally skips validation; preserve the actual SecretStr type.
    from pydantic import SecretStr

    settings = settings.model_copy(update={"mongodb_uri": SecretStr(settings.mongodb_uri)})
    embedder = TestOnlyEmbedder()
    app = create_app(settings, engine=env.engine, embedding_provider=embedder)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://workspace-test"
        ) as client:
            yield client, env, embedder, settings


def document(identifier="new-policy", version=1):
    return {
        "documentId": identifier,
        "version": version,
        "title": "Python library policy",
        "format": "markdown",
        "content": "Python library access is available on Tuesdays.",
    }


def test_schema_exporter_preserves_unions_and_required_fields():
    assert (
        type_expression({"type": "object", "additionalProperties": False})
        == "Record<string, never>"
    )
    assert type_expression({"anyOf": [{"type": "string"}, {"type": "null"}]}) == "(string | null)"
    assert (
        type_expression(
            {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        )
        == '{ "x": number }'
    )
    with pytest.raises(ValueError):
        type_expression({"type": "unsupported"})


@pytest.mark.integration
class TestWorkspace:
    async def test_conversation_pagination_and_private_field_redaction(self, workspace):
        client, env, _, _ = workspace
        for _ in range(3):
            await env.gateway.start_web_session("training-institute")
        first = await client.get(BASE + "/conversations?limit=2")
        assert first.status_code == 200 and first.headers["Cache-Control"] == "no-store"
        data = first.json()
        assert len(data["items"]) == 2 and data["nextCursor"]
        second = await client.get(
            BASE + "/conversations", params={"limit": 2, "cursor": data["nextCursor"]}
        )
        ids = [item["sessionId"] for item in data["items"] + second.json()["items"]]
        assert len(ids) == 4 and len(set(ids)) == 4 and ids == sorted(ids)
        assert second.json()["nextCursor"] is None
        assert not any(
            word in first.text
            for word in ("access_hash", "pending_token", "stateSteps", "senderId")
        )
        detail = await client.get(BASE + f"/conversations/{ids[0]}")
        assert detail.json() == data["items"][0]

    @pytest.mark.parametrize(
        "query", ["limit=0", "limit=51", "limit=x", "cursor=not-base64", "cursor=" + "x" * 1025]
    )
    async def test_bad_page_inputs_fail_safely(self, workspace, query):
        client, _, _, _ = workspace
        assert (await client.get(BASE + "/conversations?" + query)).status_code == 400

    async def test_cursor_cannot_cross_business_resource_or_history(self, workspace):
        client, env, _, _ = workspace
        cursor = encode_cursor("training-institute", "conversations", env.context.session_id)
        for path in [
            "/api/v1/businesses/fitness-studio/conversations",
            BASE + "/leads",
            BASE + "/documents",
        ]:
            assert (await client.get(path, params={"cursor": cursor})).status_code == 400
        foreign = encode_cursor("training-institute", "turns:foreign-session", "sequence", 1)
        assert (
            await client.get(
                BASE + f"/conversations/{env.context.session_id}/turns", params={"cursor": foreign}
            )
        ).status_code == 400

    async def test_history_order_statuses_and_no_internal_tokens(self, workspace):
        client, env, _, _ = workspace
        await env.send("Hello")
        await env.send("Second message")
        base = BASE + f"/conversations/{env.context.session_id}/turns"
        first = (await client.get(base + "?limit=1")).json()
        assert first["items"][0]["sequence"] == 1
        second = (await client.get(base, params={"cursor": first["nextCursor"]})).json()
        assert second["items"][0]["sequence"] == 2 and second["nextCursor"] is None
        assert first["items"][0]["output"]["status"] == "completed"
        assert "processingToken" not in str(first) and "stateCheckpoints" not in str(first)
        await env.storage.db.turns.update_one(
            {"sequence": 2}, {"$set": {"status": "failed", "output": None}}
        )
        assert (await client.get(base)).json()["items"][1]["status"] == "failed"

    async def test_lead_pending_completed_summary_and_notification_match_storage(self, workspace):
        client, env, _, settings = workspace
        await intake(env)
        page = (await client.get(BASE + "/leads")).json()
        assert len(page["items"]) == 1
        lead = page["items"][0]
        assert lead["customer"]["phone"] == "+919876543210" and lead["callbackRequestId"]
        assert lead["automation"]["status"] == "pending" and lead["automation"]["summary"] is None
        assert lead["notificationCreatedAt"] is None
        assert not any(
            word in str(page)
            for word in ("automationInput", "claimToken", "leaseUntil", "automationCallbackIntent")
        )
        worker = AutomationWorker(env.storage, env.registry, TestPlanner(), settings)
        await worker.tick()
        detail = (await client.get(BASE + f"/leads/{lead['leadId']}")).json()
        assert detail["automation"]["status"] == "completed" and detail["notificationCreatedAt"]
        assert detail["automation"]["summary"]["leadId"] == lead["leadId"]
        stored = await env.storage.db.leads.find_one({"lead_id": lead["leadId"]})
        assert (
            detail["automation"]["summary"]["summary"] == stored["automation"]["summary"]["summary"]
        )

    async def test_upload_ready_detail_and_agent_retrieval(self, workspace):
        client, env, _, _ = workspace
        body = document()
        response = await client.post(BASE + "/documents", json=body)
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert response.json()["content"] == body["content"]
        assert not any(
            word in response.text
            for word in ("ingestionToken", "leaseUntil", "pipelineId", "vector")
        )
        detail = await client.get(BASE + "/documents/new-policy/versions/1")
        assert detail.json() == response.json()
        result = await env.send(
            "Python library access?", steps=[[("readKnowledge", {"query": "Python library"})]]
        )
        assert any(source.document_id == "new-policy" for source in result.sources)
        assert "Tuesdays" in result.reply
        repeat = await client.post(BASE + "/documents", json=body)
        assert repeat.json() == response.json()
        conflict = await client.post(BASE + "/documents", json=body | {"content": "Changed"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "DOCUMENT_VERSION_CONFLICT"

    async def test_failed_ingestion_visible_and_same_body_retry(self, workspace):
        client, _, embedder, _ = workspace
        embedder.fail_after = 0
        response = await client.post(BASE + "/documents", json=document())
        assert response.status_code == 503 and "KNOWLEDGE_UNAVAILABLE" in response.text
        failed = (await client.get(BASE + "/documents/new-policy/versions/1")).json()
        assert failed["status"] == "failed" and failed["failureCode"]
        embedder.fail_after = None
        retry = await client.post(BASE + "/documents", json=document())
        assert retry.status_code == 200 and retry.json()["status"] == "ready"

    async def test_document_version_pages(self, workspace):
        client, _, _, _ = workspace
        for version in (1, 2, 3):
            assert (
                await client.post(BASE + "/documents", json=document(version=version))
            ).status_code == 200
        cursor, found = None, []
        while True:
            params = {"limit": 1} | ({"cursor": cursor} if cursor else {})
            page = (await client.get(BASE + "/documents", params=params)).json()
            found.extend((d["documentId"], d["version"]) for d in page["items"])
            cursor = page["nextCursor"]
            if not cursor:
                break
        assert found == sorted(set(found)) and ("new-policy", 3) in found

    async def test_wrong_business_ids_and_unknown_business_fail(self, workspace):
        client, env, _, _ = workspace
        await intake(env)
        lead = await env.storage.db.leads.find_one({})
        for suffix in [
            f"/conversations/{env.context.session_id}",
            f"/conversations/{env.context.session_id}/turns",
            f"/leads/{lead['lead_id']}",
            "/documents/refund/versions/1",
        ]:
            assert (
                await client.get("/api/v1/businesses/fitness-studio" + suffix)
            ).status_code == 404
        for suffix in ("/leads", "/conversations", "/documents"):
            assert (await client.get("/api/v1/businesses/unknown" + suffix)).status_code == 404

    @pytest.mark.parametrize("extra", ["businessId", "knowledgeBaseId", "status", "vector"])
    async def test_upload_cannot_forge_owner_or_processing(self, workspace, extra):
        client, _, _, _ = workspace
        response = await client.post(BASE + "/documents", json=document() | {extra: "forged"})
        assert response.status_code == 400

    async def test_script_content_is_stored_as_data(self, workspace):
        client, _, _, _ = workspace
        body = document() | {"content": "<script>window.assessmentInjected=true</script>"}
        response = await client.post(BASE + "/documents", json=body)
        assert response.status_code == 200 and response.json()["content"] == body["content"]
        assert response.headers["Content-Type"].startswith("application/json")
