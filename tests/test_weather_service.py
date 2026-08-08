from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from psycopg2.extras import Json

from app import database
from app.services import weather as weather_service


class FakeWeatherClient:
    def __init__(self, documents_by_location, failure=None):
        self.documents_by_location = documents_by_location
        self.failure = failure
        self.calls = []

    def fetch_documents(self, location, source_types=("forecast", "alert")):
        self.calls.append(location)
        if self.failure and location == self.failure[0]:
            raise self.failure[1]
        return self.documents_by_location[location]

    def fetch_state_documents(self, state, source_types, station_limit):
        self.calls.append((state, list(source_types), station_limit))
        return self.documents_by_location[state]


class FakeCursor:
    def __init__(self, rows=None):
        self.executions = []
        self.rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=None):
        self.fake_cursor = FakeCursor(rows)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def connection_context(connection):
    @contextmanager
    def fake_get_connection():
        yield connection

    return fake_get_connection


def oauth_settings(**overrides):
    values = {
        "app_env": "local",
        "pg_host": "ep-test.database.databricks.com",
        "pg_database": "databricks_postgres",
        "pg_user": "user@example.com",
        "pg_port": 5432,
        "pg_sslmode": "require",
        "endpoint_name": "projects/p/branches/b/endpoints/e",
        "databricks_config_profile": "weather-dev",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_get_connection_generates_fresh_oauth_token(monkeypatch):
    generated_endpoints = []
    connect_calls = []
    connections = []

    class FakePostgresAPI:
        def generate_database_credential(self, *, endpoint):
            generated_endpoints.append(endpoint)
            return SimpleNamespace(token=f"oauth-token-{len(generated_endpoints)}")

    workspace = SimpleNamespace(postgres=FakePostgresAPI())

    def fake_connect(**kwargs):
        connection = FakeConnection()
        connections.append(connection)
        connect_calls.append(kwargs)
        return connection

    monkeypatch.setattr(database, "get_settings", oauth_settings)
    monkeypatch.setattr(database, "_workspace_client", lambda: workspace)
    monkeypatch.setattr(database.psycopg2, "connect", fake_connect)

    with database.get_connection():
        pass
    with database.get_connection():
        pass

    assert generated_endpoints == [
        "projects/p/branches/b/endpoints/e",
        "projects/p/branches/b/endpoints/e",
    ]
    assert [call["password"] for call in connect_calls] == [
        "oauth-token-1",
        "oauth-token-2",
    ]
    assert connect_calls[0]["host"] == "ep-test.database.databricks.com"
    assert connect_calls[0]["port"] == 5432
    assert connect_calls[0]["dbname"] == "databricks_postgres"
    assert connect_calls[0]["user"] == "user@example.com"
    assert connect_calls[0]["sslmode"] == "require"
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize(
    ("app_env", "configured_profile", "expected_profile"),
    [
        ("local", "weather-dev", "weather-dev"),
        ("databricks", "ignored-profile", None),
        ("test", "ignored-profile", None),
    ],
)
def test_workspace_client_selects_identity_source(
    monkeypatch, app_env, configured_profile, expected_profile
):
    profiles = []

    monkeypatch.setattr(
        database,
        "get_settings",
        lambda: oauth_settings(
            app_env=app_env,
            databricks_config_profile=configured_profile,
        ),
    )
    monkeypatch.setattr(
        database,
        "WorkspaceClient",
        lambda *, profile: profiles.append(profile) or SimpleNamespace(),
    )
    database._workspace_client.cache_clear()

    first = database._workspace_client()
    second = database._workspace_client()

    assert first is second
    assert profiles == [expected_profile]
    database._workspace_client.cache_clear()


def test_get_connection_reports_missing_oauth_configuration(monkeypatch):
    monkeypatch.setattr(database, "get_settings", lambda: oauth_settings(pg_host=""))

    with pytest.raises(RuntimeError, match="PGHOST"):
        with database.get_connection():
            pass


def test_get_connection_rejects_insecure_sslmode(monkeypatch):
    monkeypatch.setattr(
        database,
        "get_settings",
        lambda: oauth_settings(pg_sslmode="disable"),
    )

    with pytest.raises(RuntimeError, match="requires PGSSLMODE"):
        with database.get_connection():
            pass


def test_service_applies_limit_per_location_and_persists_once(
    monkeypatch, document_factory
):
    chicago = [document_factory(f"forecast:chi-{index}") for index in range(3)]
    austin = [
        document_factory(f"forecast:aus-{index}", location="Austin, TX")
        for index in range(3)
    ]
    client = FakeWeatherClient({"Chicago, IL": chicago, "Austin, TX": austin})
    events = []

    monkeypatch.setattr(database, "ensure_weather_table", lambda: events.append("ensure"))

    def fake_upsert(documents):
        events.append([document.id for document in documents])
        return len(documents)

    monkeypatch.setattr(database, "upsert_weather_documents", fake_upsert)

    synced = weather_service.sync_weather(
        ["Chicago, IL", "Austin, TX"], 2, client
    )

    assert synced == 4
    assert client.calls == ["Chicago, IL", "Austin, TX"]
    assert events == [
        "ensure",
        ["forecast:chi-0", "forecast:chi-1", "forecast:aus-0", "forecast:aus-1"],
    ]


def test_service_does_not_write_after_fetch_failure(monkeypatch, document_factory):
    error = RuntimeError("upstream failed")
    client = FakeWeatherClient(
        {"Chicago, IL": [document_factory()]},
        failure=("Austin, TX", error),
    )
    monkeypatch.setattr(
        database,
        "ensure_weather_table",
        lambda: pytest.fail("DDL must not run after a fetch failure"),
    )
    monkeypatch.setattr(
        database,
        "upsert_weather_documents",
        lambda documents: pytest.fail("upsert must not run after a fetch failure"),
    )

    with pytest.raises(RuntimeError, match="upstream failed"):
        weather_service.sync_weather(["Chicago, IL", "Austin, TX"], 50, client)


def test_state_service_persists_one_complete_batch(monkeypatch, document_factory):
    from app.models import StateWeatherBatch

    documents = [
        document_factory("zone_forecast:one", source_type="zone_forecast"),
        document_factory("urn:nws:alert:one", source_type="alert"),
    ]
    batch = StateWeatherBatch(
        state="IL",
        documents=documents,
        locations=["Northern Cook, IL"],
        zones_processed=1,
        stations_processed=0,
    )
    client = FakeWeatherClient({"IL": batch})
    events = []
    monkeypatch.setattr(database, "ensure_weather_table", lambda: events.append("ensure"))
    monkeypatch.setattr(
        database,
        "upsert_weather_documents",
        lambda items: events.append([item.id for item in items]) or len(items),
    )

    synced, result = weather_service.sync_state_weather(
        "IL", ["zone_forecast", "alert"], 25, client
    )

    assert synced == 2
    assert result is batch
    assert client.calls == [("IL", ["zone_forecast", "alert"], 25)]
    assert events == ["ensure", ["zone_forecast:one", "urn:nws:alert:one"]]


def test_database_ddl_contains_exact_raw_weather_columns(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(
        database, "get_connection", connection_context(connection)
    )

    database.ensure_weather_table()

    sql = connection.fake_cursor.executions[0][0]
    assert "CREATE TABLE IF NOT EXISTS weather_documents" in sql
    assert "id TEXT PRIMARY KEY" in sql
    assert "location TEXT NOT NULL" in sql
    assert "latitude DOUBLE PRECISION" in sql
    assert "longitude DOUBLE PRECISION" in sql
    assert "source_type TEXT NOT NULL" in sql
    assert "headline TEXT" in sql
    assert "narrative_text TEXT NOT NULL" in sql
    assert "issued_at TIMESTAMPTZ" in sql
    assert "effective_at TIMESTAMPTZ" in sql
    assert "payload JSONB NOT NULL" in sql
    assert "synced_at TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    assert connection.commits == 1


def test_database_upsert_updates_every_field_and_commits_once(
    monkeypatch, document_factory
):
    connection = FakeConnection()
    monkeypatch.setattr(
        database, "get_connection", connection_context(connection)
    )
    documents = [
        document_factory("forecast:one"),
        document_factory("forecast:two"),
        document_factory("forecast:one", location="Austin, TX"),
    ]
    calls = []

    def fake_execute_values(cursor, sql, values, *, template, page_size):
        calls.append(
            {
                "cursor": cursor,
                "sql": sql,
                "values": values,
                "template": template,
                "page_size": page_size,
            }
        )

    monkeypatch.setattr(database, "execute_values", fake_execute_values)

    count = database.upsert_weather_documents(documents)

    assert count == 2
    assert connection.commits == 1
    assert len(calls) == 1
    sql = calls[0]["sql"]
    assert "ON CONFLICT (id) DO UPDATE" in sql
    for column in (
        "location",
        "latitude",
        "longitude",
        "source_type",
        "headline",
        "narrative_text",
        "issued_at",
        "effective_at",
        "payload",
        "synced_at",
    ):
        assert column in sql
    assert calls[0]["page_size"] == 500
    assert calls[0]["template"].endswith("%s, now())")
    assert [values[0] for values in calls[0]["values"]] == [
        "forecast:one",
        "forecast:two",
    ]
    assert calls[0]["values"][0][1] == "Austin, TX"
    assert all(isinstance(values[-1], Json) for values in calls[0]["values"])


def test_weather_summary_queries_counts_without_loading_documents(monkeypatch):
    class SummaryCursor(FakeCursor):
        def __init__(self):
            super().__init__()
            self.result_sets = [
                [
                    {
                        "total_documents": 12,
                        "total_embeddings": 18,
                        "total_locations": 3,
                        "source_type_count": 2,
                        "last_synced_at": "2026-08-08T10:00:00+00:00",
                    }
                ],
                [
                    {"source_type": "alert", "document_count": 2},
                    {"source_type": "forecast", "document_count": 10},
                ],
            ]

        def fetchall(self):
            return self.result_sets[len(self.executions) - 1]

    connection = FakeConnection()
    connection.fake_cursor = SummaryCursor()
    monkeypatch.setattr(
        database, "get_connection", connection_context(connection)
    )

    result = database.get_weather_summary()

    aggregate_sql, aggregate_params = connection.fake_cursor.executions[0]
    breakdown_sql, breakdown_params = connection.fake_cursor.executions[1]
    assert result == {
        "total_documents": 12,
        "total_embeddings": 18,
        "total_locations": 3,
        "source_type_count": 2,
        "last_synced_at": "2026-08-08T10:00:00+00:00",
        "source_counts": {"alert": 2, "forecast": 10},
    }
    assert "COUNT(*) AS total_documents" in aggregate_sql
    assert "COUNT(*) FROM weather_embeddings" in aggregate_sql
    assert "COUNT(DISTINCT location) AS total_locations" in aggregate_sql
    assert "MAX(synced_at) AS last_synced_at" in aggregate_sql
    assert "GROUP BY source_type" in breakdown_sql
    assert "narrative_text" not in aggregate_sql + breakdown_sql
    assert aggregate_params is None
    assert breakdown_params is None
