"""Lightweight domain models used by weather ingestion."""

from dataclasses import dataclass
from typing import Any


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


@dataclass(frozen=True, slots=True)
class WeatherDocument:
    id: str
    location: str
    latitude: float | None
    longitude: float | None
    source_type: str
    headline: str | None
    narrative_text: str
    issued_at: str | None
    effective_at: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StateWeatherBatch:
    """Normalized result of one state-wide NWS ingestion pass."""

    state: str
    documents: list[WeatherDocument]
    locations: list[str]
    zones_processed: int
    stations_processed: int
