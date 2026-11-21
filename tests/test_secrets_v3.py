import pytest

from app.core.secrets import read_secret


@pytest.mark.parametrize("name", ["FINANCE_API_KEY", "PHONE_API_KEY", "FIREFLY_API_TOKEN", "LLM_API_KEY", "LLM_FALLBACK_API_KEY", "MAIL_ACCOUNTS", "OWN_ACCOUNTS", "DATABASE_URL"])
def test_secret_file_precedence_and_newline(name, monkeypatch, tmp_path):
    path = tmp_path / "secret"
    path.write_text(" secret value \r\n", encoding="utf-8")
    monkeypatch.setenv(name, "old-secret")
    monkeypatch.setenv(name + "_FILE", str(path))
    assert read_secret(name) == " secret value "


def test_absent_and_direct_environment(monkeypatch):
    monkeypatch.delenv("TEST_SECRET_FILE", raising=False)
    monkeypatch.delenv("TEST_SECRET", raising=False)
    assert read_secret("TEST_SECRET") is None
    monkeypatch.setenv("TEST_SECRET", " exact ")
    assert read_secret("TEST_SECRET") == " exact "


def test_unreadable_file_never_falls_back_or_leaks(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_SECRET", "sensitive-value")
    monkeypatch.setenv("TEST_SECRET_FILE", str(tmp_path / "private-path"))
    with pytest.raises(ValueError) as caught:
        read_secret("TEST_SECRET")
    assert str(caught.value) == "Cannot read secret TEST_SECRET_FILE"


@pytest.mark.parametrize("name", ["TEST_SECRET", "DATABASE_URL"])
def test_empty_file_setting_uses_direct_value(name, monkeypatch):
    monkeypatch.setenv(name + "_FILE", "")
    monkeypatch.setenv(name, "direct-value")
    assert read_secret(name) == "direct-value"
    monkeypatch.delenv(name)
    assert read_secret(name) is None


@pytest.fixture
def compose_env(monkeypatch):
    values = {
        "FINANCE_API_KEY": "test-admin-key-01234567890123456789",
        "PHONE_API_KEY": "test-phone-key-01234567890123456789",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "FIREFLY_API_TOKEN": "direct-firefly-token", "LLM_API_KEY": "direct-llm-token",
        "LLM_FALLBACK_API_KEY": "", "MAIL_ACCOUNTS": "", "OWN_ACCOUNTS": "",
        "SCHEDULER_ENABLED": "false", "APP_ENV": "production",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
        monkeypatch.setenv(name + "_FILE", "")
    return values


def test_compose_direct_values_with_empty_file_variables(compose_env):
    from app.core.config import Settings

    config = Settings(_env_file=None)
    for name in ("FINANCE_API_KEY", "PHONE_API_KEY", "FIREFLY_API_TOKEN", "LLM_API_KEY"):
        assert getattr(config, name.lower()).get_secret_value() == compose_env[name]
    assert config.database_url == compose_env["DATABASE_URL"]
    assert config.mail_accounts == config.own_accounts == []


def test_database_url_file_overrides_direct_environment(compose_env, monkeypatch, tmp_path):
    from app.core.config import Settings

    path = tmp_path / "database-url"
    path.write_text("sqlite+aiosqlite:///secret.db\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL_FILE", str(path))
    assert Settings(_env_file=None).database_url == "sqlite+aiosqlite:///secret.db"


@pytest.mark.parametrize("name", ["FINANCE_API_KEY", "PHONE_API_KEY"])
@pytest.mark.parametrize("length", [0, 1, 31, 32])
def test_api_key_minimum_length(compose_env, monkeypatch, name, length):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv(name, "k" * length)
    if length < 32:
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
    else:
        assert getattr(Settings(_env_file=None), name.lower()).get_secret_value() == "k" * length


def test_create_application_with_typical_compose_environment(compose_env, monkeypatch):
    from app import main
    from app.core.config import Settings

    config = Settings(_env_file=None)
    monkeypatch.setattr(main, "get_settings", lambda: config)
    application = main.create_application()
    assert application.docs_url is None
    paths = set(application.openapi()["paths"])
    assert "/api/v1/notifications/webhook" in paths
    assert "/api/v1/auth/check" in paths


def test_admin_and_phone_keys_must_be_distinct(compose_env, monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("PHONE_API_KEY", compose_env["FINANCE_API_KEY"])
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    assert compose_env["FINANCE_API_KEY"] not in str(caught.value)
