from contextlib import contextmanager
from decimal import Decimal
from types import ModuleType, SimpleNamespace
import math
import sys

from fastapi import HTTPException
from pydantic import SecretStr, ValidationError
import pytest
from starlette.requests import Request

from app import database
from app.main import app
from app.models import US_STATES, WeatherSourceType
from app.routers import weather as weather_router
from app.schemas import WeatherSearchRequest, WeatherSearchResponse, WeatherSearchResult
from app.services import embeddings, llm, weather as weather_service


SOURCE_VALUES = [source_type.value for source_type in WeatherSourceType]


def search_row(source_type: str = "alert") -> dict:
    return {
        "document_id": "urn:nws:alert:test",
        "source_type": source_type,
        "location": "Chicago, IL",
        "headline": "Severe Thunderstorm Warning",
        "narrative_text": "Severe thunderstorms are expected. Seek shelter.",
        "chunk_text": "Severe thunderstorms are expected.",
        "similarity": 0.875,
    }


@pytest.mark.parametrize("query", ["", " ", "\n\t"])
def test_search_schema_rejects_blank_queries(query):
    with pytest.raises(ValidationError):
        WeatherSearchRequest(query=query)


def test_search_schema_trims_query_defaults_and_clamps_top_k():
    default = WeatherSearchRequest(query="  severe storms  ")
    low = WeatherSearchRequest(query="storms", top_k=-10)
    high = WeatherSearchRequest(query="storms", top_k=500)

    assert default.query == "severe storms"
    assert default.top_k == 5
    assert low.top_k == 1
    assert high.top_k == 20


@pytest.mark.parametrize("top_k", [True, "5", 1.5, None])
def test_search_schema_rejects_non_integer_top_k(top_k):
    with pytest.raises(ValidationError):
        WeatherSearchRequest(query="storms", top_k=top_k)


def test_search_schema_rejects_extra_fields_and_unknown_source():
    with pytest.raises(ValidationError):
        WeatherSearchRequest.model_validate({"query": "storms", "extra": True})
    with pytest.raises(ValidationError):
        WeatherSearchRequest(query="storms", source_type="radar")


@pytest.mark.parametrize("source_type", list(WeatherSourceType))
def test_search_schema_accepts_every_shared_source_type(source_type):
    payload = WeatherSearchRequest(query="weather risk", source_type=source_type.value)
    assert payload.source_type is source_type


class FakeEmbeddingModel:
    def __init__(self, dimension=embeddings.EMBEDDING_DIM, vector=None):
        self.dimension = dimension
        self.vector = vector or [0.25] * embeddings.EMBEDDING_DIM
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def encode(self, sentences, **kwargs):
        self.calls.append((sentences, kwargs))
        return [self.vector]


def install_fake_sentence_transformers(monkeypatch, factory):
    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = factory
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    embeddings.get_embedding_model.cache_clear()


def test_embedding_model_is_loaded_once_and_query_is_embedded(monkeypatch):
    constructed = []
    model = FakeEmbeddingModel()

    def factory(model_name):
        constructed.append(model_name)
        return model

    install_fake_sentence_transformers(monkeypatch, factory)
    try:
        first = embeddings.embed_query("storm risk")
        second = embeddings.embed_query("flood risk")
    finally:
        embeddings.get_embedding_model.cache_clear()

    assert constructed == [embeddings.EMBEDDING_MODEL_NAME]
    assert len(first) == len(second) == embeddings.EMBEDDING_DIM
    assert model.calls == [
        (
            ["storm risk"],
            {
                "batch_size": 1,
                "show_progress_bar": False,
                "convert_to_numpy": True,
            },
        ),
        (
            ["flood risk"],
            {
                "batch_size": 1,
                "show_progress_bar": False,
                "convert_to_numpy": True,
            },
        ),
    ]


def test_embedding_rejects_wrong_model_dimension(monkeypatch):
    install_fake_sentence_transformers(
        monkeypatch, lambda model_name: FakeEmbeddingModel(dimension=128)
    )
    try:
        with pytest.raises(embeddings.EmbeddingError, match="384-dimensional"):
            embeddings.get_embedding_model()
    finally:
        embeddings.get_embedding_model.cache_clear()


@pytest.mark.parametrize(
    "vector",
    [
        [0.1] * (embeddings.EMBEDDING_DIM - 1),
        [math.nan] + [0.1] * (embeddings.EMBEDDING_DIM - 1),
        [math.inf] + [0.1] * (embeddings.EMBEDDING_DIM - 1),
    ],
)
def test_embedding_rejects_invalid_generated_vectors(monkeypatch, vector):
    install_fake_sentence_transformers(
        monkeypatch, lambda model_name: FakeEmbeddingModel(vector=vector)
    )
    try:
        with pytest.raises(embeddings.EmbeddingError):
            embeddings.embed_query("storm")
    finally:
        embeddings.get_embedding_model.cache_clear()


