# Cold Chain Control Tower & AI Copilot

An operations copilot that replaces static supply chain BI dashboards for temperature-controlled freight. Dispatchers ask questions in plain language. The copilot answers by combining three sources:

- **Live fleet telemetry** in Aiven PostgreSQL, read through a least-privilege role.
- **Live weather** at each truck's position, from Open-Meteo.
- **The company SOP library** in Pinecone, cited section by section.

A LangGraph agent orchestrates these sources and routes each step between a cheap fast model and a stronger reasoning model. Every step is traced in the UI and written to an immutable audit trail.

> "Audit active temperature excursions" returns the four out-of-band trucks, the live wind at each truck's location, and the required response per the SOP. Each recommendation carries an `[SOP: cold_chain_sop_v4.md §…]` citation. The execution trace shows every SQL query, weather payload, and retrieved policy chunk.

---

## Why it matters

| Dashboard era | Control tower copilot |
|---|---|
| Fixed charts. New questions need an analyst and a sprint. | Ad-hoc questions answered in seconds against live data. |
| Thresholds and runbooks live in a PDF nobody opens mid-incident. | Every recommended action cites the governing SOP section. |
| BI service accounts often see everything, including margins. | The agent's database role can read one margin-free view. The engine enforces this, and a live pen test proves it. |
| No record of who queried what. | Every model call and tool call lands in an append-only audit log. |
| One expensive model for everything. | A hybrid router sends most calls to Flash and escalates to Pro only for SQL repair or multi-source synthesis. The savings are shown live. |

## Architecture

```
 [ Dispatcher / Operator Browser ]
                 │  HTTPS (TLS at the edge) → :8000
                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│  AWS EC2 t3.micro — single Docker container (Uvicorn ASGI, 1 worker)   │
│  ├─ Static mount (GET /*)          → React 19 + Vite + Tailwind SPA    │
│  ├─ Telemetry API (GET /api/v1)    → live fleet KPIs & status ribbon   │
│  └─ Copilot API (POST /api/v1)     → LangGraph turn + execution trace  │
└───────────────────────┬────────────────────────────────────────────────┘
                        │  StateGraph + MemorySaver checkpointer
                        ▼
┌────────────────────────────────────────────────────────────────────────┐
│  Hybrid DeepSeek V4 router (src/cold_chain_copilot/agent/)             │
│  ├── Flash  (deepseek-chat,     $0.15 / 1M in)  tool routing, SQL       │
│  └── Pro    (deepseek-reasoner, $0.66 / 1M in)  SQL self-fix, synthesis │
└───────┬───────────────────────────────┬────────────────────────────────┘
        │ SQL (psycopg v3 pool)         │ semantic search
        ▼                               ▼
┌───────────────────────────────┐ ┌──────────────────────────────────────┐
│ Aiven PostgreSQL              │ │ Pinecone Serverless                  │
│ ├─ fde_views.vw_active_fleet  │ │ └─ fde-sop-index-openai (cosine)     │
│ │  (no internal margins)      │ │    SHA-256 chunk cache               │
│ ├─ usr_fde_ro (least priv.)   │ └──────────────────▲───────────────────┘
│ └─ fde_views.agent_audit_log  │                    │ embeddings
│    (append-only)              │      ┌─────────────┴──────────────────┐
└───────────────────────────────┘      │ OpenAI text-embedding-3-small  │
                                       └────────────────────────────────┘
                 Open-Meteo REST API (live weather, wind & gusts) ◀── weather tool
```

### Agent graph

```
START → agent (Flash) ──tool calls──▶ tools ──SQL failure──▶ sql_fix (Pro) ──repaired SQL──▶ tools
            │                           │                        └─ retries exhausted ─▶ END
            │                           └─ tool budget spent ─▶ synthesize (Pro) ─▶ END
            ├─ answer used ≥2 tools ─▶ synthesize (Pro) ─▶ END
            └─ simple answer ─▶ END
```

- **Flash** handles every routing decision, every SQL draft and single-source answers.
- **Pro** is called only when it pays for itself:
  - **SQL self-correction.** This covers a guard block, an RBAC denial or an execution error, bounded by `SQL_MAX_FIX_ATTEMPTS`. Pro returns corrected SQL, which goes back through the same guard and the same read-only role.
  - **Final synthesis.** This applies when the answer reconciles two or more evidence sources, such as telemetry plus weather plus SOP.
