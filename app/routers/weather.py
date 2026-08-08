"""Weather JSON and server-rendered routes."""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from app import database
from app.dependencies import WeatherClientDependency
from app.models import US_STATES, WeatherSourceType
from app.schemas import (
    StateWeatherSyncRequest,
    StateWeatherSyncResponse,
    WeatherSearchRequest,
    WeatherSearchResponse,
    WeatherSearchResult,
    WeatherSyncRequest,
    WeatherSyncResponse,
)
from app.services.weather import answer_weather_query, sync_state_weather, sync_weather
from app.services.weather_client import InvalidLocationError, WeatherAPIError


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weather", tags=["weather"])


@router.get("", response_class=HTMLResponse)
def weather_page(
    request: Request,
    synced: Annotated[int | None, Query(ge=0)] = None,
    error: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    return _render_weather_page(request, synced=synced, error=error)


@router.post("/search", response_model=WeatherSearchResponse)
def search_weather_json(payload: WeatherSearchRequest) -> WeatherSearchResponse:
    return _search_response(payload)


@router.get("/search", response_model=WeatherSearchResponse)
def search_weather_json_get(
    query: Annotated[str, Query(min_length=1)],
    top_k: Annotated[int, Query()] = 5,
    source_type: Annotated[WeatherSourceType | None, Query()] = None,
) -> WeatherSearchResponse:
    try:
        payload = WeatherSearchRequest(
            query=query,
            top_k=top_k,
            source_type=source_type,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc
    return _search_response(payload)


@router.post("/search-form", response_class=HTMLResponse)
def search_weather_form(
    request: Request,
    query: Annotated[str, Form()] = "",
    top_k: Annotated[str, Form()] = "5",
    source_type: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        payload = WeatherSearchRequest(
            query=query,
            top_k=int(top_k),
            source_type=source_type or None,
        )
    except (ValueError, ValidationError):
        return _render_weather_page(
            request,
            search_query=query,
            search_top_k=top_k,
            search_source_type=source_type,
            search_error="Enter a query, a whole-number result limit, and a valid source type.",
        )

    try:
        response = answer_weather_query(
            payload.query,
            payload.top_k,
            payload.source_type,
        )
    except Exception:
        logger.exception("Unable to search weather embeddings from the form")
        return _render_weather_page(
            request,
            search_query=payload.query,
            search_top_k=payload.top_k,
            search_source_type=(
                payload.source_type.value if payload.source_type is not None else ""
            ),
            search_error="Unable to search weather documents right now.",
        )

    return _render_weather_page(
        request,
        search_query=payload.query,
        search_top_k=payload.top_k,
        search_source_type=(
            payload.source_type.value if payload.source_type is not None else ""
        ),
        search_summary=response.summary,
        search_results=response.results,
        search_performed=True,
    )


def _search_response(payload: WeatherSearchRequest) -> WeatherSearchResponse:
    try:
        return answer_weather_query(
            payload.query,
            payload.top_k,
            payload.source_type,
        )
    except Exception as exc:
        logger.exception("Unable to search weather embeddings")
        raise HTTPException(
            status_code=503,
            detail="Unable to search weather documents right now.",
        ) from exc


def _render_weather_page(
    request: Request,
    *,
    synced: int | None = None,
    error: str | None = None,
    search_query: str = "",
    search_top_k: int | str = 5,
    search_source_type: str = "",
    search_summary: str | None = None,
    search_results: list[WeatherSearchResult] | None = None,
    search_performed: bool = False,
    search_error: str | None = None,
) -> HTMLResponse:
    summary = {
        "total_documents": 0,
        "total_locations": 0,
        "source_type_count": 0,
        "total_embeddings": 0,
        "last_synced_at": None,
        "source_counts": {},
    }
    page_error = error
    try:
        database.ensure_weather_table()
        summary = database.get_weather_summary()
    except Exception:
        logger.exception("Unable to load the weather document summary")
        page_error = page_error or "Unable to load the Lakebase summary."

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="weather/index.html",
        context={
            "summary": summary,
            "synced": synced,
            "error": page_error,
            "default_limit": 50,
            "states": US_STATES,
            "search_source_types": list(WeatherSourceType),
            "search_query": search_query,
            "search_top_k": search_top_k,
            "search_source_type": search_source_type,
            "search_summary": search_summary,
            "search_results": search_results or [],
            "search_performed": search_performed,
            "search_error": search_error,
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
    logger.info(
        "State sync request received state=%s source_types=%s station_limit=%d",
        payload.state,
        payload.source_types,
        payload.station_limit,
    )
    try:
        synced, batch = sync_state_weather(
            payload.state,
            payload.source_types,
            payload.station_limit,
            client,
        )
    except InvalidLocationError as exc:
        logger.warning("State sync rejected state=%s error=%s", payload.state, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WeatherAPIError as exc:
        logger.warning(
            "State sync upstream failure state=%s error=%s",
            payload.state,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("State sync failed state=%s", payload.state)
        raise HTTPException(
            status_code=500,
            detail="Unable to persist state weather data.",
        ) from exc
    logger.info(
        "State sync response ready state=%s synced=%d zones=%d stations=%d",
        payload.state,
        synced,
        batch.zones_processed,
        batch.stations_processed,
    )
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