def test_pgvector_serialization():
    vector = [0.0] * embeddings.EMBEDDING_DIM
    vector[0] = 0.123456789
    vector[-1] = -0.5

    serialized = embeddings.serialize_pgvector(vector)

    assert serialized.startswith("[0.123456789,")
    assert serialized.endswith(",-0.5]")
    assert serialized.count(",") == embeddings.EMBEDDING_DIM - 1


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.fake_cursor = FakeCursor(rows)

    def cursor(self):
        return self.fake_cursor


def connection_context(connection):
    @contextmanager
    def fake_get_connection():
        yield connection

    return fake_get_connection


def test_database_search_uses_vector_join_model_and_source_filter(monkeypatch):
    row = search_row("zone_forecast")
    row["similarity"] = Decimal("0.8125")
    connection = FakeConnection([row])
    monkeypatch.setattr(database, "get_connection", connection_context(connection))

    result = database.search_weather_embeddings(
        query_vector="[0.1,0.2]",
        model_name=embeddings.EMBEDDING_MODEL_NAME,
        top_k=7,
        source_type="zone_forecast",
    )

    sql, params = connection.fake_cursor.executions[0]
    assert sql.count("e.embedding <=> %s::vector") == 2
    assert "JOIN weather_documents AS d" in sql
    assert "ON e.document_id = d.id" in sql
    assert "e.model_name = %s" in sql
    assert "d.source_type = %s" in sql
    assert params == (
        "[0.1,0.2]",
        embeddings.EMBEDDING_MODEL_NAME,
        "zone_forecast",
        "zone_forecast",
        "[0.1,0.2]",
        7,
    )
    assert result[0]["similarity"] == 0.8125


def test_database_search_returns_empty_list(monkeypatch):
    connection = FakeConnection([])
    monkeypatch.setattr(database, "get_connection", connection_context(connection))
    assert database.search_weather_embeddings("[0]", "model", 5) == []


@pytest.mark.parametrize("source_type", list(WeatherSourceType))
def test_search_service_uses_embedding_and_forwards_each_source(monkeypatch, source_type):
    captured = {}
    monkeypatch.setattr(weather_service, "embed_query", lambda query: [0.1] * 384)
    monkeypatch.setattr(
        weather_service, "serialize_pgvector", lambda vector: "[serialized]"
    )

    def fake_search(**kwargs):
        captured.update(kwargs)
        return [search_row(source_type.value)]

    monkeypatch.setattr(database, "search_weather_embeddings", fake_search)

    results = weather_service.search_weather("weather risk", 4, source_type)

    assert captured == {
        "query_vector": "[serialized]",
        "model_name": embeddings.EMBEDDING_MODEL_NAME,
        "top_k": 4,
        "source_type": source_type.value,
    }
    assert results[0].source_type is source_type


def test_search_service_preserves_empty_results(monkeypatch):
    monkeypatch.setattr(weather_service, "embed_query", lambda query: [0.1] * 384)
    monkeypatch.setattr(
        weather_service, "serialize_pgvector", lambda vector: "[serialized]"
    )
    monkeypatch.setattr(database, "search_weather_embeddings", lambda **kwargs: [])
    assert weather_service.search_weather("weather risk", 5) == []


@pytest.mark.parametrize("source_type", list(WeatherSourceType))
def test_post_and_get_search_support_every_source(monkeypatch, source_type):
    calls = []

    def fake_answer(query, top_k, selected_source):
        calls.append((query, top_k, selected_source))
        return WeatherSearchResponse(
            query=query,
            top_k=top_k,
            source_type=selected_source,
            summary="Grounded summary [1].",
            results=[WeatherSearchResult.model_validate(search_row(source_type.value))],
        )

    monkeypatch.setattr(weather_router, "answer_weather_query", fake_answer)
    post_response = weather_router.search_weather_json(
        WeatherSearchRequest(
            query="  weather risk  ", top_k=30, source_type=source_type
        )
    )
    get_response = weather_router.search_weather_json_get(
        query=" weather risk ", top_k=0, source_type=source_type
    )

    assert post_response.top_k == 20
    assert post_response.source_type is source_type
    assert get_response.top_k == 1
    assert get_response.source_type is source_type
    assert get_response.summary == "Grounded summary [1]."
    assert calls == [
        ("weather risk", 20, source_type),
        ("weather risk", 1, source_type),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "weather", "top_k": True},
        {"query": "weather", "top_k": "5"},
        {"query": "weather", "top_k": 1.5},
        {"query": "weather", "source_type": "radar"},
        {"query": "weather", "extra": True},
    ],
)
def test_post_search_schema_rejects_invalid_json(payload):
    with pytest.raises(ValidationError):
        WeatherSearchRequest.model_validate(payload)


