# Weather Retrieval Intelligent System

A FastAPI application that retrieves raw forecasts, alerts, and observations
from the National Weather Service, normalizes them, and upserts them into
Databricks Lakebase. A server-rendered Jinja page supports both precise point
locations and state-wide NWS forecast-zone coverage. A separate Databricks
notebook chunks the stored narratives and creates MiniLM vector embeddings.
The FastAPI app embeds search queries with the same model, performs pgvector
cosine-similarity retrieval, and calls OpenRouter's chat-completions API for a
short answer grounded in the ranked chunks. A separate Day 3 FastMCP App adds
global live Open-Meteo tools and exposes the existing stored retrieval service
to Agent Bricks without duplicating embedding or database logic.

This repository implements **Part 1: raw weather ingestion**, **Part 2: the
offline vectorization pipeline**, **Part 3: semantic retrieval**, and the Day 3
MCP + Agent Bricks integration. It does not implement conversational memory,
an optional MCP dashboard, Spark, or the stock/news reference functionality.

## Application structure

- `app/main.py` creates the FastAPI app, registers routers, and configures
  templates and static files.
- `app/services/weather_client.py` performs synchronous Open-Meteo geocoding
  and weather.gov point, zone, station, forecast, and alert requests.
- `app/services/weather.py` coordinates fetch, limit, and persistence behavior.
- `app/services/embeddings.py` lazily loads the query embedding model once per
  app worker and validates 384-dimensional query vectors.
- `app/services/llm.py` calls OpenRouter with the ranked chunks and validates
  the returned grounded summary.
- `app/database.py` generates Lakebase OAuth credentials and contains direct
  psycopg2 DDL, upsert, and read logic.
- `app/routers/weather.py` exposes both JSON and HTML workflows.
- `app/routers/retrieval.py` exposes raw stored retrieval for authenticated
  Databricks app-to-app calls without invoking OpenRouter.
- `notebooks/ingest_weather_embeddings.ipynb` creates chunk embeddings from
  the existing `weather_documents` rows using a notebook-local Lakebase OAuth
  connection without changing the FastAPI runtime.
- `mcp_server/` is an independently deployable FastMCP Databricks App with
  global Open-Meteo tools and a delegated Day 2 corpus-search tool.
- `agent/` contains the Agent Bricks system prompt and configuration steps.

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

`WEATHER_STATE_SYNC_WORKERS` controls the bounded concurrency used for NWS
zone forecasts and station observations. It defaults to 6 and is validated
from 1 through 12. `WEATHER_REQUEST_TIMEOUT` remains the timeout for each
individual upstream request.

Grounded summaries use OpenRouter's OpenAI-compatible API. Create an OpenRouter
API key and configure `LLM_API_KEY`; the default model is `openrouter/free`,
the base URL is `https://openrouter.ai/api/v1`, and the request timeout is 45
seconds. For a Databricks App, add the key as a **Secret** app resource with
resource key `openrouter-api-key`; `app.yaml` maps its decrypted value to
`LLM_API_KEY`. Health checks and ingestion can start without this setting, but
searches with results require it. The LLM remains remote and no generative
model weights are downloaded into the application.

OpenRouter's free router is intended for low-volume development and may choose
different free models as availability changes. Its free-model quota and
latency are provider-controlled and do not carry a production SLA. Pin a
specific model in `LLM_MODEL_NAME` or move to a paid route when predictable
model behavior is required.

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
- `GET /weather` — state/point sync console, Lakebase document/embedding
  summary, and semantic search interface.
- `POST /weather/sync` — point-location JSON ingestion endpoint.
- `POST /weather/sync-state` — one-state JSON ingestion endpoint used by the
  progress UI.
- `POST /weather/sync-form` — form ingestion endpoint used by the frontend.
- `POST /weather/search` — semantic search JSON endpoint.
- `GET /weather/search` — query-parameter variant of semantic search.
- `POST /weather/search-form` — server-rendered semantic search form.
- `POST /api/weather/search` — raw semantic matches for authenticated app
  consumers; it uses the existing request/response schema and returns
  `summary: null`.

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

Large states can contain more than 100 NWS forecast zones. Zone forecasts and
station observations therefore use bounded concurrent GET requests with retry
handling for transient 429/5xx responses. Normalized documents retain
deterministic source ordering and are written to Lakebase with batched
`execute_values` upserts rather than one database round trip per document.
Application logs report state start, zone discovery, every ten completed zone
forecasts, Lakebase upsert start/completion, elapsed time, and upstream status
details when failures occur.

