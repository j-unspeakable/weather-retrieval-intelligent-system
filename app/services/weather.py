"""Shared weather ingestion orchestration."""

from collections.abc import Sequence
import logging
import time

from app import database
from app.models import StateWeatherBatch, WeatherSourceType
from app.schemas import WeatherSearchResponse, WeatherSearchResult
from app.services.embeddings import (
    EMBEDDING_MODEL_NAME,
    embed_query,
    serialize_pgvector,
)
from app.services.llm import generate_weather_summary
from app.services.weather_client import WeatherClient


logger = logging.getLogger(__name__)


def sync_weather(
    locations: Sequence[str],
    limit: int,
    client: WeatherClient,
    source_types: Sequence[str] = ("forecast", "alert"),
) -> int:
    """Fetch all locations before atomically persisting the selected documents."""
    documents = []
    for location in locations:
        location_documents = client.fetch_documents(location, source_types)
        documents.extend(location_documents[:limit])

    database.ensure_weather_table()
    return database.upsert_weather_documents(documents)


def sync_state_weather(
    state: str,
    source_types: Sequence[str],
    station_limit: int,
    client: WeatherClient,
) -> tuple[int, StateWeatherBatch]:
    """Fetch one state completely before writing its documents atomically."""
    started_at = time.monotonic()
    logger.info(
        "State sync service started state=%s source_types=%s station_limit=%d",
        state,
        list(source_types),
        station_limit,
    )
    batch = client.fetch_state_documents(state, source_types, station_limit)
    logger.info(
        "State sync fetch ready state=%s documents=%d; starting Lakebase upsert",
        state,
        len(batch.documents),
    )
    database.ensure_weather_table()
    synced = database.upsert_weather_documents(batch.documents)
    logger.info(
        "State sync service completed state=%s synced=%d elapsed_seconds=%.2f",
        state,
        synced,
        time.monotonic() - started_at,
    )
    return synced, batch


def search_weather(
    query: str,
    top_k: int,
    source_type: WeatherSourceType | None = None,
) -> list[WeatherSearchResult]:
    """Embed a query and return ranked weather chunks with source metadata."""
    query_vector = serialize_pgvector(embed_query(query))
    rows = database.search_weather_embeddings(
        query_vector=query_vector,
        model_name=EMBEDDING_MODEL_NAME,
        top_k=top_k,
        source_type=source_type.value if source_type is not None else None,
    )
    return [WeatherSearchResult.model_validate(row) for row in rows]


def answer_weather_query(
    query: str,
    top_k: int,
    source_type: WeatherSourceType | None = None,
) -> WeatherSearchResponse:
    """Retrieve matching chunks and generate one grounded natural-language answer."""
    results = search_weather(query, top_k, source_type)
    summary = generate_weather_summary(query, results)
    return WeatherSearchResponse(
        query=query,
        top_k=top_k,
        source_type=source_type,
        summary=summary,
        results=results,
    )
