from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from app.database import open_database
from app.measurements import Measurements
from app.settings import Settings


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    # Background provider jobs require explicit opt-in in automation integration tests.
    monkeypatch.setenv("AUTOMATION_ENABLED", "false")


@pytest.fixture(autouse=True)
def isolate_unit_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Foundation tests use a fake; M03 integration explicitly uses the real factory."""

    async def ready() -> bool:
        return True

    @asynccontextmanager
    async def fake_database(_settings):
        yield SimpleNamespace(ready=ready, measurements=Measurements())

    monkeypatch.setattr("app.main.open_database", fake_database)


@pytest_asyncio.fixture
async def storage():
    name = "ai_engagement_test_" + uuid4().hex
    settings = Settings(_env_file=None, mongodb_uri=f"mongodb://127.0.0.1:27017/{name}")
    async with open_database(settings) as database:
        try:
            yield database
        finally:
            assert database.db.name == name and name.startswith("ai_engagement_test_")
            await database.client.drop_database(name)
