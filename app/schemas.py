"""Pydantic schemas for the public weather API."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
)

from app.models import US_STATES, WeatherSourceType


StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]
PointSourceType = Literal["forecast", "hourly_forecast", "alert", "observation"]
StateSourceType = Literal["zone_forecast", "alert", "observation"]

class WeatherSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: list[StrictStr] = Field(min_length=1)
    limit: StrictPositiveInt = 50
    source_types: list[PointSourceType] = Field(
        default_factory=lambda: ["forecast", "alert"],
        min_length=1,
    )

    @field_validator("locations")
    @classmethod
    def trim_and_validate_locations(cls, locations: list[str]) -> list[str]:
        trimmed = [location.strip() for location in locations]
        if any(not location for location in trimmed):
            raise ValueError("location entries must not be blank")
        return trimmed

    @field_validator("source_types")
    @classmethod
    def deduplicate_source_types(
        cls, source_types: list[PointSourceType]
    ) -> list[PointSourceType]:
        return list(dict.fromkeys(source_types))


class WeatherSyncResponse(BaseModel):
    synced: int
    locations: list[str]


class StateWeatherSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: StrictStr
    source_types: list[StateSourceType] = Field(
        default_factory=lambda: ["zone_forecast", "alert"],
        min_length=1,
    )
    station_limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 25

    @field_validator("state")
    @classmethod
    def normalize_state(cls, state: str) -> str:
        state = state.strip().upper()
        if state not in US_STATES:
            raise ValueError("state must be a U.S. state or DC abbreviation")
        return state

    @field_validator("source_types")
    @classmethod
    def deduplicate_source_types(
        cls, source_types: list[StateSourceType]
    ) -> list[StateSourceType]:
        return list(dict.fromkeys(source_types))


class StateWeatherSyncResponse(BaseModel):
    synced: int
    state: str
    source_types: list[StateSourceType]
    zones_processed: int
    stations_processed: int
    locations: list[str]


class WeatherSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr
    top_k: StrictInt = 5
    source_type: WeatherSourceType | None = None

    @field_validator("query")
    @classmethod
    def trim_and_validate_query(cls, query: str) -> str:
        query = query.strip()
        if not query:
            raise ValueError("query must not be blank")
        return query

    @field_validator("top_k")
    @classmethod
    def clamp_top_k(cls, top_k: int) -> int:
        return min(max(top_k, 1), 20)


class WeatherSearchResult(BaseModel):
    document_id: str
    source_type: WeatherSourceType
    location: str
    headline: str | None
    narrative_text: str
    chunk_text: str
    similarity: float


class WeatherSearchResponse(BaseModel):
    query: str
    top_k: int
    source_type: WeatherSourceType | None
    summary: str | None
    results: list[WeatherSearchResult]
