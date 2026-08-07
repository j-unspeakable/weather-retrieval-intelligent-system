# Weather Intelligence — Parts 1 and 2

A FastAPI application that retrieves raw forecasts, alerts, and observations
from the National Weather Service, normalizes them, and upserts them into
Databricks Lakebase. A server-rendered Jinja page supports both precise point
locations and state-wide NWS forecast-zone coverage. A separate Databricks
notebook chunks the stored narratives and creates MiniLM vector embeddings.

This repository implements **Part 1: raw weather ingestion** and **Part 2:
the offline vectorization pipeline**. It does not implement vector search,
query embeddings, RAG, Spark, scheduled jobs, or the stock/news functionality
from the Day 2 reference application.

## Application structure

- `app/main.py` creates the FastAPI app, registers routers, and configures
  templates and static files.
- `app/services/weather_client.py` performs synchronous Open-Meteo geocoding
  and weather.gov point, zone, station, forecast, and alert requests.
- `app/services/weather.py` coordinates fetch, limit, and persistence behavior.
- `app/database.py` generates Lakebase OAuth credentials and contains direct
  psycopg2 DDL, upsert, and read logic.
- `app/routers/weather.py` exposes both JSON and HTML workflows.
- `notebooks/ingest_weather_embeddings.ipynb` creates chunk embeddings from
  the existing `weather_documents` rows without changing the FastAPI runtime.

## Configuration

The app uses Lakebase OAuth rather than a stored PostgreSQL password. Every new
psycopg2 connection requests a fresh, one-hour database credential through the
Databricks SDK.

`APP_ENV` makes environment-specific validation and identity selection
explicit:

- `local`: requires Lakebase connection parameters and uses
  `DATABRICKS_CONFIG_PROFILE` for OAuth.
- `test`: does not require Lakebase configuration; tests replace external
  services and database connections.
- `databricks`: requires the injected Lakebase parameters and uses the
  Databricks App service principal automatically.

### Local development

Copy the example environment file:

```bash
cp .env.example .env
```

Get `PGHOST`, `PGUSER`, and `ENDPOINT_NAME` from a dedicated development
branch's Lakebase **Connect** dialog. Use your Databricks email for `PGUSER`,
then authenticate the profile named in `DATABRICKS_CONFIG_PROFILE`:

```bash
databricks auth login --profile weather-dev
```

Your Databricks identity must have a corresponding OAuth Postgres role in the
same workspace as the Lakebase project. The project owner's role is created
automatically; other identities must be added by a database administrator.

OAuth requires `PGSSLMODE=require` (or a stricter verification mode) and a
direct Lakebase endpoint; the built-in PgBouncer endpoint does not support
OAuth.

### Databricks App deployment

Create the Databricks App, then add the Lakebase database as an app resource:

- Resource key: `postgres`
- Permission: **Can connect and create**

The resource binding injects the standard `PG*` connection values.
`app.yaml` sets `APP_ENV=databricks` and resolves `ENDPOINT_NAME` with
`valueFrom: postgres`. Databricks also supplies `WorkspaceClient()` with the
app service principal identity, so no interactive login, profile, stored token,
or database password is required in the deployed app.

The app service principal must have a corresponding Lakebase OAuth Postgres
role and permission to create and use objects in the target schema. The app
resource setup should provide these permissions; confirm them in Lakebase if
deployment authentication or DDL fails.

Set `WEATHER_USER_AGENT` to an identifiable application name with contact
information. No weather or geocoding API key is required.

## Install and run

```bash
uv sync --all-groups
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/> for the frontend; the root redirects to
`/weather`. The deployment command
and non-secret defaults used by Databricks Apps are in `app.yaml`.

## Endpoints

- `GET /healthz` — lightweight health check without a database call.
- `GET /weather` — state/point sync console and recent ingested rows.
- `POST /weather/sync` — point-location JSON ingestion endpoint.
- `POST /weather/sync-state` — one-state JSON ingestion endpoint used by the
  progress UI.
- `POST /weather/sync-form` — form ingestion endpoint used by the frontend.

Example JSON request:

```bash
curl -X POST http://127.0.0.1:8000/weather/sync \
  -H 'Content-Type: application/json' \
  -d '{"locations":["Chicago, IL","30.2672,-97.7431"],"limit":50,"source_types":["forecast","hourly_forecast","observation","alert"]}'
