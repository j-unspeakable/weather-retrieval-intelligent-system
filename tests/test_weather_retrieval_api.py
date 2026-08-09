from fastapi import HTTPException
import pytest

from app.main import app
from app.models import WeatherSourceType
from app.routers import retrieval
from app.schemas import WeatherSearchRequest, WeatherSearchResult


def result(source_type: WeatherSourceType = WeatherSourceType.ALERT):
    return WeatherSearchResult(
        document_id="urn:nws:alert:test",
        source_type=source_type,
        location="Chicago, IL",
        headline="Flood Warning",
        narrative_text="Flooding is occurring near the river.",
        chunk_text="Flooding is occurring.",
        similarity=0.91,
    )


def test_raw_retrieval_reuses_search_service_without_summary(monkeypatch):
    calls = []

    def fake_search(query, top_k, source_type):
        calls.append((query, top_k, source_type))
        return [result(source_type)]

    monkeypatch.setattr(retrieval, "search_weather", fake_search)
    payload = WeatherSearchRequest(
        query="  flood context  ",
        top_k=50,
        source_type="alert",
    )

    response = retrieval.search_weather_for_tools(payload)

    assert calls == [("flood context", 20, WeatherSourceType.ALERT)]
    assert response.query == "flood context"
    assert response.top_k == 20
    assert response.source_type is WeatherSourceType.ALERT
    assert response.summary is None
    assert response.results[0].similarity == 0.91


def test_raw_retrieval_returns_empty_results(monkeypatch):
    monkeypatch.setattr(retrieval, "search_weather", lambda *args: [])

    response = retrieval.search_weather_for_tools(
        WeatherSearchRequest(query="snow context")
    )

    assert response.summary is None
    assert response.results == []


def test_raw_retrieval_returns_safe_service_error(monkeypatch):
    def fail(*args):
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(retrieval, "search_weather", fail)

    with pytest.raises(HTTPException) as error:
        retrieval.search_weather_for_tools(WeatherSearchRequest(query="storms"))

    assert error.value.status_code == 503
    assert error.value.detail == "Unable to search weather documents right now."


def test_raw_retrieval_is_registered_at_api_path():
    operation = app.openapi()["paths"]["/api/weather/search"]
    assert set(operation) == {"post"}
    assert operation["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/WeatherSearchResponse"}
