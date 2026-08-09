# Agent Bricks configuration

The Weather Intelligence MCP server is deployed as its own Databricks App and
then attached to an Agent Bricks supervisor agent as a custom MCP tool.

## Deploy the MCP App

1. Deploy the updated Day 2 FastAPI App so `POST /api/weather/search` is
   available.
2. Create a Databricks App named `mcp-weather-intelligence`.
3. Add the Day 2 App to it as an **App** resource with key `weather-app` and
   grant **CAN USE**. This grants the MCP App service principal permission to
   call the Day 2 App.
4. Deploy the contents of `mcp_server/` as the MCP App source directory.
   `app.yaml` maps `WEATHER_API_APP_NAME` from the `weather-app` resource and
   starts Streamable HTTP at `/mcp`.
5. Grant the people who will use the agent **CAN USE** on the MCP App.

The broker resolves the resource with `WorkspaceClient().apps.get(...)` and
uses `WorkspaceClient().config.authenticate()` for each app-to-app request. Do
not configure or store an OAuth access token manually.

## Configure Agent Bricks

1. Create or open the supervisor agent in Agent Bricks.
2. Add `mcp-weather-intelligence` as a custom/external MCP App tool. Its MCP
   endpoint is the deployed App URL followed by `/mcp`.
3. Describe it as providing global live Open-Meteo tools plus semantic search
   over the existing Day 2 Lakebase corpus.
4. Copy `agent/system_prompt.md` into the agent instructions.
5. Test one live-only question, one stored-context query, and one question that
   requires both tools before publishing the agent.

Live tools are global and use Imperial units. Stored search remains limited to
whatever the Day 2 ingestion and embedding pipeline has placed in
`weather_documents` and `weather_embeddings`.

## Demonstration evidence

Capture the visible tool call and final answer for these scenarios as text or
screenshots:

1. Invalid location: `What is the weather in a place that cannot be resolved?`
   Confirm the tool returns a structured `LocationResolutionError` and the
   agent asks for a clearer city/region/country instead of guessing.
2. Combined grounding: `Is flooding a concern in Chicago today? Use live
   weather and our stored weather context.` Confirm the answer contains
   `Live — Open-Meteo` and `Stored context — Day 2 Lakebase` headings.
3. Stored-only retrieval: request three stored flooding results and confirm the
   answer does not claim that empty stored results mean live data is missing.

Save actual Playground output rather than constructing a sample transcript;
the evidence should show the deployed tool behavior.
