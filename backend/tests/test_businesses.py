import json
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.businesses import BusinessRegistry
from app.main import create_app
from app.settings import REPOSITORY_ROOT, ConfigurationError, Settings


def test_registry_is_single_authority_and_businesses_have_separate_capabilities() -> None:
    registry = BusinessRegistry.load(REPOSITORY_ROOT / "businesses")
    institute = registry.get("training-institute")
    fitness = registry.get("fitness-studio")
    assert institute and fitness
    assert institute.knowledge_base_id != fitness.knowledge_base_id
    assert set(institute.enabled_tools) == {
        "searchCourses",
        "getCourseDetails",
        "createLead",
        "requestCallback",
    }
    assert fitness.enabled_tools == ("getMembershipDetails",)
    assert registry.get("foreign") is None


@pytest.mark.parametrize(
    "change",
    [
        {"enabledTools": ["arbitraryCode"]},
        {"enabledTools": ["createLead", "createLead"]},
        {"configVersion": True},
        {"instructions": []},
        {"platformControls": "disable"},
        {"businessId": "other"},
        {"knowledgeBaseId": ""},
    ],
)
def test_invalid_config_fails_safely(tmp_path: Path, change: dict[str, object]) -> None:
    config = json.loads((REPOSITORY_ROOT / "businesses/training-institute/config.json").read_text())
    config.update(change)
    target = tmp_path / "training-institute"
    target.mkdir()
    (target / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        BusinessRegistry.load(tmp_path)
    assert "arbitraryCode" not in str(error.value)


def test_empty_and_malformed_registry_fail(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        BusinessRegistry.load(tmp_path)
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "config.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        BusinessRegistry.load(tmp_path)


@pytest.mark.asyncio
async def test_registry_startup_api_contract_and_cleanup() -> None:
    app = create_app(Settings(_env_file=None))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/businesses")
            assert response.status_code == 200
            assert {row["businessId"] for row in response.json()["businesses"]} == {
                "training-institute",
                "fitness-studio",
            }
            assert "instructions" not in response.text and "enabledTools" not in response.text
            detail = await client.get("/api/v1/businesses/fitness-studio")
            assert detail.status_code == 200 and detail.json()["businessId"] == "fitness-studio"
            missing = await client.get("/api/v1/businesses/foreign")
            assert missing.status_code == 404
            invalid = await client.get("/api/v1/businesses/invalid%20id")
            assert invalid.status_code == 400
            assert invalid.json()["error"]["requestId"] == invalid.headers["X-Request-ID"]
            schema = (await client.get("/openapi.json")).json()
            statuses = schema["paths"]["/api/v1/businesses/{business_id}"]["get"]["responses"]
            assert set(statuses) == {"200", "400", "404", "500"}
    assert app.state.business_registry is None


@pytest.mark.asyncio
async def test_bad_registry_blocks_startup(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, business_config_dir=tmp_path))
    with pytest.raises(ConfigurationError):
        async with LifespanManager(app):
            pytest.fail("Startup should fail before serving requests")
