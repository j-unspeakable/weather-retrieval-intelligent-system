from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import FakeResponse, FakeSession
from weather_broker import (
    LocationResolutionError,
    WeatherBroker,
    WeatherInputError,
    WeatherRetrievalError,
    WeatherUpstreamError,
)


def daily_payload(code=3, **overrides):
    daily = {
        "time": ["2026-08-10"],
        "weather_code": [code],
        "temperature_2m_max": [82.0],
        "temperature_2m_min": [62.0],
        "apparent_temperature_max": [84.0],
        "apparent_temperature_min": [61.0],
        "precipitation_probability_max": [25],
        "precipitation_sum": [0.01],
        "rain_sum": [0.01],
        "showers_sum": [0.0],
        "snowfall_sum": [0.0],
        "wind_speed_10m_max": [12.0],
        "wind_gusts_10m_max": [20.0],
        "sunrise": ["2026-08-10T05:30"],
        "sunset": ["2026-08-10T20:15"],
    }
    for key, value in overrides.items():
        daily[key] = [value]
    return {
        "timezone": "Europe/London",
        "timezone_abbreviation": "BST",
        "daily": daily,
    }


def test_global_geocoding_selects_first_result_without_country_filter(geocoding_result):
    session = FakeSession(get_responses=[FakeResponse(geocoding_result)])
    broker = WeatherBroker(session=session)

    resolved = broker.resolve_location("London")

    assert resolved.name == "London"
    assert resolved.country == "United Kingdom"
    assert resolved.region == "England"
    assert resolved.timezone == "Europe/London"
    _, request = session.get_calls[0]
    assert request["params"] == {
        "name": "London",
        "count": 1,
        "language": "en",
        "format": "json",
    }
    assert "countryCode" not in request["params"]


def test_city_state_remains_valid_free_form_input(geocoding_result):
    geocoding_result["results"][0].update(
        name="Chicago",
        admin1="Illinois",
        country="United States",
        country_code="US",
    )
    session = FakeSession(get_responses=[FakeResponse(geocoding_result)])

    resolved = WeatherBroker(session=session).resolve_location("Chicago, IL")

    assert resolved.name == "Chicago"
    assert session.get_calls[0][1]["params"]["name"] == "Chicago, IL"


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("91,0", "latitude"),
        ("0,181", "longitude"),
        (" ", "blank"),
    ],
)
def test_invalid_locations_are_rejected(location, message):
    with pytest.raises(LocationResolutionError, match=message):
        WeatherBroker(session=FakeSession()).resolve_location(location)


def test_coordinates_bypass_geocoding():
    session = FakeSession()
    resolved = WeatherBroker(session=session).resolve_location("47.6062,-122.3321")
    assert resolved.name == "47.6062,-122.3321"
    assert resolved.latitude == 47.6062
    assert session.get_calls == []


def test_broker_reads_environment_names_by_component_ownership(monkeypatch):
    monkeypatch.setenv("MCP_OPEN_METEO_GEOCODING_URL", "https://geo.example/v1")
    monkeypatch.setenv("MCP_OPEN_METEO_FORECAST_URL", "https://weather.example/v1")
    monkeypatch.setenv("MCP_WEATHER_REQUEST_TIMEOUT", "12")
    monkeypatch.setenv("WEATHER_API_APP_URL", "http://weather-api:8000")

    broker = WeatherBroker(session=FakeSession())

    assert broker.geocoding_base_url == "https://geo.example/v1"
    assert broker.forecast_base_url == "https://weather.example/v1"
    assert broker.timeout == 12
    assert broker.weather_api_app_url == "http://weather-api:8000"


def test_missing_geocoding_result_is_rejected():
    session = FakeSession(get_responses=[FakeResponse({"results": []})])
    with pytest.raises(LocationResolutionError, match="Could not resolve"):
        WeatherBroker(session=session).resolve_location("Atlantis")


def test_current_weather_uses_imperial_units_and_normalizes_values(geocoding_result):
    current_payload = {
        "timezone": "Europe/London",
        "timezone_abbreviation": "BST",
        "current": {
            "time": "2026-08-09T14:00",
            "temperature_2m": 72.5,
            "relative_humidity_2m": 58,
            "apparent_temperature": 73.0,
            "precipitation": 0.0,
            "rain": 0.0,
            "showers": 0.0,
            "snowfall": 0.0,
            "weather_code": 2,
            "cloud_cover": 42,
            "surface_pressure": 1012.4,
            "wind_speed_10m": 8.2,
            "wind_direction_10m": 240,
            "wind_gusts_10m": 14.0,
        },
    }
    session = FakeSession(
        get_responses=[FakeResponse(geocoding_result), FakeResponse(current_payload)]
    )

    result = WeatherBroker(session=session).get_current_weather("London")

    assert result["current"]["condition"] == "Partly cloudy"
    assert result["current"]["temperature"] == 72.5
    assert result["resolved_location"]["country"] == "United Kingdom"
    assert result["resolved_location"]["timezone"] == "Europe/London"
    assert result["units"]["temperature"] == "°F"
    _, request = session.get_calls[1]
    assert request["params"]["temperature_unit"] == "fahrenheit"
    assert request["params"]["wind_speed_unit"] == "mph"
    assert request["params"]["precipitation_unit"] == "inch"
    assert request["params"]["timezone"] == "auto"