The browser processes selected states or point locations sequentially. Its
progress panel reports the active item, completion/failure status, and document
count, then reloads the page so aggregate document, location, and source-type
statistics reflect the new rows. The interface intentionally avoids loading
every stored narrative into the page.

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

Run the notebook on Databricks compute. It is independent from workspace-file
imports and does not use private notebook context or JVM bridge APIs. Its first
cells install only the independent notebook-compute dependencies needed for
the embedding pipeline and Lakebase OAuth connection:

```python
%pip install -q sentence-transformers 'psycopg2-binary>=2.9.9' 'databricks-sdk>=0.118.0'
```

Notebook compute does not inherit the FastAPI environment, so it installs its
own dependencies. Part 3 also includes `sentence-transformers` in the FastAPI
runtime lock because the web application must embed search queries.

Run the widget-creation cell by itself first. After it completes, enter
`pg_host` and `endpoint_name` in the notebook widget panel, then run the
following validation cell. The notebook also provides widgets for
`pg_database`, `pg_user`, `pg_port`, and `pg_sslmode`; `pg_user` defaults to the
current Databricks user when available. If the widget panel is collapsed, open
or pin it from the notebook's widget-panel controls. Creating or editing
widgets requires edit permission on the notebook.

These values populate the standard `PG*`/`ENDPOINT_NAME` environment contract.
The notebook defines a small local `get_connection()` context manager that
generates a fresh credential with
`WorkspaceClient().postgres.generate_database_credential(...)`, connects with
psycopg2 and `RealDictCursor`, and closes every connection. It stores no
database password or OAuth token.

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

## Part 3: semantic retrieval

Open `/weather` and use **Semantic Weather Search** beneath the ingestion
controls. The form accepts a natural-language query, a result count from 1 to
20, and an optional source filter. It renders an LLM-generated grounded answer
followed by ranked matching chunks with their source metadata, and keeps the
complete narrative in a collapsible section.

The same search is available as JSON:

```bash
curl -X POST http://127.0.0.1:8000/weather/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"risk of severe thunderstorms","top_k":5,"source_type":"alert"}'
```

or with query parameters:

```bash
curl 'http://127.0.0.1:8000/weather/search?query=severe%20thunderstorms&top_k=5&source_type=alert'
```

`query` is trimmed and must not be blank. JSON `top_k` values must be integers;
valid integers are clamped to 1 through 20 and default to 5. `source_type` is
optional and uses the application's shared `WeatherSourceType` definition:
`forecast`, `hourly_forecast`, `zone_forecast`, `alert`, or `observation`.
Both POST and GET return a nullable `summary` alongside `results`. When there
are matches, the summary is generated by OpenRouter from those retrieved
chunks. When retrieval returns no rows, the application skips the external API
call and returns `summary: null` with `results: []`.

Example response shape:

```json
{
  "query": "risk of severe thunderstorms",
  "top_k": 5,
  "source_type": "alert",
  "summary": "Severe thunderstorm hazards are present in the retrieved alerts [1].",
  "results": [
    {
      "document_id": "urn:nws:alert:example",
      "source_type": "alert",
      "location": "Illinois",
      "headline": "Severe Thunderstorm Warning",
      "narrative_text": "Full source narrative...",
      "chunk_text": "Matching embedded passage...",
      "similarity": 0.83
    }
  ]
}
```

Search uses `sentence-transformers/all-MiniLM-L6-v2`, the same 384-dimensional
model used by the Part 2 notebook. The application loads it lazily and caches
one model instance per process. The first search may take longer and requires
access to download the model from Hugging Face unless its files are already
cached. Each additional Uvicorn worker loads a separate model copy.

The FastAPI runtime binds `torch` to PyTorch's CPU-only package index through
`tool.uv.sources`. This avoids downloading CUDA, NVIDIA, and Triton packages
that are unnecessary for the application's CPU-based query embedding.

Cosine similarity is calculated only against
`weather_embeddings.embedding` using pgvector's `<=>` operator. The ranked
embedding rows are joined through `weather_embeddings.document_id =
weather_documents.id` so responses can include the original location, source
type, headline, and narrative without duplicating those values in the
embedding table. An empty or nonmatching embedding table produces an empty
result list.

