from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import REPOSITORY_ROOT, ConfigurationError, Settings, load_settings


def test_defaults_resolve_from_repository_after_cwd_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert Settings.model_config["env_file"] == REPOSITORY_ROOT / ".env"
    assert settings.business_config_dir == REPOSITORY_ROOT / "businesses"
    assert settings.api_port == 4000
    assert settings.gemini_api_key is None


def test_dotenv_and_environment_precedence_without_exposing_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "private-test.env"
    env.write_text(
        "API_PORT=4100\nGEMINI_API_KEY=synthetic-private-value\n"
        "BUSINESS_CONFIG_DIR=businesses\nUNRELATED_SETTING=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("API_PORT", "4200")
    settings = Settings(_env_file=env)
    assert settings.api_port == 4200
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "synthetic-private-value"
    assert "synthetic-private-value" not in repr(settings)
    assert "synthetic-private-value" not in settings.model_dump_json()


def test_committed_example_loads_without_provider_access() -> None:
    settings = Settings(_env_file=REPOSITORY_ROOT / ".env.example")
    assert settings.gemini_api_key is None
    assert settings.gemini_chat_model == "gemini-3.5-flash-lite"
    assert settings.mongodb_uri.get_secret_value().endswith("/ai_engagement")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("API_PORT", "0"),
        ("WEB_ORIGIN", "http://localhost:bad-port"),
        ("WEB_ORIGIN", "https://example.test/unexpected/path"),
        ("MONGODB_URI", "https://unexpected.example"),
        ("CHUNK_OVERLAP", "400"),
        ("RAG_TOP_K", "0"),
    ],
)
def test_invalid_configuration_is_rejected(
    key: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_configuration_error_does_not_echo_secret_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGODB_URI", "synthetic-private-invalid-value")
    with pytest.raises(ConfigurationError) as captured:
        load_settings()
    assert "synthetic-private-invalid-value" not in str(captured.value)
    assert captured.value.__suppress_context__


def test_failure_injection_is_rejected_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_TEST_FAILURES", "true")
    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.parametrize(
    "key,value",
    [
        ("AI_PROVIDER", "openai"),
        ("EMBEDDING_DIMENSIONS", "3"),
        ("GEMINI_EMBEDDING_MODEL", "text-embedding-3-small"),
        ("GEMINI_CHAT_MODEL", "https://untrusted.example/model"),
    ],
)
def test_gemini_configuration_rejects_invalid_provider_identity(key, value, monkeypatch):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
