# Weather retrieval pipeline

## Data sources

Weather forecasts, alerts, zones, and observations come from the U.S. National
Weather Service at `api.weather.gov`. It is an authoritative public source,
requires no API key, and exposes both forecast narratives and their raw source
objects. City/state inputs are converted to coordinates with Open-Meteo's U.S.
geocoding API; coordinate inputs go directly to the NWS.

## Storage and embeddings

`weather_documents` stores one normalized source record per row: a stable text
ID, requested location, resolved coordinates, source type, headline, narrative,
source timestamps, raw JSON payload, and sync timestamp. Writes use
`INSERT ... ON CONFLICT (id) DO UPDATE`, so ingestion can be rerun safely.

`weather_embeddings` stores narrative chunks linked to the source document by
`document_id`. Chunking uses an 800-character window with 100 characters of
overlap. Embeddings use
`sentence-transformers/all-MiniLM-L6-v2` (`vector(384)`) and are indexed with an
HNSW cosine index. Search ranks embedding chunks with pgvector and joins back to
`weather_documents` for the original metadata and narrative.

## Run sync → embed → search

1. Copy `.env.example` to `.env`, configure the Lakebase OAuth settings, and
   optionally set `LLM_API_KEY` for OpenRouter summaries.
2. Install and start the application:

   ```bash
   uv sync --all-groups
   uv run fastapi dev
   ```

3. Open `/weather` and sync states, cities, or coordinates. The JSON alternative
   is `POST /weather/sync` (or `POST /weather/sync-state`).
4. The Databricks Job containing
   `notebooks/ingest_weather_embeddings.ipynb` runs once daily and embeds
   documents that do not yet have rows for the configured model. Trigger the
   job manually when newly synced documents must become searchable immediately.
5. Return to `/weather` and use Semantic Weather Search, or call:

   ```text
   GET /weather/search?query=severe+thunderstorms&top_k=5
   ```

## Known limitations

- NWS coverage is U.S.-focused, and ingestion depends on public API availability
  and rate limits.
- The daily embedding schedule means newly ingested documents can take up to
  roughly 24 hours to appear in semantic search unless the job is triggered
  manually.
- Existing embeddings are not refreshed when `narrative_text` changes without a
  document ID change; content hashes would improve this.
- The first semantic search can be slow while MiniLM is downloaded and loaded.
- Natural-language summaries depend on OpenRouter; the free router has variable
  model choice, latency, and availability. A production version should pin a
  model and add stronger monitoring and retry controls.
