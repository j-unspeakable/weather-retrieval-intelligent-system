"""Weather JSON and server-rendered routes."""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import database
from app.config import get_settings
from app.dependencies import WeatherClientDependency
from app.models import US_STATES
from app.schemas import (
    StateWeatherSyncRequest,
    StateWeatherSyncResponse,
    WeatherSyncRequest,
    WeatherSyncResponse,
)
from app.services.weather import sync_state_weather, sync_weather
from app.services.weather_client import InvalidLocationError, WeatherAPIError


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("", response_class=HTMLResponse)
def weather_page(
    request: Request,
    synced: Annotated[int | None, Query(ge=0)] = None,
    error: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    documents = []
    page_error = error
    try:
        database.ensure_weather_table()
        documents = database.list_recent_weather_documents(
            get_settings().recent_weather_limit
        )
    except Exception:
        logger.exception("Unable to load recent weather documents")
        page_error = page_error or "Unable to load weather documents from Lakebase."

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="weather/index.html",
        context={
            "documents": documents,
            "synced": synced,
            "error": page_error,
            "default_limit": 50,
            "states": US_STATES,
        },
    )


@router.post("/sync", response_model=WeatherSyncResponse)
def sync_weather_json(
    payload: WeatherSyncRequest,
    client: WeatherClientDependency,
) -> WeatherSyncResponse:
    try:
        synced = sync_weather(
            payload.locations,
            payload.limit,
            client,
            payload.source_types,
        )
    except InvalidLocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WeatherAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return WeatherSyncResponse(synced=synced, locations=payload.locations)


@router.post("/sync-state", response_model=StateWeatherSyncResponse)
def sync_weather_state_json(
    payload: StateWeatherSyncRequest,
    client: WeatherClientDependency,
) -> StateWeatherSyncResponse:
    try:
        synced, batch = sync_state_weather(
            payload.state,
            payload.source_types,
            payload.station_limit,
            client,
        )
    except InvalidLocationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WeatherAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return StateWeatherSyncResponse(
        synced=synced,
        state=batch.state,
        source_types=payload.source_types,
        zones_processed=batch.zones_processed,
        stations_processed=batch.stations_processed,
        locations=batch.locations,
    )


@router.post("/sync-form", response_class=RedirectResponse)
def sync_weather_form(
    client: WeatherClientDependency,
    locations: Annotated[str, Form()] = "",
    limit: Annotated[str, Form()] = "50",
    source_types: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    try:
        parsed_limit = int(limit)
        payload = WeatherSyncRequest(
            locations=locations.splitlines(),
            limit=parsed_limit,
            source_types=source_types or ["forecast", "alert"],
        )
        synced = sync_weather(
            payload.locations,
            payload.limit,
            client,
            payload.source_types,
        )
    except (ValueError, ValidationError) as exc:
        return _error_redirect(f"Invalid form input: {exc}")
    except InvalidLocationError as exc:
        return _error_redirect(str(exc))
    except WeatherAPIError as exc:
        return _error_redirect(str(exc))
    except Exception:
        logger.exception("Unable to sync weather documents from the form")
        return _error_redirect("Unable to sync weather documents.")

    return RedirectResponse(url=f"/weather?synced={synced}", status_code=303)


def _error_redirect(message: str) -> RedirectResponse:
    query = urlencode({"error": message})
    return RedirectResponse(url=f"/weather?{query}", status_code=303)