The Part 2 uniqueness constraint on `(document_id, chunk_index, model_name)`
keeps stored chunks deduplicated. Part 3 is read-only and does not regenerate
or update embeddings. The RAG prompt treats retrieved text as untrusted data,
requires the answer to use only supplied context, requests numbered citations,
and limits output to 300 tokens. Conversational memory and LLM-generated
source data remain out of scope.

## Day 3: MCP and Agent Bricks

`mcp_server/` is a standalone FastMCP application with four Streamable HTTP
tools:

- `get_current_weather(location)` — global live current conditions.
- `get_forecast(location, days=7)` — a global 1–16 day forecast.
- `get_weather_recommendation(location, date)` — deterministic precautions for
  an ISO forecast date, including the evidence and thresholds used.
- `search_weather_documents(query, top_k=5, source_type=None)` — semantic
  retrieval from the existing Day 2 corpus.

The three Open-Meteo tools accept global free-form places or valid `lat,lon`
coordinates. Free-form geocoding selects Open-Meteo's highest-ranked result,
and responses return the resolved name, region, country, coordinates, and
timezone. Coordinate inputs are not reverse-geocoded; their supplied
coordinate label is retained. Live values use Fahrenheit, mph, and inches.
No Open-Meteo API key is required.

Recommendation rules are deterministic and use these exact cutoffs:

- Carry an umbrella when maximum precipitation probability is at least 40% or
  forecast precipitation is at least 0.05 inches.
- Bring a jacket when the temperature low or apparent-temperature low is at
  most 50°F.
- Take heat precautions when the high is at least 90°F or the apparent high is
  at least 95°F.
- Take wind precautions when maximum sustained wind is at least 25 mph or
  maximum gusts are at least 35 mph.
- Take snow/ice precautions for at least 0.01 inches of snow or WMO freezing
  precipitation/snow codes 56, 57, 66, 67, 71, 73, 75, 77, 85, or 86.
- Take thunderstorm precautions for WMO codes 95, 96, or 99.

Every triggered result contains both the measurements and the corresponding
thresholds. These rules are practical guidance rather than official emergency
advice.

Stored search is intentionally different: it can only find documents already
ingested into `weather_documents` and embedded into `weather_embeddings`.
Empty stored results do not imply that live weather is unavailable. The MCP
App delegates retrieval to `POST /api/weather/search`, which calls the existing
`search_weather()` service and returns `WeatherSearchResponse` with
`summary: null`. The MCP App therefore needs neither Lakebase access nor a
local MiniLM/PyTorch installation.

### Run locally

Run Day 2 on port 8000, then run the MCP package on port 8001:

```bash
uv run fastapi dev

cd mcp_server
cp .env.example .env
set -a
source .env
set +a
uv sync --all-groups
uv run weather-mcp
```

The local Streamable HTTP endpoint is `http://127.0.0.1:8001/mcp`.

The MCP App uses the same dependency approach as the Day 2 App:
`mcp_server/pyproject.toml` declares the complete dependency graph and
`mcp_server/uv.lock` pins reproducible versions and hashes. Deploy both files
with the MCP source. No separate `requirements.txt` dependency specification
is used; the existing `uv run --frozen weather-mcp` command consumes the
project and lock directly.

### Deploy to Databricks Apps

1. Deploy the updated Day 2 App so `/api/weather/search` is available.
2. Create a separate App named `mcp-weather-intelligence`.
3. Add the Day 2 App as an App resource with key `weather-app` and grant the
   MCP App service principal **CAN USE**.
4. Deploy the contents of `mcp_server/`. Its `app.yaml` resolves
   `WEATHER_API_APP_NAME` from `weather-app` and starts `weather-mcp`.
5. Grant intended Agent Bricks users **CAN USE** on the MCP App.
6. Add the deployed App as a custom MCP tool in Agent Bricks and apply
   `agent/system_prompt.md`.

For deployed app-to-app retrieval, the broker resolves the Day 2 App using
`WorkspaceClient().apps.get(...)` and obtains request headers through
`WorkspaceClient().config.authenticate()`. It does not manage OAuth tokens.
See `agent/README.md` for the remaining workspace configuration steps.

## Tests

Tests mock external APIs and Lakebase, so no credentials or network access are
required:

```bash
uv run pytest

cd mcp_server
uv run pytest
```
