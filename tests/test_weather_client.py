from typing import Any
import threading
import time

import pytest
import requests

from app.config import Settings
from app.services.weather_client import (
    InvalidLocationError,
    WeatherAPIError,
    WeatherClient,
)


class FakeResponse:
    def __init__(
        self,
        data: Any,
        error: requests.RequestException | None = None,
        status_code: int = 200,
    ):
        self.data = data
        self.error = error
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def json(self) -> Any:
        return self.data


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, params=None, timeout=None) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        pg_host="unused.database.databricks.com",
        pg_database="databricks_postgres",
        pg_user="unused@example.com",
        pg_port=5432,
        pg_sslmode="require",
        endpoint_name="projects/test/branches/test/endpoints/test",
        weather_user_agent="weather-tests/1.0 (tests@example.com)",
        nws_api_base_url="https://api.weather.test",
        geocoding_api_base_url="https://geocoding.test/v1",
        weather_request_timeout=12,
        weather_state_sync_workers=1,
    )


def test_coordinate_location_is_validated_and_rounded(settings):
    client = WeatherClient(settings=settings, session=FakeSession([]))

    assert client.resolve_location(" 41.878113, -87.629799 ") == (41.8781, -87.6298)

    with pytest.raises(InvalidLocationError):
        client.resolve_location("91,-87")
    with pytest.raises(InvalidLocationError):
        client.resolve_location("not a location")


def test_city_state_geocoding_is_us_restricted_and_matches_state(settings):
    session = FakeSession(
        [
            FakeResponse(
                {
                    "results": [
                        {
                            "latitude": 30.2672,
                            "longitude": -97.7431,
                            "admin1": "Texas",
                        },
                        {
                            "latitude": 41.878113,
                            "longitude": -87.629799,
                            "admin1": "Illinois",
                        },
                    ]
                }
            )
        ]
    )
    client = WeatherClient(settings=settings, session=session)

    assert client.resolve_location("Chicago, il") == (41.8781, -87.6298)
    assert session.calls[0]["url"] == "https://geocoding.test/v1/search"
    assert session.calls[0]["params"]["name"] == "Chicago"
    assert session.calls[0]["params"]["countryCode"] == "US"
    assert session.calls[0]["timeout"] == 12
    assert session.headers["User-Agent"] == "weather-tests/1.0 (tests@example.com)"


def test_city_state_rejects_unknown_state_and_missing_match(settings):
    client = WeatherClient(settings=settings, session=FakeSession([]))
    with pytest.raises(InvalidLocationError):
        client.resolve_location("Chicago, ZZ")

    client = WeatherClient(
        settings=settings,
        session=FakeSession([FakeResponse({"results": []})]),
    )
    with pytest.raises(InvalidLocationError):
        client.resolve_location("Chicago, IL")


def test_fetch_normalizes_and_deterministically_orders_documents(settings):
    later_period = {
        "name": "Tomorrow",
        "startTime": "2026-08-08T06:00:00-05:00",
        "detailedForecast": "Sunny.",
    }
    earlier_period = {
        "name": "Tonight",
        "startTime": "2026-08-07T18:00:00-05:00",
        "detailedForecast": "  Clear.  ",
    }
    later_alert = {
        "id": "https://api.weather.test/alerts/later",
        "properties": {
            "id": "urn:nws:alert:later",
            "headline": None,
            "event": "Flood Warning",
            "description": "Flooding expected.",
            "instruction": None,
            "sent": "2026-08-07T11:00:00+00:00",
            "effective": "2026-08-07T13:00:00+00:00",
        },
    }
    earlier_alert = {
        "id": "https://api.weather.test/alerts/earlier",
        "properties": {
            "id": "urn:nws:alert:earlier",
            "headline": "Heat Advisory issued August 7",
            "event": "Heat Advisory",
            "description": " Hot conditions. ",
            "instruction": " Drink water. ",
            "sent": "2026-08-07T09:00:00+00:00",
            "effective": "2026-08-07T10:00:00+00:00",
        },
    }
    point_response = {
        "properties": {"forecast": "https://api.weather.test/grid/forecast"}
    }
    forecast_response = {
        "properties": {
            "generatedAt": "2026-08-07T08:00:00+00:00",
            "periods": [later_period, earlier_period],
        }
    }
    alerts_response = {"features": [later_alert, earlier_alert]}
    session = FakeSession(
        [
            FakeResponse(point_response),
            FakeResponse(forecast_response),
            FakeResponse(alerts_response),
        ]
    )
    client = WeatherClient(settings=settings, session=session)

    documents = client.fetch_documents(" 41.8781,-87.6298 ")

    assert [document.source_type for document in documents] == [
        "forecast",
        "forecast",
        "alert",
        "alert",
    ]
    assert [document.headline for document in documents] == [
        "Tonight",
        "Tomorrow",
        "Heat Advisory issued August 7",
        "Flood Warning",
    ]
    assert documents[0].narrative_text == "Clear."
    assert documents[0].issued_at == "2026-08-07T08:00:00+00:00"
    assert documents[0].payload is earlier_period
    assert documents[2].id == "urn:nws:alert:earlier"
    assert documents[2].narrative_text == "Hot conditions.\n\nDrink water."
    assert documents[2].payload is earlier_alert
    assert all(document.location == "41.8781,-87.6298" for document in documents)
    assert session.calls[0]["url"].endswith("/points/41.8781,-87.6298")
    assert session.calls[2]["params"] == {"point": "41.8781,-87.6298"}

    repeated = client._normalize_documents(
        "41.8781,-87.6298",
        41.8781,
        -87.6298,
        forecast_response,
        alerts_response,
    )
    assert repeated[0].id == documents[0].id
    assert repeated[0].id.startswith("forecast:")


