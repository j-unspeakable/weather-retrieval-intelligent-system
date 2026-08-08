"""Direct psycopg2 access for Databricks Lakebase using OAuth."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
import logging
from typing import Any

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extras import Json, RealDictCursor, execute_values

from app.config import get_settings
from app.models import WeatherDocument


logger = logging.getLogger(__name__)


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
VALUES %s
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


WEATHER_EMBEDDING_SEARCH = """
SELECT
    d.id AS document_id,
    d.source_type,
    d.location,
    d.headline,
    d.narrative_text,
    e.chunk_text,
    1 - (e.embedding <=> %s::vector) AS similarity
FROM weather_embeddings AS e
JOIN weather_documents AS d
    ON e.document_id = d.id
WHERE e.model_name = %s
  AND (%s IS NULL OR d.source_type = %s)
ORDER BY e.embedding <=> %s::vector
LIMIT %s
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

    documents_by_id = {document.id: document for document in documents}
    values = [
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
        )
        for document in documents_by_id.values()
    ]
    logger.info(
        "Weather document batch upsert started received=%d unique=%d",
        len(documents),
        len(values),
    )
    with get_connection() as connection:
        with connection.cursor() as cursor:
            execute_values(
                cursor,
                WEATHER_DOCUMENT_UPSERT,
                values,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                page_size=500,
            )
        connection.commit()
    logger.info("Weather document batch upsert completed documents=%d", len(values))
    return len(values)


def get_weather_summary() -> dict[str, Any]:
    """Return compact Lakebase document and location statistics."""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total_documents,
                       COUNT(DISTINCT location) AS total_locations,
                       COUNT(DISTINCT source_type) AS source_type_count,
                       (SELECT COUNT(*) FROM weather_embeddings) AS total_embeddings,
                       MAX(synced_at) AS last_synced_at
                FROM weather_documents
                """
            )
            summary_rows = list(cursor.fetchall())

            cursor.execute(
                """
                SELECT source_type, COUNT(*) AS document_count
                FROM weather_documents
                GROUP BY source_type
                ORDER BY source_type
                """
            )
            source_rows = list(cursor.fetchall())

    summary = dict(summary_rows[0]) if summary_rows else {}
    summary["total_documents"] = int(summary.get("total_documents") or 0)
    summary["total_locations"] = int(summary.get("total_locations") or 0)
    summary["source_type_count"] = int(summary.get("source_type_count") or 0)
    summary["total_embeddings"] = int(summary.get("total_embeddings") or 0)
    summary["source_counts"] = {
        row["source_type"]: int(row["document_count"]) for row in source_rows
    }
    return summary


def search_weather_embeddings(
    query_vector: str,
    model_name: str,
    top_k: int,
    source_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return chunks ranked by cosine similarity with their source documents."""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                WEATHER_EMBEDDING_SEARCH,
                (
                    query_vector,
                    model_name,
                    source_type,
                    source_type,
                    query_vector,
                    top_k,
                ),
            )
            rows = [dict(row) for row in cursor.fetchall()]

    for row in rows:
        row["similarity"] = float(row["similarity"])
    return rows
