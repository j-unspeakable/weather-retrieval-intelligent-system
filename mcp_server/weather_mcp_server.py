"""FastMCP entry point for live and stored weather intelligence tools."""

from collections.abc import Callable
from functools import lru_cache
import os
from typing import Any, Literal

from fastmcp import FastMCP

try:  # Package import in repository tests; top-level module in the deployed app.
    from .weather_broker import WeatherBroker, WeatherBrokerError
except ImportError:  # pragma: no cover - exercised by the standalone app process
    from weather_broker import WeatherBroker, WeatherBrokerError


WeatherSourceType = Literal[
    "forecast",
    "hourly_forecast",
    "zone_forecast",
    "alert",
    "observation",
]

mcp = FastMCP(
    "Weather Intelligence",
    instructions=(
        "Use live Open-Meteo tools for global current and future weather. "
        "Use stored document search only for context in the existing Day 2 corpus."
    ),
)


@lru_cache(maxsize=1)
def get_broker() -> WeatherBroker:
    """Return the process-wide synchronous weather broker."""
    return WeatherBroker()


def _call_broker(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Return a predictable MCP payload for expected broker failures."""
    try:
        return operation()
    except WeatherBrokerError as exc:
        return {
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            }
        }


@mcp.tool()
def get_current_weather(location: str) -> dict[str, Any]:
    """Get current live Open-Meteo conditions for a global location.

    Args:
        location: A nonblank free-form place such as ``Lagos, Nigeria`` or an
            explicit ``lat,lon`` pair with valid numeric coordinate ranges.

    Returns:
        A dictionary containing the original request, resolved city/region/
        country/coordinates/timezone, Imperial unit labels, and normalized
        temperature, humidity, precipitation, cloud, pressure, condition, and
        wind values. Expected resolution or upstream failures return
        ``{"error": {"type": ..., "message": ...}}``.
    """
    return _call_broker(lambda: get_broker().get_current_weather(location))


@mcp.tool()
def get_forecast(location: str, days: int = 7) -> dict[str, Any]:
    """Get a normalized global 1-16 day Open-Meteo forecast.

    Args:
        location: A nonblank free-form place or valid ``lat,lon`` pair.
        days: Whole-number forecast horizon from 1 through 16; defaults to 7.

    Returns:
        A dictionary with resolved location metadata, Imperial unit labels,
        and daily conditions, highs/lows, apparent highs/lows, precipitation,
        snowfall, wind, sunrise, and sunset. Expected input, resolution, or
        upstream failures use the structured ``error`` payload.
    """
    return _call_broker(lambda: get_broker().get_forecast(location, days))


@mcp.tool()
def get_weather_recommendation(location: str, date: str) -> dict[str, Any]:
    """Get deterministic, evidence-based precautions for a forecast date.

    Args:
        location: A nonblank global place or valid ``lat,lon`` pair.
        date: An ISO ``YYYY-MM-DD`` date inside Open-Meteo's forecast window.

    Returns:
        A dictionary with resolved location, condition, complete forecast
        evidence, and every triggered recommendation and threshold. Rules are:
        umbrella at >=40% precipitation probability or >=0.05 inch; jacket at
        <=50°F low/apparent low; heat precautions at >=90°F high or >=95°F
        apparent high; wind precautions at >=25 mph sustained wind or >=35 mph
        gusts; snow/ice and thunderstorm precautions from snowfall and WMO
        weather codes. Expected failures use the structured ``error`` payload.
    """
    return _call_broker(
        lambda: get_broker().get_weather_recommendation(location, date)
    )


@mcp.tool()
def search_weather_documents(
    query: str,
    top_k: int = 5,
    source_type: WeatherSourceType | None = None,
) -> dict[str, Any]:
    """Semantically search embedded chunks in the stored Day 2 weather corpus.

    Args:
        query: Nonblank natural-language semantic query.
        top_k: Whole-number result count from 1 through 20; defaults to 5.
        source_type: Optional exact filter: ``forecast``, ``hourly_forecast``,
            ``zone_forecast``, ``alert``, or ``observation``.

    Returns:
        A dictionary with the normalized query, filter, ranked matching chunks,
        similarity scores, and joined weather-document metadata. Search covers
        only rows already ingested and embedded by Day 2. Expected validation,
        authentication, or retrieval failures use the structured ``error``
        payload.
    """
    return _call_broker(
        lambda: get_broker().search_weather_documents(query, top_k, source_type)
    )


def main() -> None:
    """Run the MCP server with Streamable HTTP for Databricks Apps."""
    port = int(
        os.getenv("DATABRICKS_APP_PORT")
        or os.getenv("MCP_PORT", "8001")
    )
    mcp.run(transport="http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
