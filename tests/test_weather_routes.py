from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from starlette.requests import Request

from app import database
from app.main import app
from app.models import StateWeatherBatch, US_STATES, WeatherSourceType
from app.routers import weather as weather_router
from app.schemas import StateWeatherSyncRequest, WeatherSyncRequest
from app.services.weather_client import InvalidLocationError, WeatherAPIError


class RouteWeatherClient:
    pass


class FakeTemplates:
    def TemplateResponse(self, *, request, name, context):
        return {"request": request, "name": name, "context": context}


def test_json_sync_trims_locations_and_returns_response(monkeypatch):
    captured = {}
    route_client = RouteWeatherClient()

    def fake_sync(locations, limit, weather_client, source_types):
        captured.update(
            locations=locations,
            limit=limit,
            client=weather_client,
            source_types=source_types,
        )
        return 7

    monkeypatch.setattr(weather_router, "sync_weather", fake_sync)
    payload = WeatherSyncRequest(
        locations=[" Chicago, IL ", " 30.2672,-97.7431 "], limit=5
    )

    response = weather_router.sync_weather_json(payload, route_client)

    assert response.model_dump() == {
        "synced": 7,
        "locations": ["Chicago, IL", "30.2672,-97.7431"],
    }
    assert captured == {
        "locations": ["Chicago, IL", "30.2672,-97.7431"],
        "limit": 5,
        "client": route_client,
        "source_types": ["forecast", "alert"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"locations": []},
        {"locations": [""]},
        {"locations": ["   "]},
        {"locations": [123]},
        {"locations": ["Chicago, IL"], "limit": 0},
        {"locations": ["Chicago, IL"], "limit": -1},
        {"locations": ["Chicago, IL"], "limit": True},
        {"locations": ["Chicago, IL"], "limit": "50"},
        {"locations": ["Chicago, IL"], "limit": 1.5},
        {"locations": ["Chicago, IL"], "source_types": []},
        {"locations": ["Chicago, IL"], "source_types": ["zone_forecast"]},
    ],
)
def test_json_schema_rejects_invalid_request_bodies(payload):
    with pytest.raises(ValidationError):
        WeatherSyncRequest.model_validate(payload)


def test_json_sync_maps_location_and_upstream_errors(monkeypatch):
    payload = WeatherSyncRequest(locations=["Chicago, IL"])
    route_client = RouteWeatherClient()

    monkeypatch.setattr(
        weather_router,
        "sync_weather",
        lambda *args: (_ for _ in ()).throw(InvalidLocationError("bad location")),
    )
    with pytest.raises(HTTPException) as location_error:
        weather_router.sync_weather_json(payload, route_client)
    assert location_error.value.status_code == 400
    assert location_error.value.detail == "bad location"

    monkeypatch.setattr(
        weather_router,
        "sync_weather",
        lambda *args: (_ for _ in ()).throw(WeatherAPIError("provider failed")),
    )
    with pytest.raises(HTTPException) as provider_error:
        weather_router.sync_weather_json(payload, route_client)
    assert provider_error.value.status_code == 502
    assert provider_error.value.detail == "provider failed"


def test_state_json_sync_returns_coverage_details(monkeypatch, document_factory):
    batch = StateWeatherBatch(
        state="IL",
        documents=[document_factory("zone_forecast:one")],
        locations=["Northern Cook, IL"],
        zones_processed=1,
        stations_processed=2,
    )
    captured = {}

    def fake_sync(state, source_types, station_limit, client):
        captured.update(
            state=state,
            source_types=source_types,
            station_limit=station_limit,
            client=client,
        )
        return 3, batch

    monkeypatch.setattr(weather_router, "sync_state_weather", fake_sync)
    client = RouteWeatherClient()
    payload = StateWeatherSyncRequest(
        state=" il ",
        source_types=["zone_forecast", "observation", "alert"],
        station_limit=10,
    )

    response = weather_router.sync_weather_state_json(payload, client)

    assert response.model_dump() == {
        "synced": 3,
        "state": "IL",
        "source_types": ["zone_forecast", "observation", "alert"],
        "zones_processed": 1,
        "stations_processed": 2,
        "locations": ["Northern Cook, IL"],
    }
    assert captured == {
        "state": "IL",
        "source_types": ["zone_forecast", "observation", "alert"],
        "station_limit": 10,
        "client": client,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"state": ""},
        {"state": "XX"},
        {"state": "Illinois"},
        {"state": "IL", "source_types": []},
        {"state": "IL", "source_types": ["forecast"]},
        {"state": "IL", "station_limit": 0},
        {"state": "IL", "station_limit": 201},
        {"state": "IL", "station_limit": "25"},
    ],
)
def test_state_schema_rejects_invalid_requests(payload):
    with pytest.raises(ValidationError):
        StateWeatherSyncRequest.model_validate(payload)


