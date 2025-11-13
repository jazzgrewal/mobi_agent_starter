# Agent Bricks — Sample Agent & Tool Mapping for Mobi

This document shows a sample agent implementation and step-by-step instructions
for wiring it into Agent Bricks. It assumes you already ran the three notebooks
in this repo and built the Silver tables and SQL tools (`recent_trips_by_station`,
`live_station_status`, `nearby_stations`, `site_search`).

Files added to this repo as examples:

- `src/mobi/sample_agent.py` — a small rule-based ChatAgent that calls Databricks SQL functions and the metadata analyzer.
- `src/mobi/metadata_agent.py` — earlier added module for analyzing tables and applying comments.
- `tools/` — sample tool JSON definitions (examples you can adapt to Agent Bricks).

---

## Goal

Create a chatbot in Agent Bricks that can:

1. Run a table analysis (dry-run) and return suggested column comments.
2. Show the suggestions to a human in the chat.
3. When confirmed, apply selected comments to the Databricks table metadata.

We implement two tool endpoints:

- analyze_table (read-only) — runs `metadata_agent.analyze_and_update_table(..., dry_run=True)`
- apply_table_comments (write) — triggers a Databricks Job that runs the apply flow with `dry_run=False` (service token recommended)

Other helpful tools to expose from Databricks (already available in the repo):

- get_recent_trips(station_id) — SQL function `recent_trips_by_station`
- get_live_status(station_id) — SQL/UDTF `live_station_status`
- find_nearby(lat, lon, radius_km) — SQL UDTF `nearby_stations`
- site_search(query) — SQL function `site_search`

---

## Step-by-step: Create Tools in Agent Bricks

1) Prepare endpoints in Databricks

- ANALYZE (notebook): create a notebook `notebooks/analyze_table.ipynb` which:
  - Accepts parameters: `catalog`, `schema`, `table`, `sample_limit`.
  - Imports `mobi.metadata_agent` from repo `src` path.
  - Calls `analyze_and_update_table(spark, catalog, schema, table, sample_limit, dry_run=True)` and prints/returns JSON of suggestions.

- APPLY (job): create a notebook `notebooks/apply_table_comments.ipynb` which:
  - Accepts parameters: `catalog`, `schema`, `table`, `columns` (CSV), `actor`.
  - Validates inputs and runs `metadata_agent` apply logic for specified columns.
  - Appends audit rows to `vanhack.mobi_data.metadata_comment_audit` (create the table once).

2) Create a Databricks Job for apply (recommended)

- In Databricks Jobs UI, create a job that runs `apply_table_comments.ipynb` and accepts `notebook_params`.
- Assign the job a service principal or token that has ONLY the privileges required to comment table metadata in the target schema.

3) Add the tools in Agent Bricks

- analyze_table tool
  - Type: Notebook / API call
  - Connector: Databricks Notebook or SQL
  - Input: catalog, schema, table
  - Behavior: Execute `analyze_table` notebook (dry-run) and return JSON suggestions

- apply_table_comments tool
  - Type: Job / API call
  - Connector: Databricks Jobs API
  - Input: catalog, schema, table, columns
  - Behavior: Trigger Databricks job that runs the apply notebook. Require explicit human confirmation in the chat before calling.

4) Map other read-only tools

- Map `get_recent_trips`, `get_live_status`, `find_nearby`, `site_search` to lightweight SQL endpoints or Databricks SQL Warehouse endpoints. These can be used directly by agents for answering user queries.

---

## Sample Agent Prompts and Tool Usage

System prompt (Agent Bricks agent):

"You are Mobi Assistant. Use the provided tools to answer user questions and to manage table metadata. For any change to metadata, always show a dry-run and request explicit user confirmation before calling the apply tool."

User flow example:

1. User: "Analyze table vanhack.mobi_data.silver_trips and suggest column comments."
2. Agent: Calls `analyze_table` tool; displays suggestions in chat.
3. User: "Apply comments for departure_time and return_time."
4. Agent: Asks for confirmation. User replies "Yes".
5. Agent: Calls `apply_table_comments` job with columns param `departure_time,return_time` and returns the result of the job (success/failure per column).

---

## Tool JSON examples (adapt for Agent Bricks)

See `tools/analyze_table_tool.json` and `tools/apply_comments_tool.json` for example payloads you can adapt when creating tools in Agent Bricks.

---

## Security and Auditing

- Use a service token for apply jobs with narrow privileges.
- Store audit results in `vanhack.mobi_data.metadata_comment_audit` with who/when/old/new comment and SQL executed.
- Keep apply tool invisible to non-admin agents/users or require multi-party approval.

---

If you'd like, I can also:

- Create the sample notebooks (`analyze_table.ipynb`, `apply_table_comments.ipynb`) in this repo with parameter handling and example cells.  
- Produce a ready-to-import Agent Bricks tool JSON (complete with API endpoint and sample authentication placeholders).  
- Add automated audit writes into `src/mobi/metadata_agent.py` so the apply path logs changes automatically.

Tell me which of those you'd like me to add next and I'll implement it.
