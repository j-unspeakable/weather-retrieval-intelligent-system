"""Shared weather ingestion orchestration."""

from collections.abc import Sequence

from app import database
from app.models import StateWeatherBatch
from app.services.weather_client import WeatherClient


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
    batch = client.fetch_state_documents(state, source_types, station_limit)
    database.ensure_weather_table()
    synced = database.upsert_weather_documents(batch.documents)
    return synced, batch
