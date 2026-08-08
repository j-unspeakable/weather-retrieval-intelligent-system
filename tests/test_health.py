from pathlib import Path

import pytest
from pydantic import ValidationError
import yaml

from app.config import Settings
from app.routers.health import healthz, home


DATABASE_ENV_NAMES = (
    "PGHOST",
    "PG_HOST",
    "PGDATABASE",
    "PG_DATABASE",
    "PGUSER",
    "PG_USER",
    "PGPORT",
    "PG_PORT",
    "PGSSLMODE",
    "PG_SSLMODE",
    "ENDPOINT_NAME",
    "DATABRICKS_CONFIG_PROFILE",
    "LLM_API_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL_NAME",
    "LLM_REQUEST_TIMEOUT",
)


def clear_database_environment(monkeypatch):
    for name in DATABASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_healthz_does_not_require_database():
    assert healthz() == {"status": "ok"}


def test_home_redirects_to_weather_ui():
    response = home()

    assert response.status_code == 307
    assert response.headers["location"] == "/weather"


def test_local_settings_accept_standard_postgres_names(monkeypatch):
    values = {
        "APP_ENV": "local",
        "PGHOST": "ep-example.database.databricks.com",
        "PGDATABASE": "databricks_postgres",
        "PGUSER": "developer@example.com",
        "PGPORT": "5432",
        "PGSSLMODE": "require",
        "ENDPOINT_NAME": "projects/example/branches/dev/endpoints/primary",
        "DATABRICKS_CONFIG_PROFILE": "weather-dev",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.app_env == "local"
    assert settings.pg_host == values["PGHOST"]
    assert settings.pg_database == values["PGDATABASE"]
    assert settings.pg_user == values["PGUSER"]
    assert settings.pg_port == 5432
    assert settings.pg_sslmode == "require"
    assert settings.endpoint_name == values["ENDPOINT_NAME"]
    assert settings.databricks_config_profile == "weather-dev"


def test_test_environment_does_not_require_lakebase_settings(monkeypatch):
    clear_database_environment(monkeypatch)
    settings = Settings(app_env="test", _env_file=None)

    assert settings.app_env == "test"
    assert settings.pg_host is None
    assert settings.endpoint_name is None
    assert settings.llm_api_base_url == "https://openrouter.ai/api/v1"
    assert settings.llm_api_key is None
    assert settings.llm_model_name == "openrouter/free"
    assert settings.llm_request_timeout == 45


@pytest.mark.parametrize("app_env", ["local", "databricks"])
def test_runtime_environments_require_lakebase_settings(monkeypatch, app_env):
    clear_database_environment(monkeypatch)
    with pytest.raises(ValidationError, match="Missing required Lakebase settings"):
        Settings(app_env=app_env, _env_file=None)


def test_runtime_environment_rejects_insecure_sslmode():
    with pytest.raises(ValidationError, match="requires PGSSLMODE"):
        Settings(
            app_env="local",
            pg_host="ep-example.database.databricks.com",
            pg_database="databricks_postgres",
            pg_user="developer@example.com",
            pg_sslmode="disable",
            endpoint_name="projects/example/branches/dev/endpoints/primary",
            _env_file=None,
        )


def test_app_yaml_uses_databricks_postgres_resource_binding():
    config = yaml.safe_load(Path("app.yaml").read_text(encoding="utf-8"))
    environment = {item["name"]: item for item in config["env"]}

    assert config["command"] == ["uvicorn", "app.main:app", "--workers", "1"]
    assert environment["APP_ENV"] == {
        "name": "APP_ENV",
        "value": "databricks",
    }
    assert environment["ENDPOINT_NAME"] == {
        "name": "ENDPOINT_NAME",
        "valueFrom": "postgres",
    }
    assert environment["LLM_API_KEY"] == {
        "name": "LLM_API_KEY",
        "valueFrom": "openrouter-api-key",
    }
    assert environment["LLM_API_BASE_URL"]["value"] == (
        "https://openrouter.ai/api/v1"
    )
    assert environment["LLM_MODEL_NAME"]["value"] == "openrouter/free"
    assert "PGHOST" not in environment
    assert "PGUSER" not in environment