- Pro never calls tools, so the reasoning model stays off the tool-calling path.
- **Prefix caching.** The system prompt (`agent/prompts/system_prompt.md`, about 1,500 tokens) is fully static and always sent first. That lets DeepSeek's automatic prefix cache serve it on repeated calls. Dynamic context (the current time) is appended after it.

## Security model (defense in depth)

1. **SQL guard** (`agent/tools/sql_telemetry.py::guard_sql`). This is a tokenizer, not a single regex. It allows exactly one `SELECT`/`WITH` statement. It blocks the following:
   - DML, DDL, `INTO`, locking clauses and data-modifying CTEs.
   - Dangerous functions: `pg_*`, `dblink`, `query_to_xml`, `set_config` and similar.
   - Dollar quoting and prefixed literals.
   - Every relation except `fde_views.vw_active_fleet` (or a CTE built over it).

   Comments are stripped, and a row cap is always enforced.
2. **Read-only transaction.** `SET TRANSACTION READ ONLY` plus a local `statement_timeout`.
3. **Least-privilege role.** `usr_fde_ro` holds exactly `SELECT` on the view and `INSERT` on the audit log (plus the sequence `USAGE` that the `SERIAL` column requires). It has no access to `public.*`, cannot create objects, and cannot read the audit trail.
4. **Margin-free view.** `vw_active_fleet` never projects `internal_margin_usd` and hides `DELIVERED` rows.
5. **Immutable audit.** Triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on `agent_audit_log` for every role, the owner included.
6. **Live proof.** `POST /api/v1/audit/rbac-test` sends 13 fixed attack probes straight to PostgreSQL as `usr_fde_ro`, deliberately skipping layer 1. It reports pass or fail for each probe. Every probe runs in a transaction that is always rolled back.
7. **Admin surface.** The audit endpoints require `X-Admin-Password`. It is compared in constant time, and a client is locked out after repeated failures.

## Quickstart

Prerequisites:
- `uv` and Node 22.12 or later.
- An Aiven PostgreSQL service.
- API keys for DeepSeek, OpenAI (embeddings) and Pinecone.

```bash
cp .env.example .env               # fill in real values
make sync                          # uv sync --all-extras (Python 3.12, locked)
make frontend-install              # npm ci

make ingest                        # setup-rbac → ingest-db → ingest-sops
make run                           # build SPA, serve API + SPA on http://localhost:8000
```

For frontend development, run `make serve` in one terminal and `make frontend-dev` in another. The Vite dev server on `:5173` proxies `/api` to `:8000`.

### CLI

| Command | What it does |
|---|---|
| `fde-copilot setup-rbac` | Creates or re-keys `usr_fde_ro`, then applies `001_security_setup.sql` in a single transaction. Idempotent. |
| `fde-copilot ingest-db` | Generates `data/raw/fleet_telemetry.csv` if it is missing (25 rows: 25 active, 4 excursions, 2 weather delays). Validates every row and batch-upserts into `public.legacy_fleet_shipments`. Pings are rebased to "now". |
| `fde-copilot ingest-sops` | Chunks `data/policy/*` (`.md`, `.txt`, `.pdf`, `.csv`, `.xlsx`) and embeds with `text-embedding-3-small`. Files whose SHA-256 matches the local manifest `data/policy/.ingest_cache.json` are skipped with no API calls. Changed files embed only new chunks (content-addressed IDs) and delete stale ones. Removed files have their vectors deleted. `--ignore-cache` re-diffs every file against Pinecone. |
| `fde-copilot serve` | Uvicorn with a single worker, JSON logs in production. |

### Environment

| Variable | Purpose |
|---|---|
| `AIVEN_ADMIN_DB_URI` | `avnadmin` URI. Used for migrations, seeding and the admin audit viewer. |
| `AIVEN_AGENT_RO_DB_URI` | URI for the agent role. The user **must** be `usr_fde_ro`, and `setup-rbac` sets its password from here. |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` | DeepSeek's OpenAI-compatible endpoint (`https://api.deepseek.com/v1`). |
| `DEEPSEEK_FLASH_MODEL`, `DEEPSEEK_PRO_MODEL` | Default to `deepseek-chat` and `deepseek-reasoner`. |
| `FLASH_INPUT_PRICE_PER_M`, `PRO_INPUT_PRICE_PER_M` | USD per 1M input tokens (0.15 / 0.66). Output prices are optional. |
| `OPENAI_API_KEY` | Embeddings (`text-embedding-3-small`, 1536 dimensions). |
| `PINECONE_API_KEY`, `PINECONE_INDEX_NAME` | The serverless index (`fde-sop-index-openai`). `ingest-sops` creates it if missing. |
| `ADMIN_AUDIT_PASSWORD` | Gates `/api/v1/audit/*`. |
| `SQL_MAX_FIX_ATTEMPTS`, `SQL_ROW_LIMIT`, `SQL_STATEMENT_TIMEOUT_MS`, `AGENT_MAX_TOOL_CALLS` | Agent guardrails. |

