"""FastMCP entry point for live and stored weather intelligence tools."""

from functools import lru_cache
import os
from typing import Any, Literal

from fastmcp import FastMCP

try:  # Package import in repository tests; top-level module in the deployed app.
    from .weather_broker import WeatherBroker
except ImportError:  # pragma: no cover - exercised by the standalone app process
    from weather_broker import WeatherBroker


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


@mcp.tool()
def get_current_weather(location: str) -> dict[str, Any]:
    """Get current live Open-Meteo conditions for a global place or lat,lon."""
    return get_broker().get_current_weather(location)


@mcp.tool()
def get_forecast(location: str, days: int = 7) -> dict[str, Any]:
    """Get a 1-16 day live Open-Meteo forecast for a global place or lat,lon."""
    return get_broker().get_forecast(location, days)


@mcp.tool()
def get_weather_recommendation(location: str, date: str) -> dict[str, Any]:
    """Get deterministic, evidence-based precautions for an ISO forecast date."""
    return get_broker().get_weather_recommendation(location, date)


@mcp.tool()
def search_weather_documents(
    query: str,
    top_k: int = 5,
    source_type: WeatherSourceType | None = None,
) -> dict[str, Any]:
    """Semantically search stored chunks in the existing Day 2 Lakebase corpus."""
    return get_broker().search_weather_documents(query, top_k, source_type)


def main() -> None:
    """Run the MCP server with Streamable HTTP for Databricks Apps."""
    port = int(
        os.getenv("DATABRICKS_APP_PORT")
        or os.getenv("MCP_PORT", "8001")
    )
    mcp.run(transport="http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
