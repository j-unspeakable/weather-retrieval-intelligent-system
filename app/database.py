"""Direct psycopg2 access for Databricks Lakebase using OAuth."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extras import Json, RealDictCursor

from app.config import get_settings
from app.models import WeatherDocument


WEATHER_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS weather_documents (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source_type TEXT NOT NULL,
    headline TEXT,
    narrative_text TEXT NOT NULL,
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


WEATHER_DOCUMENT_UPSERT = """
INSERT INTO weather_documents (
    id, location, latitude, longitude, source_type, headline,
    narrative_text, issued_at, effective_at, payload, synced_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (id) DO UPDATE
SET location = EXCLUDED.location,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    source_type = EXCLUDED.source_type,
    headline = EXCLUDED.headline,
    narrative_text = EXCLUDED.narrative_text,
    issued_at = EXCLUDED.issued_at,
    effective_at = EXCLUDED.effective_at,
    payload = EXCLUDED.payload,
    synced_at = now()
"""


@lru_cache
def _workspace_client() -> WorkspaceClient:
    """Use a named profile locally and the app identity on Databricks."""
    settings = get_settings()
    profile = (
        settings.databricks_config_profile
        if settings.app_env == "local"
        else None
    )
    return WorkspaceClient(profile=profile)


def _oauth_token(endpoint_name: str) -> str:
    credential = _workspace_client().postgres.generate_database_credential(
        endpoint=endpoint_name
    )
    if not credential.token:
        raise RuntimeError("Databricks did not return a Lakebase OAuth token")
    return credential.token


def _validate_oauth_settings() -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("PGHOST", settings.pg_host),
            ("PGDATABASE", settings.pg_database),
            ("PGUSER", settings.pg_user),
            ("ENDPOINT_NAME", settings.endpoint_name),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing Lakebase OAuth configuration: {', '.join(missing)}"
        )
    if settings.pg_sslmode not in {"require", "verify-ca", "verify-full"}:
        raise RuntimeError(
            "Lakebase OAuth requires PGSSLMODE=require, verify-ca, or verify-full"
        )


@contextmanager
def get_connection() -> Iterator[PsycopgConnection]:
    """Yield a connection authenticated with a freshly generated OAuth token."""
    _validate_oauth_settings()
    settings = get_settings()
    assert settings.pg_host is not None
    assert settings.pg_database is not None
    assert settings.pg_user is not None
    assert settings.endpoint_name is not None
    connection = psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=settings.pg_user,
        password=_oauth_token(settings.endpoint_name),
        sslmode=settings.pg_sslmode,
        cursor_factory=RealDictCursor,
    )
    try:
        yield connection
    finally:
        connection.close()


def ensure_weather_table() -> None:
    """Create the raw weather document table when it does not exist."""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(WEATHER_DOCUMENTS_DDL)
        connection.commit()


def upsert_weather_documents(documents: Sequence[WeatherDocument]) -> int:
    """Upsert normalized weather documents in one transaction."""
    if not documents:
        return 0

    count = 0
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for document in documents:
                cursor.execute(
                    WEATHER_DOCUMENT_UPSERT,
                    (
                        document.id,
                        document.location,
                        document.latitude,
                        document.longitude,
                        document.source_type,
                        document.headline,
                        document.narrative_text,
                        document.issued_at,
                        document.effective_at,
                        Json(document.payload),
                    ),
                )
                count += 1
        connection.commit()
    return count


def list_recent_weather_documents(limit: int) -> list[dict[str, Any]]:
    """Return recent rows without loading their potentially large payloads."""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, location, latitude, longitude, source_type, headline,
                       narrative_text, issued_at, effective_at, synced_at
                FROM weather_documents
                ORDER BY synced_at DESC, id ASC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cursor.fetchall())
