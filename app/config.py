"""Validated environment-backed application configuration."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import AliasChoices, Field, field_validator, model_validator
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

    weather_user_agent: str = "weather-retrieval-system/0.1"
    nws_api_base_url: str = "https://api.weather.gov"
    geocoding_api_base_url: str = "https://geocoding-api.open-meteo.com/v1"
    weather_request_timeout: int = Field(default=30, ge=1)
    recent_weather_limit: int = Field(default=100, ge=1)

    @field_validator("nws_api_base_url", "geocoding_api_base_url")
    @classmethod
    def trim_base_url(cls, value: str) -> str:
        return value.rstrip("/")

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
