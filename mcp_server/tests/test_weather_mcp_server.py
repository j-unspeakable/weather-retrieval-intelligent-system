from __future__ import annotations

from fastmcp import Client
import pytest

import weather_mcp_server as server


class FakeBroker:
    def __init__(self):
        self.calls = []

    def get_current_weather(self, location):
        self.calls.append(("current", location))
        return {"kind": "current", "location": location}

    def get_forecast(self, location, days):
        self.calls.append(("forecast", location, days))
        return {"kind": "forecast", "days": days}

    def get_weather_recommendation(self, location, requested_date):
        self.calls.append(("recommendation", location, requested_date))
        return {"kind": "recommendation", "date": requested_date}

    def search_weather_documents(self, query, top_k, source_type):
        self.calls.append(("search", query, top_k, source_type))
        return {"kind": "search", "results": []}


@pytest.fixture
def fake_broker(monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(server, "get_broker", lambda: broker)
    return broker


def test_tool_functions_are_thin_broker_delegates(fake_broker):
    assert server.get_current_weather.fn("Lagos")["kind"] == "current"
    assert server.get_forecast.fn("Tokyo", 4)["kind"] == "forecast"
    assert (
        server.get_weather_recommendation.fn("Paris", "2026-08-10")["kind"]
        == "recommendation"
    )
    assert (
        server.search_weather_documents.fn("flood", 3, "alert")["kind"]
        == "search"
    )
    assert fake_broker.calls == [
        ("current", "Lagos"),
        ("forecast", "Tokyo", 4),
        ("recommendation", "Paris", "2026-08-10"),
        ("search", "flood", 3, "alert"),
    ]


@pytest.mark.asyncio
async def test_fastmcp_exposes_all_four_tools():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == {
        "get_current_weather",
        "get_forecast",
        "get_weather_recommendation",
        "search_weather_documents",
    }
    search_tool = next(tool for tool in tools if tool.name == "search_weather_documents")
    source_schema = search_tool.inputSchema["properties"]["source_type"]
    serialized_schema = str(source_schema)
    for source_type in (
        "forecast",
        "hourly_forecast",
        "zone_forecast",
        "alert",
        "observation",
    ):
        assert source_type in serialized_schema


def test_main_uses_local_mcp_port(monkeypatch):
    calls = []
    monkeypatch.delenv("DATABRICKS_APP_PORT", raising=False)
    monkeypatch.setenv("MCP_PORT", "8001")
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main()

    assert calls == [{"transport": "http", "host": "0.0.0.0", "port": 8001}]


def test_main_prefers_databricks_injected_port(monkeypatch):
    calls = []
    monkeypatch.setenv("MCP_PORT", "8001")
    monkeypatch.setenv("DATABRICKS_APP_PORT", "9000")
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main()

    assert calls == [{"transport": "http", "host": "0.0.0.0", "port": 9000}]