def test_forecast_normalizes_daily_values(geocoding_result):
    session = FakeSession(
        get_responses=[FakeResponse(geocoding_result), FakeResponse(daily_payload())]
    )

    result = WeatherBroker(session=session).get_forecast("London", 1)

    assert result["days"] == 1
    assert result["forecast"][0]["condition"] == "Overcast"
    assert result["forecast"][0]["temperature_high"] == 82.0
    assert result["forecast"][0]["precipitation_probability_max"] == 25
    assert session.get_calls[1][1]["params"]["forecast_days"] == 1


def test_live_weather_surfaces_upstream_failure(geocoding_result):
    session = FakeSession(
        get_responses=[FakeResponse(geocoding_result), FakeResponse({}, 502)]
    )
    with pytest.raises(WeatherUpstreamError, match="retrieve weather data"):
        WeatherBroker(session=session).get_forecast("London", 1)


@pytest.mark.parametrize("days", [0, 17, True, 1.5, "5"])
def test_forecast_rejects_invalid_days(days):
    with pytest.raises(WeatherInputError, match="between 1 and 16"):
        WeatherBroker(session=FakeSession()).get_forecast("London", days)


def test_recommendation_returns_all_triggered_thresholds(monkeypatch):
    broker = WeatherBroker(session=FakeSession())
    day = {
        "date": "2026-08-10",
        "weather_code": 96,
        "condition": "Thunderstorm with slight hail",
        "temperature_high": 92,
        "temperature_low": 45,
        "apparent_temperature_high": 97,
        "apparent_temperature_low": 44,
        "precipitation_probability_max": 80,
        "precipitation": 0.5,
        "rain": 0.4,
        "showers": 0.1,
        "snowfall": 0.0,
        "wind_speed_max": 28,
        "wind_gusts_max": 40,
        "sunrise": "2026-08-10T06:00",
        "sunset": "2026-08-10T20:00",
    }
    monkeypatch.setattr(
        broker,
        "get_forecast",
        lambda location, days: {
            "requested_location": location,
            "resolved_location": {"name": "Chicago", "country": "United States"},
            "units": {"temperature": "°F"},
            "forecast": [day],
        },
    )

    result = broker.get_weather_recommendation("Chicago, IL", "2026-08-10")

    categories = {item["category"] for item in result["recommendations"]}
    assert categories == {"umbrella", "jacket", "heat", "wind", "thunderstorm"}
    assert result["evidence"] == day
    umbrella = result["recommendations"][0]
    assert umbrella["thresholds"]["precipitation_probability_max"] == ">= 40%"


def test_recommendation_returns_general_when_no_threshold_is_triggered(monkeypatch):
    broker = WeatherBroker(session=FakeSession())
    monkeypatch.setattr(
        broker,
        "get_forecast",
        lambda location, days: {
            "requested_location": location,
            "resolved_location": {"name": "London"},
            "units": {},
            "forecast": [
                {
                    "date": "2026-08-10",
                    "weather_code": 1,
                    "condition": "Mainly clear",
                    "temperature_high": 75,
                    "temperature_low": 60,
                    "apparent_temperature_high": 75,
                    "apparent_temperature_low": 60,
                    "precipitation_probability_max": 5,
                    "precipitation": 0,
                    "snowfall": 0,
                    "wind_speed_max": 5,
                    "wind_gusts_max": 10,
                }
            ],
        },
    )
    result = broker.get_weather_recommendation("London", "2026-08-10")
    assert result["recommendations"][0]["category"] == "general"