`.env.example` documents every variable.

## API

| Method & path | Auth | Description |
|---|---|---|
| `GET /api/v1/health` | none | Liveness plus a database ping. |
| `GET /api/v1/fleet/summary` | none | KPI ribbon counts plus the active rows from `vw_active_fleet`, most urgent first (excursions, weather holds, low battery). See below. |
| `POST /api/v1/chat` | none | `{message, session_id?}` → answer, `trace_steps[]`, models used, token usage, cost. |
| `POST /api/v1/audit/logs` | admin | Paginated, filterable audit trail. |
| `POST /api/v1/audit/rbac-test` | admin | Live RBAC penetration test report. |

The KPI ribbon data covers active fleet, temperature excursions, weather alerts, low battery, open doors and routing cost savings.

Interactive docs are served at `/api/docs`.

## Testing and CI

```bash
make check     # ruff lint + format check, mypy --strict, pytest (offline, no keys needed)
```

- `tests/unit/test_sql_guard.py`: allowed and blocked queries, comment and literal handling, row-cap enforcement, and tool error codes.
- `tests/unit/test_hybrid_router.py`: runs routing decisions and full graph turns on scripted models. It covers:
  - Flash-only answers.
  - Flash → Pro escalation on `SQL_EXECUTION_ERROR` and on guard blocks.
  - Graceful retry exhaustion.
  - Pro synthesis for multi-tool turns.
  - The per-turn tool budget.
  - Session memory.
  - Cost-ledger savings.

`.github/workflows/ci.yml` runs on every push and PR:
- the backend checks through `astral-sh/setup-uv`;
- the strict TypeScript and Vite build;
- a Docker build.

## Deployment (EC2 t3.micro)

The `Dockerfile` has three stages:
1. Node 22 builds the SPA.
2. `astral-sh/uv` runs a locked, non-editable install.
3. A `python:3.12-slim` runtime runs as non-root on `:8000`, with a `HEALTHCHECK`.

`.github/workflows/deploy.yml` runs on manual dispatch or on `v*.*.*` tags, never on branch pushes. It works like this:
1. It builds a `linux/amd64` image and pushes it to GHCR.
2. It SSHes into the instance, swaps the container, and waits for `/api/v1/health` before pruning old images.

The workflow needs these settings:
- Repository secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `GHCR_READ_TOKEN`.
- A `production` environment.
- An env file on the host, by default `/opt/cold-chain-copilot/.env`.

Terminate TLS in front of the container, for example with an AWS ALB or Caddy on the instance. The container itself serves plain HTTP on 8000.

## Project layout

```
src/cold_chain_copilot/
├── cli.py                      Typer CLI
├── core/                       settings (SecretStr), structlog, exceptions
├── db/                         pools, audit writer, RBAC setup, migrations/001_security_setup.sql
├── ingestion/                  telemetry seed loader, SOP indexer
├── agent/                      state, router, graph, prompts/, tools/
└── api/                        app factory, dependencies, schemas, v1/{chat,fleet,audit}
frontend/src/                   App, api/client.ts, components/{TelemetryRibbon,DispatchChat,AuditDashboard}
data/                           raw/fleet_telemetry.csv, policy/cold_chain_sop_v4.md
```

## Operational notes

- **Conversation memory** uses LangGraph's in-memory checkpointer. It is scoped per `session_id`, keeps the last 500 sessions (least recently used are evicted first) and resets on restart. This matches the single-worker t3.micro footprint. For multi-instance deployments, swap in a Postgres checkpointer.
- **The cost-savings tile** reflects the current process lifetime. When output-token prices are not configured, it compares input-token cost only, and the UI says so.
- The "V4 Flash" and "V4 Pro" labels map to the model IDs in `DEEPSEEK_FLASH_MODEL` and `DEEPSEEK_PRO_MODEL`. Both IDs, and the prices, are configuration, not code.

## License

MIT © 2026 Balakrishnan