def test_upstream_http_error_is_wrapped(settings):
    session = FakeSession(
        [FakeResponse({}, requests.HTTPError("service unavailable"))]
    )
    client = WeatherClient(settings=settings, session=session)

    with pytest.raises(WeatherAPIError, match="upstream weather request"):
        client.fetch_documents("41.8781,-87.6298")


def test_point_sync_supports_hourly_forecast_and_nearest_observation(settings):
    point_response = {
        "properties": {
            "forecastHourly": "https://api.weather.test/grid/hourly",
            "observationStations": "https://api.weather.test/grid/stations",
        }
    }
    hourly_period = {
        "name": "",
        "startTime": "2026-08-07T14:00:00-05:00",
        "shortForecast": "Mostly sunny",
        "detailedForecast": "",
    }
    station = {
        "id": "https://api.weather.test/stations/KORD",
        "geometry": {"type": "Point", "coordinates": [-87.9048, 41.9786]},
        "properties": {
            "stationIdentifier": "KORD",
            "name": "Chicago O'Hare International Airport",
        },
    }
    observation = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-87.9049, 41.9787]},
        "properties": {
            "timestamp": "2026-08-07T19:00:00+00:00",
            "textDescription": "Mostly Cloudy",
            "temperature": {"value": 24.4, "unitCode": "wmoUnit:degC"},
            "relativeHumidity": {"value": 63.2, "unitCode": "wmoUnit:percent"},
            "windDirection": {"value": None, "unitCode": "wmoUnit:degree_(angle)"},
            "windSpeed": {"value": 14.8, "unitCode": "wmoUnit:km_h-1"},
        },
    }
    session = FakeSession(
        [
            FakeResponse(
                {
                    "results": [
                        {
                            "latitude": 41.8781,
                            "longitude": -87.6298,
                            "admin1": "Illinois",
                        }
                    ]
                }
            ),
            FakeResponse(point_response),
            FakeResponse(
                {
                    "properties": {
                        "generatedAt": "2026-08-07T18:55:00Z",
                        "periods": [hourly_period],
                    }
                }
            ),
            FakeResponse({"features": [station]}),
            FakeResponse(observation),
        ]
    )
    client = WeatherClient(settings=settings, session=session)

    documents = client.fetch_documents(
        "Chicago, IL", ["hourly_forecast", "observation"]
    )

    assert [document.source_type for document in documents] == [
        "hourly_forecast",
        "observation",
    ]
    assert documents[0].id.startswith("hourly_forecast:")
    assert documents[0].narrative_text == "Mostly sunny"
    assert documents[1].id == "observation:KORD:2026-08-07T19:00:00+00:00"
    assert documents[1].location == "Chicago, IL"
    assert documents[1].latitude == 41.9787
    assert documents[1].longitude == -87.9049
    assert documents[1].narrative_text == (
        "Mostly Cloudy. Temperature: 24.4 °C. Relative humidity: 63.2 %. "
        "Wind speed: 14.8 km/h."
    )
    assert documents[1].payload is observation
    assert session.calls[2]["url"] == "https://api.weather.test/grid/hourly"
    assert session.calls[3]["url"] == "https://api.weather.test/grid/stations"
    assert session.calls[4]["url"].endswith("/stations/KORD/observations/latest")


