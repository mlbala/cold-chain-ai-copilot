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

## The control tower UI

The UI is one React 19 page with three tabs under a live KPI ribbon: active fleet, excursions, weather holds and routing cost savings. It has light, dark and system themes, remembered per browser, and works down to phone width.

| Tab | What it does |
|---|---|
| **Dispatch Console** | Chat with the copilot. Each answer carries SOP citation badges, a model-route summary (Flash → Pro), cost and copy. The expandable **execution trace** timeline shows every SQL query, weather payload and SOP chunk, with per-step latency. Beside the chat, the **Live Fleet** board offers filters, search, a temperature-vs-band gauge, battery meters and *Ask copilot* on any truck. |
| **Risk & Health** | Risk mix and level counts, affected shipments with their risk factors, delayed shipments, and supply-chain impact by destination, origin or cargo. Below that: live **system health** for every dependency, model spend against the daily budget, and the **guardrail inventory** with activation counters. |
| **Security Audit** | Password-gated. Runs the live RBAC penetration test (score ring, results by category) and pages through the immutable audit trail. |

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
│  ├── Flash  (deepseek-flash,    $0.15 / 1M in)  tool routing, SQL       │
│  └── Pro    (deepseek-v4-pro,   $0.66 / 1M in)  SQL self-fix, synthesis │
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
- **Prefix caching.** The system prompt (`agent/prompts/system_prompt.md`, about 1,970 tokens) is fully static and always sent first. That lets DeepSeek's automatic prefix cache serve it on repeated calls. Dynamic context (the current time) is appended after it.

## FinOps: routing and caching math

Two levers set the model bill. **Routing** sends most calls to Flash, at $0.15 per 1M input tokens instead of Pro's $0.66. **Prefix caching** comes from the static ~1,970-token system prompt, which leads every call, so DeepSeek can serve it from cache. The figures below come from this repository's own payloads, measured with `cl100k_base` as an approximation of DeepSeek's tokenizer:

| Component | Tokens |
|---|---|
| Static system prompt (cached prefix) | 1,966 |
| Tool schemas (Flash calls only) | 544 |
| SQL result, 4 excursion rows | 426 |
| SOP top-4 excursion chunks | 1,360 |
| Open-Meteo summary, one truck | 456 |

**Per-call cost** = `(uncached_in × p_in + cached_in × p_cache + out × p_out) / 1,000,000`.

The examples below price input tokens only, which matches the app's default `*_OUTPUT_PRICE_PER_M=0`. They also assume a cache-hit price of **10 % of the input price**. Put your contract's real price in `FLASH_CACHED_INPUT_PRICE_PER_M` and `PRO_CACHED_INPUT_PRICE_PER_M`. When these are unset, the app bills cache hits at the full input price.

| Turn type | Calls (Flash / Pro) | Input tokens (Flash / Pro) | All-Pro | Hybrid | Hybrid + cache |
|---|---|---|---|---|---|
| Simple lookup (Flash only) | 2 / 0 | 5,430 / 0 | $0.00358 | $0.00081 (−77 %) | $0.00028 (−92 %) |
| Multi-source audit: SQL + SOP + 4× weather, then Pro synthesis | 3 / 1 | 13,486 / 6,196 | $0.01299 | $0.00611 (−53 %) | $0.00415 (−68 %) |
| SQL repair: Flash, then Pro fix, then Flash | 2 / 1 | 5,746 / 2,216 | $0.00525 | $0.00232 (−56 %) | $0.00063 (−88 %) |

**Per 1,000 turns**, with a mix of 60 % simple, 30 % multi-source and 10 % repair:
- All-Pro costs **$6.57**.
- Hybrid routing costs **$2.55**, a 61 % saving.
- Hybrid plus prefix caching costs **$1.48**, a 78 % saving.

That mix makes 2,300 of 2,700 calls Flash, which is the **85 % Flash share**. The system prompt alone is about 53 % of all input tokens, which is why keeping it byte-identical and first matters.

The "+ cache" column assumes a **warm cache**. DeepSeek caches a prefix only after it has seen it, so the first calls after a deploy, a prompt change or a cache expiry pay full price. Measured on the live stack, the multi-source audit ("Audit active temperature excursions": 1 SQL query, 3 SOP searches, 4 weather checks, Pro synthesis) used 26.5 K input tokens. 39 % were cache hits, and it cost $0.0087 against $0.0175 all-Pro (−50 %). That is more tokens than the estimate above, because the model ran extra SOP searches and wrote a draft for Pro. Under steady traffic the rate rises toward the system prompt's share, and higher still because each Flash call reuses the previous call's prompt as a prefix.

