# FlowState — Web App State Mapper Agent

FlowState is a browser agent that explores a live web app and reverse-engineers it into an
**interactive state graph**. A "state" is not a URL: modals, form steps, auth walls, paywalls,
expanded dropdowns, and wizard screens are all first-class nodes, and every edge is a concrete,
replayable user action ("Clicked 'Start free trial'").

## Status

Milestone **M2** — backend API and live run events. A FastAPI service starts and tracks
explorations, serves the graph JSON, exports results, and streams run progress over
Server-Sent Events (node/edge events appear as the agent works). The engine runs entirely
through the API; the CLI remains for local/offline use.

Built so far: M1 core state-mapping engine (priority best-first explorer with ranking, safety,
dedup, sibling collapse, JSON export).

Upcoming milestones: LLM labeling/ranking via LiteLLM, React Flow UI with the live graph,
path replay + demo polish.

## Architecture

```
api/
├── main.py            FastAPI app factory: lifespan, CORS, static artifacts
├── routes.py          Thin HTTP + SSE routes; delegate to engine services
├── manager.py         Run lifecycle + per-run event pub/sub (replay buffer -> SSE)
└── schemas.py         Typed request/response models

engine/
├── __main__.py        CLI entrypoint (python -m engine capture|explore)
├── config.py          Env settings + run-config YAML loading
├── schemas.py         Pydantic domain models (RunConfig, Interactable, Observation, ...)
├── identity.py        State identity: URL normalization, DOM skeleton hash, text simhash,
│                      screenshot dHash, action signature, and the IdentityIndex match rule
├── safety.py          Rule engine: deny payment/destructive/communication/publish/legal
│                      actions, external origins, downloads, and form submissions
├── ranking.py         Heuristic action scoring (flow keywords, nav placement, novelty)
│                      + sibling collapse (explore 1 of N structurally identical elements)
├── classify.py        State classification: modal / auth wall / paywall / dead end /
│                      risky terminal, from page signals + safety verdicts
├── explorer.py        Frontier loop: budgets, path replay navigation, dedup, edges
├── capture.py         observe_page / persist_state split (dedup before writing artifacts)
├── export.py          Graph -> JSON document (the future API/UI contract)
├── storage.py         StorageBackend protocol + LocalStorage (S3/R2 drop-in later)
├── browser/
│   ├── session.py     Playwright lifecycle + page guards (dialogs, downloads)
│   ├── snapshot.py    Stabilization + snapshot + DOM skeleton + page signals
│   └── actions.py     Interactable discovery + consent-banner dismissal
└── db/
    ├── models.py      SQLAlchemy tables: runs, state_nodes, edges
    └── session.py     Async engine/session factory (SQLite default, Postgres-ready)
```

Design notes:

- **States are not URLs.** Identity = normalized URL + modal flag + DOM skeleton hash +
  action signature, with a fuzzy simhash/dHash fallback for structural noise. Modals, tabs,
  and dropdown-open views become first-class nodes; timestamp/counter changes never do.
  LLMs are never in the identity loop.
- **Safety as a feature.** Denied actions (pay, delete, send, publish, accept, upload,
  logout, external) are recorded on the state as `denied_actions`; a state with only risky
  actions becomes a `risky_terminal` node -- the agent maps the payment wall, never crosses it.
- **Graph stays clean.** Sibling collapse groups repeated cards/rows into one representative
  edge (`collapsed_count`), top-K ranked actions per state cap fanout, no-op clicks are
  dropped instead of becoming self-loops.
- **Storage-agnostic artifacts.** Screenshots/DOM snapshots are addressed by relative keys
  through a `StorageBackend` protocol; local disk today, S3/R2 later without touching callers.
- **Database-agnostic persistence.** Plain String/JSON columns via async SQLAlchemy; switching
  SQLite -> Postgres is a `FLOWSTATE_DATABASE_URL` change.
- **Config over code.** Run behavior lives in `config/default_run.yaml`; LLM provider routing
  (M2) lives in `config/models.yaml` with per-role LiteLLM model strings.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
```

Optional: copy `.env.example` to `.env` to override the database URL or data directory.

## Usage

Explore a site and build its state graph:

```bash
uv run python -m engine explore https://example.com
```

Useful flags: `--headed` (watch the browser), `--max-states N`, `--max-actions N`,
`--max-depth N`, `--out graph.json`.

Output: live event log in the terminal, per-state screenshots + DOM snapshots under
`data/runs/<run_id>/`, graph rows in `data/flowstate.db`, and a `graph.json` export with
all states (type, flags, replay path, CTAs) and action edges.

Capture a single URL as one state:

```bash
uv run python -m engine capture https://example.com
```

## API

Start the server:

```bash
uv run uvicorn api.main:app --reload --port 8077
```

Endpoints (all under `/api`):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/runs` | Start an exploration; returns `run_id` immediately (202) |
| `GET`  | `/runs/{id}` | Run status + stats (live handle merged with the persisted row) |
| `GET`  | `/runs/{id}/graph` | Full state graph (states + edges) as JSON |
| `GET`  | `/runs/{id}/export` | Same graph as a downloadable JSON attachment |
| `GET`  | `/runs/{id}/events` | Server-Sent Events stream of run progress |

`POST /runs` accepts `{ "url": "...", "headless": bool?, "max_states": N?, "max_actions": N?,
"max_depth": N?, "max_wall_seconds": N? }`; budget fields override `config/default_run.yaml`.
The SSE stream buffers and replays history, so subscribing at any time yields the complete
event sequence (`run_started`, `state_new`, `edge_created`, `actions_blocked`, `state_deduped`,
`run_finished`, ...). Screenshots referenced by graph nodes are served from `/artifacts/...`.

Example (start a run, then watch nodes/edges stream live):

```bash
curl -s -X POST http://127.0.0.1:8077/api/runs \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","max_depth":1,"max_actions":10}'
# -> {"run_id":"<id>", "events_url":"/api/runs/<id>/events", ...}

curl -N http://127.0.0.1:8077/api/runs/<id>/events     # live SSE stream
curl -s http://127.0.0.1:8077/api/runs/<id>            # status + stats
curl -s http://127.0.0.1:8077/api/runs/<id>/graph      # full graph JSON
```

## Tests

```bash
uv run pytest
```

- `tests/test_identity.py` -- dedup match rule, simhash/dHash thresholds, URL normalization
- `tests/test_safety.py` -- deny-rule coverage (payment, destructive, external, forms, ...)
- `tests/test_ranking.py` -- action scoring and sibling collapse
- `tests/test_capture.py` / `tests/test_explore.py` -- real Chromium against the offline
  fixture site in `tests/fixtures/demo_site/` (pages, modal, tabs, dropdown, contact form,
  payment-like checkout terminal, dead end, external/logout traps)
- `tests/test_api.py` -- run lifecycle, graph/export, and SSE streaming via the ASGI app
