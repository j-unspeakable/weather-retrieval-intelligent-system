"""Synchronous clients and normalization for external weather APIs."""

import hashlib
import re
from collections.abc import Sequence
from typing import Any

import requests

from app.config import Settings, get_settings
from app.models import StateWeatherBatch, US_STATES, WeatherDocument


class WeatherClientError(RuntimeError):
    """Base error for weather client failures."""


class InvalidLocationError(WeatherClientError):
    """Raised when a requested location cannot be parsed or geocoded."""


class WeatherAPIError(WeatherClientError):
    """Raised when an upstream weather service fails or returns invalid data."""


_COORDINATE_RE = re.compile(
    r"^\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*"
    r"(-?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)
_CITY_STATE_RE = re.compile(r"^\s*(.+?)\s*,\s*([A-Za-z]{2})\s*$")

class WeatherClient:
    """Thin synchronous client for geocoding and api.weather.gov."""

    def __init__(
        self,
        settings: Settings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": self.settings.weather_user_agent,
                "Accept": "application/geo+json, application/json",
            }
        )

    def close(self) -> None:
        self._session.close()

    def resolve_location(self, location: str) -> tuple[float, float]:
        """Resolve either `lat,lon` or `City, ST` to rounded coordinates."""
        location = location.strip()
        coordinate_match = _COORDINATE_RE.fullmatch(location)
        if coordinate_match:
            latitude = float(coordinate_match.group(1))
            longitude = float(coordinate_match.group(2))
            return self._validate_and_round_coordinates(latitude, longitude)

        city_state_match = _CITY_STATE_RE.fullmatch(location)
        if not city_state_match:
            raise InvalidLocationError(
                f"Invalid location {location!r}; use 'City, ST' or 'lat,lon'."
            )

        city = city_state_match.group(1).strip()
        state_code = city_state_match.group(2).upper()
        state_name = US_STATES.get(state_code)
        if not city or state_name is None:
            raise InvalidLocationError(f"Unsupported U.S. location: {location!r}.")

        data = self._get_json(
            f"{self.settings.geocoding_api_base_url}/search",
            params={
                "name": city,
                "count": 10,
                "language": "en",
                "format": "json",
                "countryCode": "US",
            },
        )
        results = data.get("results", []) if isinstance(data, dict) else []
        for result in results:
            if not isinstance(result, dict):
                continue
            if str(result.get("admin1", "")).casefold() != state_name.casefold():
                continue
            try:
                latitude = float(result["latitude"])
                longitude = float(result["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            return self._validate_and_round_coordinates(latitude, longitude)

        raise InvalidLocationError(f"No geocoding result found for {location!r}.")

    def fetch_documents(
        self,
        location: str,
        source_types: Sequence[str] = ("forecast", "alert"),
    ) -> list[WeatherDocument]:
        """Fetch selected point-based documents for one requested location."""
        requested_location = location.strip()
        latitude, longitude = self.resolve_location(requested_location)
        point = f"{latitude:.4f},{longitude:.4f}"

        point_data = self._get_json(
            f"{self.settings.nws_api_base_url}/points/{point}"
        )
        properties = point_data.get("properties", {}) if isinstance(point_data, dict) else {}
        if not isinstance(properties, dict):
            raise WeatherAPIError("weather.gov returned invalid point data.")

        enabled = set(source_types)
        documents_by_type: dict[str, list[WeatherDocument]] = {
            "forecast": [],
            "hourly_forecast": [],
            "observation": [],
            "alert": [],
        }

        if "forecast" in enabled:
            forecast_url = self._required_url(properties, "forecast")
            documents_by_type["forecast"] = self._normalize_forecast_periods(
                requested_location,
                latitude,
                longitude,
                self._get_json(forecast_url),
                "forecast",
            )

        if "hourly_forecast" in enabled:
            hourly_url = self._required_url(properties, "forecastHourly")
            documents_by_type["hourly_forecast"] = self._normalize_forecast_periods(
                requested_location,
                latitude,
                longitude,
                self._get_json(hourly_url),
                "hourly_forecast",
            )

        if "observation" in enabled:
            station_url = self._required_url(properties, "observationStations")
            station_data = self._get_json(station_url)
            observation = self._fetch_latest_observation(
                self._first_station(station_data),
                requested_location=requested_location,
                fallback_state=None,
            )
            if observation is None:
                raise WeatherAPIError(
                    "The nearest weather station has no current observation."
                )
            documents_by_type["observation"] = [observation]

        if "alert" in enabled:
            alert_data = self._get_json(
                f"{self.settings.nws_api_base_url}/alerts/active",
                params={"point": point},
            )
            documents_by_type["alert"] = self._normalize_alerts(
                requested_location,
                latitude,
                longitude,
                alert_data,
            )

        return [
            document
            for source_type in (
                "forecast",
                "hourly_forecast",
                "observation",
                "alert",
            )
            for document in documents_by_type[source_type]
        ]

    def fetch_state_documents(
        self,
        state: str,
        source_types: Sequence[str] = ("zone_forecast", "alert"),
        station_limit: int = 25,
    ) -> StateWeatherBatch:
        """Fetch selected document types across one U.S. state."""
        state = state.strip().upper()
        state_name = US_STATES.get(state)
        if state_name is None:
            raise InvalidLocationError(f"Unsupported U.S. state: {state!r}.")

        enabled = set(source_types)
        zone_documents: list[WeatherDocument] = []
        observation_documents: list[WeatherDocument] = []
        alert_documents: list[WeatherDocument] = []
        locations: list[str] = []
        zones_processed = 0
        stations_processed = 0

        if "zone_forecast" in enabled:
            zone_data = self._get_json(
                f"{self.settings.nws_api_base_url}/zones",
                params={
                    "area": state,
                    "type": "forecast",
                    "include_geometry": "false",
                    "limit": 500,
                },
            )
            for zone in self._features(zone_data, "forecast zone"):
                zone_id = self._feature_identifier(zone, "forecast zone")
                properties = zone.get("properties", {})
                zone_name = (
                    str(properties.get("name") or zone_id)
                    if isinstance(properties, dict)
                    else zone_id
                )
                zone_location = f"{zone_name}, {state}"
                forecast_data = self._get_json(
                    f"{self.settings.nws_api_base_url}/zones/forecast/{zone_id}/forecast"
                )
                zone_documents.extend(
                    self._normalize_zone_forecast(
                        zone_location,
                        zone_id,
                        forecast_data,
                    )
                )
                locations.append(zone_location)
                zones_processed += 1

        if "observation" in enabled:
            station_data = self._get_json(
                f"{self.settings.nws_api_base_url}/stations",
                params={"state": state, "limit": station_limit},
            )
            for station in self._features(station_data, "observation station")[
                :station_limit
            ]:
                document = self._fetch_latest_observation(
                    station,
                    requested_location=None,
                    fallback_state=state,
                    allow_missing=True,
                )
                if document is None:
                    continue
                observation_documents.append(document)
                locations.append(document.location)
                stations_processed += 1

        if "alert" in enabled:
            alert_data = self._get_json(
                f"{self.settings.nws_api_base_url}/alerts/active",
                params={"area": state},
            )
            alert_documents = self._normalize_alerts(
                state_name,
                None,
                None,
                alert_data,
            )
            locations.append(state_name)

        return StateWeatherBatch(
            state=state,
            documents=zone_documents + observation_documents + alert_documents,
            locations=list(dict.fromkeys(locations)),
            zones_processed=zones_processed,
            stations_processed=stations_processed,
        )

    def _normalize_documents(
        self,
        location: str,
        latitude: float,
        longitude: float,
        forecast_data: dict[str, Any],
        alert_data: dict[str, Any],
    ) -> list[WeatherDocument]:
        """Normalize the original forecast-plus-alert contract."""
        return self._normalize_forecast_periods(
            location,
            latitude,
            longitude,
            forecast_data,
            "forecast",
        ) + self._normalize_alerts(
            location,
            latitude,
            longitude,
            alert_data,
        )

    def _normalize_forecast_periods(
        self,
        location: str,
        latitude: float,
        longitude: float,
        forecast_data: dict[str, Any],
        source_type: str,
    ) -> list[WeatherDocument]:
        forecast_properties = forecast_data.get("properties", {})
        if not isinstance(forecast_properties, dict):
            raise WeatherAPIError("weather.gov returned invalid forecast data.")
        generated_at = forecast_properties.get("generatedAt")
        periods = forecast_properties.get("periods", [])
        if not isinstance(periods, list):
            raise WeatherAPIError("weather.gov returned invalid forecast periods.")

        forecasts: list[WeatherDocument] = []
        for period in periods:
            if not isinstance(period, dict):
                raise WeatherAPIError("weather.gov returned an invalid forecast period.")
            name = str(period.get("name") or "")
            start_time = period.get("startTime")
            identity = f"{latitude:.4f},{longitude:.4f}|{start_time or ''}|{name}"
            document_id = (
                f"{source_type}:"
                f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
            )
            narrative = str(
                period.get("detailedForecast")
                or period.get("shortForecast")
                or "Forecast details unavailable."
            ).strip()
            forecasts.append(
                WeatherDocument(
                    id=document_id,
                    location=location,
                    latitude=latitude,
                    longitude=longitude,
                    source_type=source_type,
                    headline=name or (str(start_time) if start_time else None),
                    narrative_text=narrative,
                    issued_at=str(generated_at) if generated_at else None,
                    effective_at=str(start_time) if start_time else None,
                    payload=period,
                )
            )

        forecasts.sort(
            key=lambda document: (
                document.effective_at or "",
                document.headline or "",
                document.id,
            )
        )
        return forecasts

    def _normalize_alerts(
        self,
        location: str,
        latitude: float | None,
        longitude: float | None,
        alert_data: dict[str, Any],
    ) -> list[WeatherDocument]:
        alerts: list[WeatherDocument] = []
        for feature in self._features(alert_data, "alert"):
            properties = feature.get("properties", {})
            if not isinstance(properties, dict):
                raise WeatherAPIError("weather.gov returned invalid alert properties.")
            alert_id = properties.get("id")
            if not isinstance(alert_id, str) or not alert_id.strip():
                raise WeatherAPIError("weather.gov returned an alert without a source ID.")

            description = str(properties.get("description") or "").strip()
            instruction = str(properties.get("instruction") or "").strip()
            narrative = "\n\n".join(
                part for part in (description, instruction) if part
            )
            headline = properties.get("headline") or properties.get("event")
            alerts.append(
                WeatherDocument(
                    id=alert_id,
                    location=location,
                    latitude=latitude,
                    longitude=longitude,
                    source_type="alert",
                    headline=str(headline) if headline else None,
                    narrative_text=narrative,
                    issued_at=(
                        str(properties["sent"]) if properties.get("sent") else None
                    ),
                    effective_at=(
                        str(properties["effective"])
                        if properties.get("effective")
                        else None
                    ),
                    payload=feature,
                )
            )

        alerts.sort(
            key=lambda document: (
                document.effective_at or document.issued_at or "",
                document.id,
            )
        )
        return alerts

    def _normalize_zone_forecast(
        self,
        location: str,
        zone_id: str,
        forecast_data: dict[str, Any],
    ) -> list[WeatherDocument]:
        properties = forecast_data.get("properties", {})
        if not isinstance(properties, dict):
            raise WeatherAPIError("weather.gov returned invalid zone forecast data.")
        updated = properties.get("updated")
        periods = properties.get("periods", [])
        if not isinstance(periods, list):
            raise WeatherAPIError("weather.gov returned invalid zone forecast periods.")

        documents: list[WeatherDocument] = []
        for period in periods:
            if not isinstance(period, dict):
                raise WeatherAPIError("weather.gov returned an invalid zone period.")
            number = period.get("number")
            name = str(period.get("name") or "")
            identity = f"{zone_id}|{updated or ''}|{number or ''}|{name}"
            documents.append(
                WeatherDocument(
                    id=(
                        "zone_forecast:"
                        f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
                    ),
                    location=location,
                    latitude=None,
                    longitude=None,
                    source_type="zone_forecast",
                    headline=name or None,
                    narrative_text=str(
                        period.get("detailedForecast")
                        or "Forecast details unavailable."
                    ).strip(),
                    issued_at=str(updated) if updated else None,
                    effective_at=None,
                    payload=period,
                )
            )
        documents.sort(
            key=lambda document: (
                document.payload.get("number", 0),
                document.headline or "",
                document.id,
            )
        )
        return documents

    def _fetch_latest_observation(
        self,
        station: dict[str, Any],
        requested_location: str | None,
        fallback_state: str | None,
        allow_missing: bool = False,
    ) -> WeatherDocument | None:
        station_id = self._feature_identifier(station, "observation station")
        station_properties = station.get("properties", {})
        station_name = station_id
        if isinstance(station_properties, dict):
            station_name = str(station_properties.get("name") or station_id)
        location = requested_location or (
            f"{station_name}, {fallback_state}" if fallback_state else station_name
        )
        observation = self._get_json(
            f"{self.settings.nws_api_base_url}/stations/{station_id}/observations/latest",
            allow_not_found=allow_missing,
        )
        if observation is None:
            return None
        if not isinstance(observation, dict):
            raise WeatherAPIError("weather.gov returned invalid observation data.")
        properties = observation.get("properties", {})
        if not isinstance(properties, dict):
            raise WeatherAPIError("weather.gov returned invalid observation properties.")
        timestamp = properties.get("timestamp")
        if not timestamp:
            raise WeatherAPIError("weather.gov returned an observation without a timestamp.")

        coordinates = self._feature_coordinates(observation) or self._feature_coordinates(
            station
        )
        latitude, longitude = coordinates if coordinates else (None, None)
        narrative = self._observation_narrative(properties, station_name)
        return WeatherDocument(
            id=f"observation:{station_id}:{timestamp}",
            location=location,
            latitude=latitude,
            longitude=longitude,
            source_type="observation",
            headline=f"Latest observation at {station_name}",
            narrative_text=narrative,
            issued_at=str(timestamp),
            effective_at=str(timestamp),
            payload=observation,
        )

    @staticmethod
    def _observation_narrative(
        properties: dict[str, Any], station_name: str
    ) -> str:
        parts: list[str] = []
        description = str(properties.get("textDescription") or "").strip()
        if description:
            parts.append(description.rstrip(".") + ".")
        for label, key in (
            ("Temperature", "temperature"),
            ("Relative humidity", "relativeHumidity"),
            ("Wind direction", "windDirection"),
            ("Wind speed", "windSpeed"),
            ("Wind gust", "windGust"),
            ("Visibility", "visibility"),
        ):
            measurement = properties.get(key)
            if not isinstance(measurement, dict) or measurement.get("value") is None:
                continue
            value = measurement["value"]
            if isinstance(value, float):
                value = round(value, 1)
            unit_code = str(measurement.get("unitCode") or "").split(":")[-1]
            unit = {
                "degC": "°C",
                "percent": "%",
                "degree_(angle)": "°",
                "km_h-1": "km/h",
                "m": "m",
                "Pa": "Pa",
            }.get(unit_code, unit_code)
            parts.append(f"{label}: {value}{f' {unit}' if unit else ''}.")
        return " ".join(parts) or f"Latest observation reported by {station_name}."

    @staticmethod
    def _required_url(properties: dict[str, Any], key: str) -> str:
        value = properties.get(key)
        if not isinstance(value, str) or not value:
            raise WeatherAPIError(
                f"weather.gov point response did not contain {key}."
            )
        return value

    @staticmethod
    def _features(data: Any, label: str) -> list[dict[str, Any]]:
        features = data.get("features", []) if isinstance(data, dict) else []
        if not isinstance(features, list) or any(
            not isinstance(feature, dict) for feature in features
        ):
            raise WeatherAPIError(f"weather.gov returned invalid {label} data.")
        return features

    def _first_station(self, station_data: Any) -> dict[str, Any]:
        stations = self._features(station_data, "observation station")
        if not stations:
            raise WeatherAPIError("weather.gov returned no observation stations.")
        return stations[0]

    @staticmethod
    def _feature_identifier(feature: dict[str, Any], label: str) -> str:
        properties = feature.get("properties", {})
        candidates = []
        if isinstance(properties, dict):
            candidates.extend((properties.get("stationIdentifier"), properties.get("id")))
        candidates.extend((feature.get("id"), feature.get("@id")))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.rstrip("/").rsplit("/", 1)[-1]
        raise WeatherAPIError(f"weather.gov returned a {label} without an ID.")

    @classmethod
    def _feature_coordinates(
        cls, feature: dict[str, Any]
    ) -> tuple[float, float] | None:
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            return None
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            return None
        try:
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
        except (TypeError, ValueError):
            return None
        try:
            return cls._validate_and_round_coordinates(latitude, longitude)
        except InvalidLocationError:
            return None

    def _get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        try:
            response = self._session.get(
                url,
                params=params,
                timeout=self.settings.weather_request_timeout,
            )
            if allow_not_found and getattr(response, "status_code", None) == 404:
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise WeatherAPIError("An upstream weather request failed.") from exc
        except ValueError as exc:
            raise WeatherAPIError("An upstream weather service returned invalid JSON.") from exc

    @staticmethod
    def _validate_and_round_coordinates(
        latitude: float,
        longitude: float,
    ) -> tuple[float, float]:
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise InvalidLocationError("Latitude or longitude is outside its valid range.")
        return round(latitude, 4), round(longitude, 4)