def test_recommendation_detects_snow_or_ice(monkeypatch):
    broker = WeatherBroker(session=FakeSession())
    day = {
        "date": "2026-08-10",
        "weather_code": 71,
        "condition": "Slight snowfall",
        "temperature_high": 35,
        "temperature_low": 30,
        "apparent_temperature_high": 32,
        "apparent_temperature_low": 25,
        "precipitation_probability_max": 20,
        "precipitation": 0,
        "snowfall": 0.2,
        "wind_speed_max": 10,
        "wind_gusts_max": 15,
    }
    monkeypatch.setattr(
        broker,
        "get_forecast",
        lambda location, days: {
            "requested_location": location,
            "resolved_location": {"name": "Oslo", "country": "Norway"},
            "units": {"snowfall": "inch"},
            "forecast": [day],
        },
    )

    result = broker.get_weather_recommendation("Oslo", "2026-08-10")

    categories = {item["category"] for item in result["recommendations"]}
    assert "snow_or_ice" in categories
    snow = next(
        item for item in result["recommendations"] if item["category"] == "snow_or_ice"
    )
    assert snow["evidence"] == {"weather_code": 71, "snowfall": 0.2}


@pytest.mark.parametrize("requested_date", ["tomorrow", "2026-99-99", 123])
def test_recommendation_rejects_invalid_dates(requested_date):
    with pytest.raises(WeatherInputError, match="YYYY-MM-DD"):
        WeatherBroker(session=FakeSession()).get_weather_recommendation(
            "London", requested_date
        )


def test_search_uses_local_day2_url_without_databricks_authentication():
    response_payload = {
        "query": "flooding",
        "top_k": 3,
        "source_type": "alert",
        "summary": None,
        "results": [{"document_id": "alert-1", "similarity": 0.9}],
    }
    session = FakeSession(post_responses=[FakeResponse(response_payload)])
    broker = WeatherBroker(
        session=session,
        weather_api_app_url="http://127.0.0.1:8000/",
    )

    result = broker.search_weather_documents(" flooding ", 3, "alert")

    url, request = session.post_calls[0]
    assert url == "http://127.0.0.1:8000/api/weather/search"
    assert request["headers"] == {}
    assert request["json"] == {
        "query": "flooding",
        "top_k": 3,
        "source_type": "alert",
    }
    assert result["source"] == "Day 2 Lakebase weather corpus"


@pytest.mark.parametrize(
    "source_type",
    ["forecast", "hourly_forecast", "zone_forecast", "alert", "observation"],
)
def test_search_accepts_every_day2_source_type(source_type):
    session = FakeSession(
        post_responses=[
            FakeResponse(
                {
                    "query": "weather",
                    "top_k": 5,
                    "source_type": source_type,
                    "summary": None,
                    "results": [],
                }
            )
        ]
    )
    broker = WeatherBroker(session=session, weather_api_app_url="http://day2")

    broker.search_weather_documents("weather", source_type=source_type)

    assert session.post_calls[0][1]["json"]["source_type"] == source_type


def test_search_resolves_databricks_app_and_uses_service_principal_auth(monkeypatch):
    calls = []

    class FakeApps:
        def get(self, name):
            calls.append(("get", name))
            return SimpleNamespace(url="https://day2-app.example/")

    class FakeConfig:
        def authenticate(self):
            calls.append(("authenticate",))
            return {"Authorization": "Bearer generated-by-sdk"}

    class FakeWorkspaceClient:
        def __init__(self):
            self.apps = FakeApps()
            self.config = FakeConfig()

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", FakeWorkspaceClient)
    session = FakeSession(
        post_responses=[
            FakeResponse(
                {
                    "query": "snow",
                    "top_k": 5,
                    "source_type": None,
                    "summary": None,
                    "results": [],
                }
            )
        ]
    )
    broker = WeatherBroker(session=session, weather_api_app_name="weather-day2")

    broker.search_weather_documents("snow")

    assert calls == [("get", "weather-day2"), ("authenticate",)]
    assert session.post_calls[0][1]["headers"] == {
        "Authorization": "Bearer generated-by-sdk"
    }


@pytest.mark.parametrize(
    ("query", "top_k", "source_type"),
    [
        (" ", 5, None),
        ("rain", 0, None),
        ("rain", 21, None),
        ("rain", True, None),
        ("rain", 5, "radar"),
    ],
)
def test_search_validates_inputs(query, top_k, source_type):
    with pytest.raises(WeatherInputError):
        WeatherBroker(
            session=FakeSession(),
            weather_api_app_url="http://day2",
        ).search_weather_documents(
            query, top_k, source_type
        )


def test_search_requires_day2_configuration():
    broker = WeatherBroker(
        session=FakeSession(),
        weather_api_app_url=None,
        weather_api_app_name=None,
    )
    broker.weather_api_app_url = None
    broker.weather_api_app_name = None
    with pytest.raises(WeatherRetrievalError, match="Configure"):
        broker.search_weather_documents("rain")