def test_state_sync_normalizes_zones_observations_and_alerts(settings):
    zone = {
        "id": "https://api.weather.test/zones/forecast/ILZ014",
        "properties": {"id": "ILZ014", "name": "Northern Cook"},
    }
    zone_period = {
        "number": 1,
        "name": "Rest Of Today",
        "detailedForecast": "Partly cloudy with a chance of showers.",
    }
    station = {
        "id": "https://api.weather.test/stations/KORD",
        "geometry": {"type": "Point", "coordinates": [-87.9048, 41.9786]},
        "properties": {
            "stationIdentifier": "KORD",
            "name": "Chicago O'Hare International Airport",
        },
    }
    observation = {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "timestamp": "2026-08-07T19:00:00+00:00",
            "textDescription": "Clear",
        },
    }
    alert = {
        "id": "https://api.weather.test/alerts/one",
        "properties": {
            "id": "urn:nws:alert:state-one",
            "event": "Heat Advisory",
            "description": "Hot conditions.",
            "instruction": "Drink water.",
            "sent": "2026-08-07T17:00:00Z",
            "effective": "2026-08-07T18:00:00Z",
        },
    }
    session = FakeSession(
        [
            FakeResponse({"features": [zone]}),
            FakeResponse(
                {
                    "properties": {
                        "updated": "2026-08-07T18:00:00Z",
                        "periods": [zone_period],
                    }
                }
            ),
            FakeResponse({"features": [station]}),
            FakeResponse(observation),
            FakeResponse({"features": [alert]}),
        ]
    )
    client = WeatherClient(settings=settings, session=session)

    batch = client.fetch_state_documents(
        "il", ["zone_forecast", "observation", "alert"], station_limit=10
    )

    assert batch.state == "IL"
    assert batch.zones_processed == 1
    assert batch.stations_processed == 1
    assert batch.locations == [
        "Northern Cook, IL",
        "Chicago O'Hare International Airport, IL",
        "Illinois",
    ]
    assert [document.source_type for document in batch.documents] == [
        "zone_forecast",
        "observation",
        "alert",
    ]
    zone_document, observation_document, alert_document = batch.documents
    assert zone_document.id.startswith("zone_forecast:")
    assert zone_document.location == "Northern Cook, IL"
    assert zone_document.latitude is None
    assert zone_document.issued_at == "2026-08-07T18:00:00Z"
    assert zone_document.effective_at is None
    assert zone_document.payload is zone_period
    assert observation_document.latitude == 41.9786
    assert observation_document.longitude == -87.9048
    assert alert_document.id == "urn:nws:alert:state-one"
    assert alert_document.location == "Illinois"
    assert alert_document.latitude is None
    assert alert_document.payload is alert
    assert session.calls[0]["params"] == {
        "area": "IL",
        "type": "forecast",
        "include_geometry": "false",
        "limit": 500,
    }
    assert session.calls[1]["url"].endswith(
        "/zones/forecast/ILZ014/forecast"
    )
    assert session.calls[2]["params"] == {"state": "IL", "limit": 10}
    assert session.calls[4]["params"] == {"area": "IL"}

    repeated = client._normalize_zone_forecast(
        "Northern Cook, IL",
        "ILZ014",
        {
            "properties": {
                "updated": "2026-08-07T18:00:00Z",
                "periods": [zone_period],
            }
        },
    )
    assert repeated[0].id == zone_document.id


def test_state_observation_sync_skips_stations_without_latest_report(settings):
    station = {
        "id": "https://api.weather.test/stations/KOLD",
        "properties": {"stationIdentifier": "KOLD", "name": "Inactive Station"},
    }
    session = FakeSession(
        [
            FakeResponse({"features": [station]}),
            FakeResponse({}, status_code=404),
        ]
    )
    client = WeatherClient(settings=settings, session=session)

    batch = client.fetch_state_documents("IL", ["observation"], station_limit=5)

    assert batch.documents == []
    assert batch.locations == []
    assert batch.stations_processed == 0


def test_state_zone_forecasts_use_bounded_concurrency_and_preserve_order(settings):
    zones = [
        {
            "id": f"https://api.weather.test/zones/forecast/ILZ00{index}",
            "properties": {"id": f"ILZ00{index}", "name": f"Zone {index}"},
        }
        for index in range(1, 5)
    ]

    class ConcurrentSession:
        def __init__(self):
            self.headers = {}
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def get(self, url, params=None, timeout=None):
            if url.endswith("/zones"):
                return FakeResponse({"features": zones})
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return FakeResponse(
                {
                    "properties": {
                        "updated": "2026-08-08T10:00:00Z",
                        "periods": [
                            {
                                "number": 1,
                                "name": "Today",
                                "detailedForecast": "Clear.",
                            }
                        ],
                    }
                }
            )

        def close(self):
            return None

    concurrent_settings = settings.model_copy(
        update={"weather_state_sync_workers": 3}
    )
    session = ConcurrentSession()
    client = WeatherClient(settings=concurrent_settings, session=session)

    batch = client.fetch_state_documents("IL", ["zone_forecast"])

    assert session.max_active == 3
    assert batch.zones_processed == 4
    assert batch.locations == [
        "Zone 1, IL",
        "Zone 2, IL",
        "Zone 3, IL",
        "Zone 4, IL",
    ]