def test_weather_page_loads_feedback_and_lakebase_summary(monkeypatch):
    monkeypatch.setattr(database, "ensure_weather_table", lambda: None)
    monkeypatch.setattr(
        database,
        "get_weather_summary",
        lambda: {
            "total_documents": 42,
            "total_embeddings": 84,
            "total_locations": 7,
            "source_type_count": 3,
            "last_synced_at": "2026-08-08T10:00:00+00:00",
            "source_counts": {"forecast": 20, "alert": 2},
        },
    )
    fake_templates = FakeTemplates()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(templates=fake_templates))
    )

    response = weather_router.weather_page(request, synced=1, error=None)

    assert response["name"] == "weather/index.html"
    assert response["context"]["synced"] == 1
    assert response["context"]["summary"]["total_documents"] == 42
    assert response["context"]["summary"]["total_locations"] == 7


def test_real_weather_template_contains_clean_sync_controls_and_summary():
    template = app.state.templates.get_template("weather/index.html")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/weather",
            "raw_path": b"/weather",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": app,
        }
    )
    html = template.render(
        request=request,
        synced=1,
        error=None,
        default_limit=50,
        states=US_STATES,
        summary={
            "total_documents": 1234,
            "total_embeddings": 2345,
            "total_locations": 18,
            "source_type_count": 5,
            "last_synced_at": "2026-08-08T10:00:00+00:00",
            "source_counts": {
                "forecast": 500,
                "hourly_forecast": 400,
                "zone_forecast": 200,
                "alert": 34,
                "observation": 100,
            },
        },
        search_source_types=list(WeatherSourceType),
    )

    assert "Sync selected states" in html
    assert "Sync precise locations" in html
    assert "Build a state-wide corpus" in html
    assert "Target precise locations" in html
    assert "Select all" in html
    assert 'id="selected-state-count">0 selected' in html
    assert 'name="states" value="IL"' in html
    assert 'id="sync-selected-states"' in html
    assert "Successfully synced 1 weather document." in html
    assert "Weather corpus at a glance" in html
    assert "1,234" in html
    assert "2,345" in html
    assert "18" in html
    assert "Recent weather documents" not in html


def test_form_sync_uses_shared_service_and_redirects(monkeypatch):
    captured = {}

    def fake_sync(locations, limit, weather_client, source_types):
        captured.update(
            locations=locations,
            limit=limit,
            source_types=source_types,
        )
        return 4

    monkeypatch.setattr(weather_router, "sync_weather", fake_sync)

    response = weather_router.sync_weather_form(
        RouteWeatherClient(),
        locations=" Chicago, IL \nAustin, TX",
        limit="2",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/weather?synced=4"
    assert captured == {
        "locations": ["Chicago, IL", "Austin, TX"],
        "limit": 2,
        "source_types": ["forecast", "alert"],
    }


@pytest.mark.parametrize(
    "locations,limit",
    [
        ("   ", "50"),
        ("Chicago, IL", "0"),
        ("Chicago, IL", "not-a-number"),
    ],
)
def test_form_validation_redirects_with_feedback(monkeypatch, locations, limit):
    monkeypatch.setattr(
        weather_router,
        "sync_weather",
        lambda *args: pytest.fail("service should not be called"),
    )

    response = weather_router.sync_weather_form(
        RouteWeatherClient(), locations=locations, limit=limit
    )

    assert response.status_code == 303
    parsed = urlparse(response.headers["location"])
    assert parsed.path == "/weather"
    assert "error" in parse_qs(parsed.query)
