"""Live Open-Meteo and stored Day 2 weather retrieval broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
import re
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1"
DEFAULT_TIMEOUT_SECONDS = 20.0

SUPPORTED_SOURCE_TYPES = frozenset(
    {
        "forecast",
        "hourly_forecast",
        "zone_forecast",
        "alert",
        "observation",
    }
)

CURRENT_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "weather_code",
    "cloud_cover",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)

DAILY_FIELDS = (
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "apparent_temperature_max",
    "apparent_temperature_min",
    "precipitation_probability_max",
    "precipitation_sum",
    "rain_sum",
    "showers_sum",
    "snowfall_sum",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "sunrise",
    "sunset",
)

WMO_CONDITIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

SNOW_OR_ICE_CODES = frozenset({56, 57, 66, 67, 71, 73, 75, 77, 85, 86})
THUNDERSTORM_CODES = frozenset({95, 96, 99})

_COORDINATE_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)


class WeatherBrokerError(RuntimeError):
    """Base error surfaced by weather MCP tools."""


class LocationResolutionError(WeatherBrokerError):
    """Raised when a requested place cannot be resolved."""


class WeatherUpstreamError(WeatherBrokerError):
    """Raised when Open-Meteo cannot provide a usable response."""


class WeatherInputError(WeatherBrokerError):
    """Raised when a tool argument is invalid."""


class WeatherRetrievalError(WeatherBrokerError):
    """Raised when the Day 2 semantic retrieval app is unavailable."""


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    requested: str
    name: str
    latitude: float
    longitude: float
    region: str | None = None
    country: str | None = None
    country_code: str | None = None
    timezone: str | None = None


def _number_at(values: dict[str, Any], name: str, index: int) -> Any:
    series = values.get(name)
    if not isinstance(series, list) or index >= len(series):
        return None
    return series[index]


def _at_least(value: Any, threshold: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= threshold


def _at_most(value: Any, threshold: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= threshold


class WeatherBroker:
    """Synchronous broker used by thin FastMCP tool functions."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        geocoding_base_url: str | None = None,
        forecast_base_url: str | None = None,
        timeout: float | None = None,
        weather_api_app_url: str | None = None,
        weather_api_app_name: str | None = None,
    ) -> None:
        self.session = session or self._create_session()
        self.geocoding_base_url = (
            geocoding_base_url
            or os.getenv("MCP_OPEN_METEO_GEOCODING_URL")
            or OPEN_METEO_GEOCODING_URL
        ).rstrip("/")
        self.forecast_base_url = (
            forecast_base_url
            or os.getenv("MCP_OPEN_METEO_FORECAST_URL")
            or OPEN_METEO_FORECAST_URL
        ).rstrip("/")
        configured_timeout = timeout
        if configured_timeout is None:
            configured_timeout = float(
                os.getenv(
                    "MCP_WEATHER_REQUEST_TIMEOUT",
                    str(DEFAULT_TIMEOUT_SECONDS),
                )
            )
        if configured_timeout <= 0:
            raise ValueError("MCP_WEATHER_REQUEST_TIMEOUT must be greater than zero")
        self.timeout = configured_timeout
        self.weather_api_app_url = weather_api_app_url or os.getenv(
            "WEATHER_API_APP_URL"
        )
        self.weather_api_app_name = weather_api_app_name or os.getenv(
            "WEATHER_API_APP_NAME"
        )

    @staticmethod
    def _create_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        session.headers.update(
            {
                "User-Agent": os.getenv(
                    "MCP_WEATHER_USER_AGENT",
                    "weather-intelligence-mcp/0.1",
                ),
                "Accept": "application/json",
            }
        )
        return session

    def resolve_location(self, location: str) -> ResolvedLocation:
        if not isinstance(location, str) or not location.strip():
            raise LocationResolutionError("location must not be blank")
        requested = location.strip()

        coordinate_match = _COORDINATE_PATTERN.fullmatch(requested)
        if coordinate_match:
            latitude = float(coordinate_match.group(1))
            longitude = float(coordinate_match.group(2))
            if not -90 <= latitude <= 90:
                raise LocationResolutionError("latitude must be between -90 and 90")
            if not -180 <= longitude <= 180:
                raise LocationResolutionError("longitude must be between -180 and 180")
            label = f"{latitude:.4f},{longitude:.4f}"
            return ResolvedLocation(
                requested=requested,
                name=label,
                latitude=latitude,
                longitude=longitude,
            )

        payload = self._get_json(
            f"{self.geocoding_base_url}/search",
            params={"name": requested, "count": 1, "language": "en", "format": "json"},
            error_type=LocationResolutionError,
            operation="resolve the location",
        )
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise LocationResolutionError(f"Could not resolve location: {requested}")

        result = results[0]
        try:
            latitude = float(result["latitude"])
            longitude = float(result["longitude"])
            name = str(result["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LocationResolutionError(
                "Open-Meteo returned an incomplete location result"
            ) from exc

        return ResolvedLocation(
            requested=requested,
            name=name,
            latitude=latitude,
            longitude=longitude,
            region=result.get("admin1"),
            country=result.get("country"),
            country_code=result.get("country_code"),
            timezone=result.get("timezone"),
        )

    def get_current_weather(self, location: str) -> dict[str, Any]:
        resolved = self.resolve_location(location)
        payload = self._forecast_request(
            resolved,
            current=",".join(CURRENT_FIELDS),
            forecast_days=1,
        )
        current = payload.get("current")
        if not isinstance(current, dict):
            raise WeatherUpstreamError("Open-Meteo did not return current weather data")

        code = current.get("weather_code")
        return {
            "source": "Open-Meteo",
            "requested_location": resolved.requested,
            "resolved_location": self._location_payload(resolved, payload),
            "units": {
                "temperature": "°F",
                "humidity": "%",
                "precipitation": "inch",
                "pressure": "hPa",
                "wind_speed": "mph",
                "wind_direction": "°",
            },
            "current": {
                "time": current.get("time"),
                "temperature": current.get("temperature_2m"),
                "apparent_temperature": current.get("apparent_temperature"),
                "relative_humidity": current.get("relative_humidity_2m"),
                "precipitation": current.get("precipitation"),
                "rain": current.get("rain"),
                "showers": current.get("showers"),
                "snowfall": current.get("snowfall"),
                "weather_code": code,
                "condition": self._condition(code),
                "cloud_cover": current.get("cloud_cover"),
                "surface_pressure": current.get("surface_pressure"),
                "wind_speed": current.get("wind_speed_10m"),
                "wind_direction": current.get("wind_direction_10m"),
                "wind_gusts": current.get("wind_gusts_10m"),
            },
        }

    def get_forecast(self, location: str, days: int = 7) -> dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 16:
            raise WeatherInputError("days must be a whole number between 1 and 16")

        resolved = self.resolve_location(location)
        payload = self._forecast_request(
            resolved,
            daily=",".join(DAILY_FIELDS),
            forecast_days=days,
        )
        daily = payload.get("daily")
        if not isinstance(daily, dict) or not isinstance(daily.get("time"), list):
            raise WeatherUpstreamError("Open-Meteo did not return daily forecast data")

        forecasts = []
        for index, forecast_date in enumerate(daily["time"]):
            code = _number_at(daily, "weather_code", index)
            forecasts.append(
                {
                    "date": forecast_date,
                    "weather_code": code,
                    "condition": self._condition(code),
                    "temperature_high": _number_at(daily, "temperature_2m_max", index),
                    "temperature_low": _number_at(daily, "temperature_2m_min", index),
                    "apparent_temperature_high": _number_at(
                        daily, "apparent_temperature_max", index
                    ),
                    "apparent_temperature_low": _number_at(
                        daily, "apparent_temperature_min", index
                    ),
                    "precipitation_probability_max": _number_at(
                        daily, "precipitation_probability_max", index
                    ),
                    "precipitation": _number_at(daily, "precipitation_sum", index),
                    "rain": _number_at(daily, "rain_sum", index),
                    "showers": _number_at(daily, "showers_sum", index),
                    "snowfall": _number_at(daily, "snowfall_sum", index),
                    "wind_speed_max": _number_at(daily, "wind_speed_10m_max", index),
                    "wind_gusts_max": _number_at(daily, "wind_gusts_10m_max", index),
                    "sunrise": _number_at(daily, "sunrise", index),
                    "sunset": _number_at(daily, "sunset", index),
                }
            )

        return {
            "source": "Open-Meteo",
            "requested_location": resolved.requested,
            "resolved_location": self._location_payload(resolved, payload),
            "units": {
                "temperature": "°F",
                "precipitation_probability": "%",
                "precipitation": "inch",
                "snowfall": "inch",
                "wind_speed": "mph",
            },
            "days": len(forecasts),
            "forecast": forecasts,
        }

    def get_weather_recommendation(self, location: str, requested_date: str) -> dict[str, Any]:
        if not isinstance(requested_date, str):
            raise WeatherInputError("date must use YYYY-MM-DD format")
        try:
            normalized_date = date.fromisoformat(requested_date.strip()).isoformat()
        except ValueError as exc:
            raise WeatherInputError("date must use YYYY-MM-DD format") from exc

        forecast_response = self.get_forecast(location, days=16)
        day = next(
            (
                item
                for item in forecast_response["forecast"]
                if item["date"] == normalized_date
            ),
            None,
        )
        if day is None:
            raise WeatherInputError(
                "date is outside the available Open-Meteo forecast window"
            )

        recommendations = self._recommendations(day)
        return {
            "source": "Open-Meteo",
            "requested_location": forecast_response["requested_location"],
            "resolved_location": forecast_response["resolved_location"],
            "date": normalized_date,
            "units": forecast_response["units"],
            "condition": day["condition"],
            "recommendations": recommendations,
            "evidence": day,
        }

    def search_weather_documents(
        self,
        query: str,
        top_k: int = 5,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise WeatherInputError("query must not be blank")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise WeatherInputError("top_k must be a whole number between 1 and 20")
        if source_type is not None and source_type not in SUPPORTED_SOURCE_TYPES:
            supported = ", ".join(sorted(SUPPORTED_SOURCE_TYPES))
            raise WeatherInputError(f"source_type must be one of: {supported}")

        app_url, headers = self._resolve_day2_app()
        try:
            response = self.session.post(
                f"{app_url}/api/weather/search",
                json={
                    "query": query.strip(),
                    "top_k": top_k,
                    "source_type": source_type,
                },
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WeatherRetrievalError(
                "The Day 2 weather-document search service is unavailable"
            ) from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise WeatherRetrievalError(
                "The Day 2 weather-document search service returned an invalid response"
            )
        payload["source"] = "Day 2 Lakebase weather corpus"
        payload["corpus_scope"] = "Only documents ingested and embedded by the Day 2 pipeline"
        return payload

    def _forecast_request(self, location: ResolvedLocation, **params: Any) -> dict[str, Any]:
        request_params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "auto",
            **params,
        }
        return self._get_json(
            f"{self.forecast_base_url}/forecast",
            params=request_params,
            error_type=WeatherUpstreamError,
            operation="retrieve weather data",
        )

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        error_type: type[WeatherBrokerError],
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise error_type(f"Unable to {operation} with Open-Meteo") from exc
        if not isinstance(payload, dict):
            raise error_type(f"Open-Meteo returned an invalid response while trying to {operation}")
        return payload

    def _resolve_day2_app(self) -> tuple[str, dict[str, str]]:
        if self.weather_api_app_url:
            return self.weather_api_app_url.rstrip("/"), {}
        if not self.weather_api_app_name:
            raise WeatherRetrievalError(
                "Configure WEATHER_API_APP_NAME or WEATHER_API_APP_URL"
            )

        try:
            from databricks.sdk import WorkspaceClient

            workspace = WorkspaceClient()
            app = workspace.apps.get(self.weather_api_app_name)
            app_url = app.url
            headers = workspace.config.authenticate()
        except Exception as exc:
            raise WeatherRetrievalError(
                "Unable to resolve or authenticate to the Day 2 Databricks App"
            ) from exc
        if not app_url:
            raise WeatherRetrievalError("The Day 2 Databricks App has no serving URL")
        return app_url.rstrip("/"), dict(headers)

    @staticmethod
    def _location_payload(
        location: ResolvedLocation,
        weather_payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "name": location.name,
            "region": location.region,
            "country": location.country,
            "country_code": location.country_code,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "timezone": weather_payload.get("timezone") or location.timezone,
            "timezone_abbreviation": weather_payload.get("timezone_abbreviation"),
        }

    @staticmethod
    def _condition(code: Any) -> str:
        if isinstance(code, float) and code.is_integer():
            code = int(code)
        return WMO_CONDITIONS.get(code, "Unknown")

    @staticmethod
    def _recommendations(day: dict[str, Any]) -> list[dict[str, Any]]:
        recommendations = []

        precipitation_probability = day.get("precipitation_probability_max")
        precipitation = day.get("precipitation")
        if _at_least(precipitation_probability, 40) or _at_least(precipitation, 0.05):
            recommendations.append(
                {
                    "category": "umbrella",
                    "recommendation": "Carry an umbrella and plan for wet conditions.",
                    "evidence": {
                        "precipitation_probability_max": precipitation_probability,
                        "precipitation": precipitation,
                    },
                    "thresholds": {
                        "precipitation_probability_max": ">= 40%",
                        "precipitation": ">= 0.05 inch",
                    },
                }
            )

        temperature_low = day.get("temperature_low")
        apparent_low = day.get("apparent_temperature_low")
        if _at_most(temperature_low, 50) or _at_most(apparent_low, 50):
            recommendations.append(
                {
                    "category": "jacket",
                    "recommendation": "Bring a jacket for the cooler part of the day.",
                    "evidence": {
                        "temperature_low": temperature_low,
                        "apparent_temperature_low": apparent_low,
                    },
                    "thresholds": {
                        "temperature_low": "<= 50°F",
                        "apparent_temperature_low": "<= 50°F",
                    },
                }
            )

        temperature_high = day.get("temperature_high")
        apparent_high = day.get("apparent_temperature_high")
        if _at_least(temperature_high, 90) or _at_least(apparent_high, 95):
            recommendations.append(
                {
                    "category": "heat",
                    "recommendation": "Hydrate, seek shade, and limit strenuous outdoor activity.",
                    "evidence": {
                        "temperature_high": temperature_high,
                        "apparent_temperature_high": apparent_high,
                    },
                    "thresholds": {
                        "temperature_high": ">= 90°F",
                        "apparent_temperature_high": ">= 95°F",
                    },
                }
            )

        wind_speed = day.get("wind_speed_max")
        wind_gusts = day.get("wind_gusts_max")
        if _at_least(wind_speed, 25) or _at_least(wind_gusts, 35):
            recommendations.append(
                {
                    "category": "wind",
                    "recommendation": "Secure loose objects and use caution outdoors and while driving.",
                    "evidence": {
                        "wind_speed_max": wind_speed,
                        "wind_gusts_max": wind_gusts,
                    },
                    "thresholds": {
                        "wind_speed_max": ">= 25 mph",
                        "wind_gusts_max": ">= 35 mph",
                    },
                }
            )

        weather_code = day.get("weather_code")
        snowfall = day.get("snowfall")
        if weather_code in SNOW_OR_ICE_CODES or _at_least(snowfall, 0.01):
            recommendations.append(
                {
                    "category": "snow_or_ice",
                    "recommendation": "Allow extra travel time and watch for snow or icy surfaces.",
                    "evidence": {"weather_code": weather_code, "snowfall": snowfall},
                    "thresholds": {
                        "weather_codes": sorted(SNOW_OR_ICE_CODES),
                        "snowfall": ">= 0.01 inch",
                    },
                }
            )

        if weather_code in THUNDERSTORM_CODES:
            recommendations.append(
                {
                    "category": "thunderstorm",
                    "recommendation": "Monitor local warnings and move indoors if thunderstorms develop.",
                    "evidence": {"weather_code": weather_code},
                    "thresholds": {"weather_codes": sorted(THUNDERSTORM_CODES)},
                }
            )

        if not recommendations:
            recommendations.append(
                {
                    "category": "general",
                    "recommendation": "No threshold-based weather precautions were triggered.",
                    "evidence": {},
                    "thresholds": {},
                }
            )
        return recommendations
