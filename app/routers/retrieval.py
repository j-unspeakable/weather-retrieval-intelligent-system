"""Authenticated app-to-app weather retrieval routes."""

import logging

from fastapi import APIRouter, HTTPException

from app.schemas import WeatherSearchRequest, WeatherSearchResponse
from app.services.weather import search_weather


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weather", tags=["weather-retrieval"])


@router.post("/search", response_model=WeatherSearchResponse)
def search_weather_for_tools(
    payload: WeatherSearchRequest,
) -> WeatherSearchResponse:
    """Return raw semantic matches for authenticated app-to-app consumers."""
    try:
        results = search_weather(
            payload.query,
            payload.top_k,
            payload.source_type,
        )
    except Exception as exc:
        logger.exception("Unable to retrieve weather documents for an app consumer")
        raise HTTPException(
            status_code=503,
            detail="Unable to search weather documents right now.",
        ) from exc

    return WeatherSearchResponse(
        query=payload.query,
        top_k=payload.top_k,
        source_type=payload.source_type,
        summary=None,
        results=results,
    )