You can check this live:
- The trace drawer shows prompt, cached and completion tokens plus cost for every step.
- The Cost Savings tile shows routing savings against an all-Pro baseline. Both sides are priced with the same cache rules, so the tile isolates the routing saving.
- The footnote under the tile shows the prompt-cache hit rate. It also shows the cache saving when a cache price is configured.

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
7. **Admin surface.** The audit endpoints require `X-Admin-Password`. It is compared in constant time, and a client is locked out after 5 failures in 5 minutes.
8. **API guardrails on `/chat`.** These run before any model call, cheapest first:
   - a per-client rate limit (`CHAT_RATE_LIMIT_PER_MINUTE`, HTTP 429);
   - a daily model-spend cap (`LLM_DAILY_BUDGET_USD`, HTTP 429 until 00:00 UTC);
   - a concurrency cap that protects the 1 GB host (`CHAT_MAX_CONCURRENT`, HTTP 503).

   `X-Forwarded-For` is honoured only from `FORWARDED_ALLOW_IPS`, so clients cannot spoof their address to dodge limits.
9. **Agent guardrails.** A per-turn tool budget, a cap on SQL repair attempts, a graph step limit, a session-memory cap, and the SOP citation contract in the static system prompt.

The **Risk & Health** tab lists all 18 controls with their live settings and activation counters, served by `GET /api/v1/system/status`.

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
| `AIVEN_HOST`, `AIVEN_PORT`, `AIVEN_DB_NAME`, `AIVEN_SSLMODE` | Aiven connection details, copied from the service Overview page. `AIVEN_SSLMODE` defaults to `require`. |
| `AIVEN_ADMIN_USER`, `AIVEN_ADMIN_PASSWORD` | `avnadmin` credentials. Used only for migrations, seeding and the admin audit viewer. |
| `AIVEN_AGENT_RO_USER`, `AIVEN_AGENT_RO_PASSWORD` | The agent role. The user **must** be `usr_fde_ro`. Choose the password (12+ characters), and `setup-rbac` creates the role with it. |
| `AIVEN_ADMIN_DB_URI`, `AIVEN_AGENT_RO_DB_URI` | Optional alternative to the separate fields: full service URIs. Used only when `AIVEN_HOST` is unset. |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` | DeepSeek's OpenAI-compatible endpoint (`https://api.deepseek.com/v1`). |
| `DEEPSEEK_FLASH_MODEL`, `DEEPSEEK_PRO_MODEL` | Default to `deepseek-flash` and `deepseek-v4-pro`, the IDs DeepSeek's `/models` lists. System health flags any configured ID the API doesn't list. |
| `DEEPSEEK_PRO_THINKING` | `disabled` (default), `enabled` or `provider_default`. With thinking on, V4 Pro spent about 160 s and about 7K hidden reasoning tokens per synthesis in testing; with it off, about 15 s for an equivalent cited answer. |
| `FLASH_INPUT_PRICE_PER_M`, `PRO_INPUT_PRICE_PER_M` | USD per 1M input tokens (0.15 / 0.66). Output prices are optional. |
| `FLASH_CACHED_INPUT_PRICE_PER_M`, `PRO_CACHED_INPUT_PRICE_PER_M` | Optional cache-hit prices. When unset, cache hits bill at the full input price. |
| `OPENAI_API_KEY` | Embeddings (`text-embedding-3-small`, 1536 dimensions). |
| `PINECONE_API_KEY`, `PINECONE_INDEX_NAME` | The serverless index (`fde-sop-index-openai`). `ingest-sops` creates it if missing. |
| `ADMIN_AUDIT_PASSWORD` | Gates `/api/v1/audit/*`. |
| `SQL_MAX_FIX_ATTEMPTS`, `SQL_ROW_LIMIT`, `SQL_STATEMENT_TIMEOUT_MS`, `AGENT_MAX_TOOL_CALLS` | Agent guardrails. |
| `CHAT_RATE_LIMIT_PER_MINUTE`, `CHAT_MAX_CONCURRENT`, `LLM_DAILY_BUDGET_USD` | API guardrails on `/chat`: 12 per minute, 4 concurrent, $5 per day. A budget of `0` disables the cap. |
| `FORWARDED_ALLOW_IPS`, `HEALTH_PROBE_TTL_SECONDS` | Trusted proxy addresses (default `127.0.0.1`) and the health-probe cache (default 60 s). |
| `TELEMETRY_SIMULATOR`, `TELEMETRY_SIMULATOR_INTERVAL_SECONDS` | Demo heartbeat that keeps seed pings current (default off, 60 s). It only updates `last_updated_utc`. Keep it off wherever real telemetry feeds the table. |

