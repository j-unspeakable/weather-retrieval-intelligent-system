"""Validated environment-backed application configuration."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_env: Literal["local", "databricks", "test"] = "local"

    pg_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PGHOST", "PG_HOST"),
    )
    pg_database: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PGDATABASE", "PG_DATABASE"),
    )
    pg_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PGUSER", "PG_USER"),
    )
    pg_port: int = Field(
        default=5432,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("PGPORT", "PG_PORT"),
    )
    pg_sslmode: str = Field(
        default="require",
        validation_alias=AliasChoices("PGSSLMODE", "PG_SSLMODE"),
    )
    endpoint_name: str | None = None
    databricks_config_profile: str | None = None
    llm_api_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    llm_model_name: str = "openrouter/free"
    llm_request_timeout: int = Field(default=45, ge=1)

    weather_user_agent: str = "weather-retrieval-system/0.1"
    nws_api_base_url: str = "https://api.weather.gov"
    geocoding_api_base_url: str = "https://geocoding-api.open-meteo.com/v1"
    weather_request_timeout: int = Field(default=30, ge=1)
    weather_state_sync_workers: int = Field(default=6, ge=1, le=12)

    @field_validator(
        "nws_api_base_url",
        "geocoding_api_base_url",
        "llm_api_base_url",
    )
    @classmethod
    def trim_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("llm_model_name")
    @classmethod
    def validate_llm_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("LLM_MODEL_NAME must not be blank")
        return value

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        if self.app_env == "test":
            return self

        required = {
            "PGHOST": self.pg_host,
            "PGDATABASE": self.pg_database,
            "PGUSER": self.pg_user,
            "ENDPOINT_NAME": self.endpoint_name,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                f"Missing required Lakebase settings: {', '.join(missing)}"
            )

        if self.pg_sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError(
                "Lakebase OAuth requires PGSSLMODE=require, verify-ca, or verify-full"
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