def test_get_search_maps_blank_query_to_422():
    with pytest.raises(HTTPException) as error:
        weather_router.search_weather_json_get(query="   ", top_k=5)
    assert error.value.status_code == 422


def test_json_search_returns_safe_service_error(monkeypatch):
    monkeypatch.setattr(
        weather_router,
        "answer_weather_query",
        lambda *args: (_ for _ in ()).throw(RuntimeError("secret connection details")),
    )

    with pytest.raises(HTTPException) as error:
        weather_router.search_weather_json(WeatherSearchRequest(query="storms"))

    assert error.value.status_code == 503
    assert error.value.detail == "Unable to search weather documents right now."


class FakeTemplates:
    def TemplateResponse(self, *, request, name, context):
        return {"request": request, "name": name, "context": context}


def fake_template_request():
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(templates=FakeTemplates()))
    )


def stub_page_database(monkeypatch):
    monkeypatch.setattr(database, "ensure_weather_table", lambda: None)
    monkeypatch.setattr(
        database,
        "get_weather_summary",
        lambda: {
            "total_documents": 0,
            "total_embeddings": 0,
            "total_locations": 0,
            "source_type_count": 0,
            "last_synced_at": None,
            "source_counts": {},
        },
    )


def test_search_form_calls_shared_service_and_preserves_values(monkeypatch):
    stub_page_database(monkeypatch)
    captured = {}
    result = WeatherSearchResult.model_validate(search_row("hourly_forecast"))

    def fake_answer(query, top_k, source_type):
        captured.update(query=query, top_k=top_k, source_type=source_type)
        return WeatherSearchResponse(
            query=query,
            top_k=top_k,
            source_type=source_type,
            summary="Heavy rain is possible [1].",
            results=[result],
        )

    monkeypatch.setattr(weather_router, "answer_weather_query", fake_answer)

    response = weather_router.search_weather_form(
        fake_template_request(),
        query="  heavy rain  ",
        top_k="100",
        source_type="hourly_forecast",
    )

    context = response["context"]
    assert captured == {
        "query": "heavy rain",
        "top_k": 20,
        "source_type": WeatherSourceType.HOURLY_FORECAST,
    }
    assert context["search_query"] == "heavy rain"
    assert context["search_top_k"] == 20
    assert context["search_source_type"] == "hourly_forecast"
    assert context["search_summary"] == "Heavy rain is possible [1]."
    assert context["search_results"] == [result]
    assert context["search_performed"] is True
    assert context["search_source_types"] == list(WeatherSourceType)


def test_search_form_renders_validation_and_empty_feedback(monkeypatch):
    stub_page_database(monkeypatch)
    monkeypatch.setattr(
        weather_router,
        "answer_weather_query",
        lambda *args: pytest.fail("invalid form must not call search service"),
    )
    invalid = weather_router.search_weather_form(
        fake_template_request(), query=" ", top_k="5", source_type=""
    )
    assert invalid["context"]["search_error"]

    monkeypatch.setattr(
        weather_router,
        "answer_weather_query",
        lambda query, top_k, source_type: WeatherSearchResponse(
            query=query,
            top_k=top_k,
            source_type=source_type,
            summary=None,
            results=[],
        ),
    )
    empty = weather_router.search_weather_form(
        fake_template_request(), query="snow", top_k="5", source_type=""
    )
    assert empty["context"]["search_performed"] is True
    assert empty["context"]["search_results"] == []


def test_weather_template_contains_ingestion_search_sources_and_results():
    template = app.state.templates.get_template("weather/index.html")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/weather",
            "raw_path": b"/weather",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": app,
        }
    )
    result = WeatherSearchResult.model_validate(search_row("observation"))

    html = template.render(
        request=request,
        synced=None,
        error=None,
        default_limit=50,
        states=US_STATES,
        summary={
            "total_documents": 1,
            "total_embeddings": 2,
            "total_locations": 1,
            "source_type_count": 1,
            "last_synced_at": "2026-08-08T10:00:00+00:00",
            "source_counts": {"observation": 1},
        },
        search_source_types=list(WeatherSourceType),
        search_query="storm risk",
        search_top_k=5,
        search_source_type="observation",
        search_summary="Severe storms may affect Chicago [1].",
        search_results=[result],
        search_performed=True,
        search_error=None,
    )

    assert "Sync selected states" in html
    assert "Sync precise locations" in html
    assert "Semantic Weather Search" in html
    for source_type in SOURCE_VALUES:
        assert f'value="{source_type}"' in html
    assert "0.875 similarity" in html
    assert "Chicago, IL" in html
    assert "Severe thunderstorms are expected." in html
    assert "Severe storms may affect Chicago [1]." in html
    assert "Grounded answer" in html
    assert "Result 1" in html
    assert "<details>" in html