`.env.example` documents every variable.

## API

| Method & path | Auth | Description |
|---|---|---|
| `GET /api/v1/health` | none | Liveness plus a database ping. |
| `GET /api/v1/fleet/summary` | none | KPI ribbon counts plus the active rows from `vw_active_fleet`, most urgent first (excursions, weather holds, low battery). See below. |
| `POST /api/v1/chat` | none | `{message, session_id?}` → answer, `trace_steps[]`, models used, token usage, cost. |
| `POST /api/v1/audit/logs` | admin | Paginated, filterable audit trail. |
| `POST /api/v1/audit/rbac-test` | admin | Live RBAC penetration test report. |
| `GET /api/v1/fleet/risk` | none | Deterministic risk triage (score 0–100, level, factors) for every active shipment, plus supply-chain impact grouped by destination hub, origin hub and cargo class. No model call. |
| `GET /api/v1/system/status` | none | Component health and the guardrail inventory with activation counters (details below). |

Component health in `/system/status` covers both database roles, DeepSeek, OpenAI, Pinecone, Open-Meteo, the audit writer and session memory.
- External services are probed with free, read-only calls (model listings, index stats, one forecast point).
- Results are cached for `HEALTH_PROBE_TTL_SECONDS`.
- No secrets or connection strings are returned.

The KPI ribbon data covers active fleet, temperature excursions, weather alerts, low battery, open doors and routing cost savings.

### Risk scoring

Each active shipment gets a 0–100 score from SOP v4 thresholds:

| Factor | Points |
|---|---|
| Temperature excursion | 35, plus 8 per °C outside the band (capped at +25), plus 15 for pharma |
| Warm-side excursion on mRNA vaccines or blood plasma | Critical floor of 70 |
| Weather hold | 20 |
| Battery below 25 % / below 15 % | 15 / 30 |
| Door open outside loading | 20 |
| No ping for 20+ minutes, or 45+ for pharma | 10 / 15 |

Levels:
- **Critical**: 70 or more.
- **High**: 40 or more.
- **Elevated**: 15 or more.
- **Normal**: below 15.

The score is a fast triage signal. The copilot remains the authority for the cited response.

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

Before deploying, test the image locally with the same flags EC2 uses: `make docker-build && make docker-run`, then open http://localhost:8000. The container always binds `0.0.0.0:8000` regardless of `HOST`/`PORT` in the env file. Measured locally, a live copilot turn peaked at about 130 MiB of the 700 MiB cap.

`.github/workflows/deploy.yml` runs on manual dispatch or on `v*.*.*` tags, never on branch pushes. It works like this:
1. It builds a `linux/amd64` image and pushes it to GHCR.
2. **Swap check.** A t3.micro has only 1 GB of RAM, so the job SSHes in and confirms at least 2 GB of swap.
   - If swap is short, it provisions a persistent 2 GB `/swapfile` (with `vm.swappiness=10`). This needs passwordless sudo and at least 3 GB of free disk.
   - Set the repository variable `EC2_ENSURE_SWAP=false` to only check, failing instead of provisioning.
3. It swaps the container, with a 700 MB memory cap and 1,500 MB including swap, and waits for `/api/v1/health` before pruning old images.

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

- **Telemetry freshness.** The seed fleet has no live feed, so pings age, and after 20 minutes every truck picks up a telemetry-gap risk factor. You have two options:
  - Run `make ingest-db` to refresh the pings once.
  - Set `TELEMETRY_SIMULATOR=true` to keep them current. The API then refreshes active shipments' `last_updated_utc` every interval, staggered over the last few minutes.

  The heartbeat changes nothing else, so the 25 / 4 / 2 scenario and the risk scores stay stable. It writes through the admin pool, because the agent role has no UPDATE privilege by design. Its state appears as **Telemetry feed** under System health.

- **Conversation memory** uses LangGraph's in-memory checkpointer. It is scoped per `session_id`, keeps the last 500 sessions (least recently used are evicted first) and resets on restart. This matches the single-worker t3.micro footprint. For multi-instance deployments, swap in a Postgres checkpointer.
- **The cost-savings tile** reflects the current process lifetime. When output-token prices are not configured, it compares input-token cost only, and the UI says so.
- The "V4 Flash" and "V4 Pro" labels map to the model IDs in `DEEPSEEK_FLASH_MODEL` and `DEEPSEEK_PRO_MODEL`. Both IDs, and the prices, are configuration, not code.

## License

MIT © 2026 Balakrishnan