```

Example response:

```json
{
  "synced": 28,
  "locations": ["Chicago, IL", "30.2672,-97.7431"]
}
```

`limit` applies independently after all selected point source documents are
combined in deterministic forecast, hourly, observation, then alert order for
each requested location. Point source types are `forecast`,
`hourly_forecast`, `observation`, and `alert`; omitting `source_types` preserves
the original `forecast` plus `alert` behavior.

Example state request:

```bash
curl -X POST http://127.0.0.1:8000/weather/sync-state \
  -H 'Content-Type: application/json' \
  -d '{"state":"IL","source_types":["zone_forecast","alert","observation"],"station_limit":25}'
```

State sync loads every NWS forecast zone returned for that state. Alerts are
fetched once using the state area code. Observations are optional because each
station requires an additional request; `station_limit` is strictly validated
from 1 through 200 and defaults to 25.

The browser processes selected states or point locations sequentially. Its
progress panel reports the active item, completion/failure status, and document
count, then reloads the page so the recent-document table reflects new rows.

## Source types

- `forecast`: 12-hour periods for a city or coordinate.
- `hourly_forecast`: hourly periods for a city or coordinate.
- `zone_forecast`: NWS text forecast periods for every selected-state zone.
- `alert`: active point or state alerts, retaining the NWS source alert ID.
- `observation`: the latest report from the nearest point station or selected
  state reporting stations.

Zone documents have null coordinates because the table stores scalar points,
while an NWS zone is a polygon. Their raw period remains in `payload`.

## Lakebase schema

The app lazily creates the table before syncing or rendering the weather page:

```sql
CREATE TABLE IF NOT EXISTS weather_documents (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source_type TEXT NOT NULL,
    headline TEXT,
    narrative_text TEXT NOT NULL,
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Rows use `INSERT ... ON CONFLICT (id) DO UPDATE`; every successful update
refreshes `synced_at`.

## Part 2: embedding pipeline

The Databricks notebook at
`notebooks/ingest_weather_embeddings.ipynb` reads `narrative_text` directly
from `weather_documents`. It does not call weather APIs or use Spark.

### Notebook configuration

Run the notebook from this repository in a Databricks Git folder. Its first
cells install only the independent notebook-compute dependencies needed for
the embedding pipeline and for importing `app.database`:

```python
%pip install -q sentence-transformers 'psycopg2-binary>=2.9.9' 'databricks-sdk>=0.118.0' 'pydantic-settings>=2.0.0'
```

`databricks-sdk` is required by `app.database`, and `pydantic-settings` is
required by `app.config`. These notebook-only installs are intentionally not
added to the FastAPI application's dependency lock.

Run the widget-creation cell by itself first. After it completes, enter
`pg_host` and `endpoint_name` in the notebook widget panel, then run the
following validation cell. The notebook also provides widgets for
`pg_database`, `pg_user`, `pg_port`, and `pg_sslmode`; `pg_user` defaults to the
current Databricks user when available. If the widget panel is collapsed, open
or pin it from the notebook's widget-panel controls. Creating or editing
widgets requires edit permission on the notebook.

These values populate the existing `APP_ENV=databricks` and
`PG*`/`ENDPOINT_NAME` environment contract before importing
`app.database.get_connection()`. The notebook does not decode a second secret
or create its own `psycopg2.connect` configuration.

The configured Databricks identity must be allowed to connect to the Lakebase
endpoint and create tables and indexes in the target schema.

### Chunking and embedding behavior

- Model: `sentence-transformers/all-MiniLM-L6-v2`
- Dimension: 384
- Character chunk size: 800
- Character overlap: 100
- Embedding batch size: 32

Embedding IDs are SHA-256 hashes of the model name, document ID, and chunk
index with explicit separators. Vectors are sent as pgvector text and cast
directly with `%s::vector` during a psycopg2 `execute_values` upsert. There is
no intermediate Postgres array or cast-after-insert step.

The notebook verifies that pgvector is enabled and creates:

```sql
CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(384) NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index, model_name)
);

CREATE INDEX IF NOT EXISTS weather_embeddings_embedding_hnsw_idx
ON weather_embeddings
USING hnsw (embedding vector_cosine_ops);
```

### Rerun behavior and known limitation

For the selected model, a source document is considered unembedded when it has
no corresponding row in `weather_embeddings`. A successful first run embeds
all selected documents; a second run selects zero of those documents and makes
no writes.

This deliberately simple Part 2 rule does not detect changes to
`weather_documents.narrative_text` when the document ID remains unchanged. An
already embedded document with edited narrative text will not automatically be
re-embedded. Document hashes or source-version tracking are deferred beyond
Part 2.

## Tests

Tests mock external APIs and Lakebase, so no credentials or network access are
required:

```bash
uv run pytest
```
