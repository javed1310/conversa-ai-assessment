from typing import Annotated
from uuid import UUID

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException, Query

from app.main import create_app
from app.settings import Settings


@pytest.mark.asyncio
async def test_lifecycle_is_owned_by_each_application() -> None:
    first = create_app(Settings(_env_file=None))
    second = create_app(Settings(_env_file=None))
    assert first.state.runtime is None
    async with LifespanManager(first):
        runtime = first.state.runtime
        assert runtime.active
        assert runtime.started_at.tzinfo is not None
        assert second.state.runtime is None
    assert not runtime.active
    assert first.state.runtime is None


@pytest.mark.asyncio
async def test_health_request_ids_and_unknown_route_do_not_disclose_settings() -> None:
    app = create_app(Settings(_env_file=None, gemini_api_key="synthetic-never-echo"))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/health/live", headers={"X-Request-ID": "untrusted-customer-id"}
            )
            assert response.status_code == 200
            assert response.json() == {"status": "alive", "service": "ai-engagement-api"}
            UUID(response.headers["X-Request-ID"])
            assert response.headers["X-Request-ID"] != "untrusted-customer-id"
            missing = await client.get("/api/v1/leads")
            assert missing.status_code == 404
            error = missing.json()["error"]
            assert error["code"] == "RESOURCE_NOT_FOUND"
            assert error["requestId"] == missing.headers["X-Request-ID"]
            assert not error["retryable"]
            assert "synthetic-never-echo" not in missing.text


@pytest.mark.asyncio
async def test_validation_and_http_errors_use_safe_envelopes() -> None:
    app = create_app(Settings(_env_file=None))

    @app.get("/_test/validation")
    async def validation(limit: Annotated[int, Query(ge=1)]) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/_test/forbidden")
    async def forbidden() -> None:
        raise HTTPException(403, detail="private implementation detail")

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            invalid = await client.get("/_test/validation?limit=0")
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
            denied = await client.get("/_test/forbidden")
            assert denied.status_code == 403
            assert "private implementation detail" not in denied.text


@pytest.mark.asyncio
async def test_unexpected_failure_does_not_expose_exception_or_fake_success() -> None:
    app = create_app(Settings(_env_file=None))

    @app.get("/_test/failure")
    async def failure() -> None:
        raise RuntimeError("synthetic-sensitive-error")

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/_test/failure")
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "INTERNAL_ERROR"
            assert response.json()["error"]["requestId"] == response.headers["X-Request-ID"]
            assert "synthetic-sensitive-error" not in response.text


@pytest.mark.asyncio
async def test_cors_only_allows_configured_browser_origin() -> None:
    app = create_app(Settings(_env_file=None))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            allowed = await client.options(
                "/api/v1/health/live",
                headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
            )
            assert allowed.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
            denied = await client.options(
                "/api/v1/health/live",
                headers={
                    "Origin": "https://unrelated.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert "Access-Control-Allow-Origin" not in denied.headers