def test_answer_weather_query_retrieves_then_summarizes(monkeypatch):
    result = WeatherSearchResult.model_validate(search_row("forecast"))
    calls = []
    monkeypatch.setattr(
        weather_service,
        "search_weather",
        lambda query, top_k, source_type: calls.append(
            ("search", query, top_k, source_type)
        )
        or [result],
    )
    monkeypatch.setattr(
        weather_service,
        "generate_weather_summary",
        lambda query, results: calls.append(("summary", query, results))
        or "Storm risk is elevated [1].",
    )

    response = weather_service.answer_weather_query(
        "storm risk",
        5,
        WeatherSourceType.FORECAST,
    )

    assert response.summary == "Storm risk is elevated [1]."
    assert response.results == [result]
    assert calls == [
        ("search", "storm risk", 5, WeatherSourceType.FORECAST),
        ("summary", "storm risk", [result]),
    ]


def test_llm_summary_skips_empty_results(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_http_session",
        lambda: pytest.fail("empty retrieval must not invoke OpenRouter"),
    )
    assert llm.generate_weather_summary("storm risk", []) is None


def test_llm_summary_requires_api_key_for_nonempty_results(monkeypatch):
    result = WeatherSearchResult.model_validate(search_row())
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(llm_api_key=None),
    )

    with pytest.raises(llm.WeatherSummaryError, match="LLM_API_KEY"):
        llm.generate_weather_summary("storm risk", [result])


def test_llm_summary_calls_openrouter_with_grounded_prompt(monkeypatch):
    result = WeatherSearchResult.model_validate(search_row("alert"))
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "  Seek shelter [1].  "}}
                ]
            }

    class FakeSession:
        def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            llm_api_base_url="https://openrouter.test/api/v1",
            llm_api_key=SecretStr("test-secret"),
            llm_model_name="openrouter/free",
            llm_request_timeout=45,
        ),
    )
    monkeypatch.setattr(llm, "get_http_session", lambda: FakeSession())

    summary = llm.generate_weather_summary("What is the risk?", [result])

    assert summary == "Seek shelter [1]."
    assert captured["url"] == "https://openrouter.test/api/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer test-secret",
        "Content-Type": "application/json",
    }
    assert captured["timeout"] == 45
    assert captured["json"]["model"] == "openrouter/free"
    assert captured["json"]["max_tokens"] == llm.SUMMARY_MAX_TOKENS
    assert captured["json"]["temperature"] == llm.SUMMARY_TEMPERATURE
    assert len(captured["json"]["messages"]) == 2
    assert captured["json"]["messages"][0]["role"] == "system"
    assert "Use only the supplied retrieved context" in (
        captured["json"]["messages"][0]["content"]
    )
    user_prompt = captured["json"]["messages"][1]["content"]
    assert "User question: What is the risk?" in user_prompt
    assert "[1] Location: Chicago, IL" in user_prompt
    assert "Retrieved text: Severe thunderstorms are expected." in user_prompt
    assert "Seek shelter." not in user_prompt


def test_llm_summary_rejects_empty_model_output(monkeypatch):
    result = WeatherSearchResult.model_validate(search_row())

    class EmptyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "  "}}]}

    session = SimpleNamespace(post=lambda *args, **kwargs: EmptyResponse())
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: SimpleNamespace(
            llm_api_base_url="https://openrouter.test/api/v1",
            llm_api_key=SecretStr("test-secret"),
            llm_model_name="openrouter/free",
            llm_request_timeout=45,
        ),
    )
    monkeypatch.setattr(llm, "get_http_session", lambda: session)

    with pytest.raises(llm.WeatherSummaryError, match="returned no text"):
        llm.generate_weather_summary("storm risk", [result])


def test_llm_summary_wraps_openrouter_http_errors_without_logging_key(
    monkeypatch,
    caplog,
):
    result = WeatherSearchResult.model_validate(search_row())
    settings = SimpleNamespace(
        llm_api_base_url="https://openrouter.test/api/v1",
        llm_api_key=SecretStr("do-not-log-this-key"),
        llm_model_name="openrouter/free",
        llm_request_timeout=45,
    )

    class FailedResponse:
        status_code = 429

        def raise_for_status(self):
            raise llm.requests.HTTPError("rate limited", response=self)

    session = SimpleNamespace(post=lambda *args, **kwargs: FailedResponse())
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    monkeypatch.setattr(llm, "get_http_session", lambda: session)

    with pytest.raises(llm.WeatherSummaryError, match="API is unavailable"):
        llm.generate_weather_summary("storm risk", [result])

    assert "status=429" in caplog.text
    assert "do-not-log-this-key" not in caplog.text
