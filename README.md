# FlowState

**A browser agent that reverse-engineers web apps into interactive state graphs.**

FlowState explores a live website and builds a visual map of how the product actually behaves: pages, modals, forms, auth walls, paywalls, tabs, dropdowns, and the replayable actions that connect them. It treats a web app as a finite-state machine, not a list of URLs.

Give it one URL. Watch the agent discover product structure in real time. Inspect every state with a screenshot, action path, and detected affordances. Export the graph or a compact context pack for downstream analysis.

---

## Why this exists

Traditional crawlers answer "what pages exist?" FlowState answers "what can a user do, and what UI states does each action reveal?"

A **state** is not just a URL. These are all distinct states:

- `/pricing`
- signup modal open
- checkout step 2
- dashboard empty state
- dropdown expanded
- login wall
- payment wall with only blocked CTAs

An **edge** is a concrete, replayable browser action: click a navbar link, open a tab, expand a menu. Every edge stores enough selector and locator data to reconstruct the path from the root.

This makes FlowState useful for product mapping, QA surface discovery, onboarding flow analysis, and building structured context for downstream agents.

### Cost-efficient by design

Many modern web crawlers lean on vision models or full-page screenshot comparison at every step. That works, but it is expensive: GPU inference, large image payloads, and per-state API calls add up fast on real products with hundreds of UI states.

FlowState takes a different path. The engine drives exploration with deterministic signals first: normalized URLs, DOM skeleton hashes, visible-text simhash, lightweight screenshot dHash, action signatures, and rule-based safety. Screenshots are captured for human inspection and export, but they are not the primary decision layer. The goal is a crawler that stays cheap and affordable at scale while still mapping dynamic UI states that URL-only tools miss.

Optional LLM calls are scoped to the engine as a later enhancement (labeling, tie-breaking, gray-area safety), not the foundation of identity or traversal. See [Engine LLM layer (WIP)](#engine-llm-layer-wip) below.

---

## Highlights

| Capability | What it does |
|------------|--------------|
| **Journey-aware exploration** | Schedules actions across user journeys instead of exhausting one deep branch first |
| **Layered state identity** | Deduplicates by normalized URL, modal flag, DOM skeleton hash, and action signature, with fuzzy fallback for structural noise |
| **Safety-first mapping** | Never submits payments, deletes data, sends messages, or performs irreversible actions. Blocked actions become product boundaries, not failures |
| **Heuristic action ranking** | Prioritizes signup, pricing, onboarding, billing, and settings flows before low-value legal or social links |
| **Sibling collapse** | Groups repeated cards, rows, and list items into one representative edge to keep the graph readable |
| **Dynamic URL families** | Detects repeated entity patterns (posts, profiles, products) and samples conservatively with labeled family nodes |
| **Guest and login journeys** | Maps auth boundaries in guest mode; login mode discovers hidden sign-in entry points and keeps guest vs authenticated states distinct |
| **Live SSE streaming** | Nodes, edges, and blocked actions stream to the UI as the agent works |
| **Auth gate pause/resume** | Pauses at login walls so a human can authenticate or supply credentials, then continues exploration |
| **Context pack export** | Deterministic, LLM-free site brief derived from the graph for engineering, QA, or agent handoff |
| **Hosted worker mode** | Disposable Chromium containers with network egress controls for production deployments |
| **Engine LLM layer (WIP)** | Optional LiteLLM roles inside `engine/` for state labels, action tie-breaks, and safety gray areas. Not required to run; identity and traversal stay heuristic-first |

Identity, classification, ranking, and safety run on deterministic heuristics today. LLMs are intentionally excluded from state deduplication. Model-assisted labeling and ranking live in the engine roadmap (`config/models.yaml`) and are work in progress, not a production dependency.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  React + Vite UI                                                        │
│  Landing · Live graph (React Flow) · Agent view · Node panel · Export   │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ REST + SSE
┌───────────────────────────────▼─────────────────────────────────────────┐
│  FastAPI (api/)                                                         │
│  Run lifecycle · Event pub/sub · Graph export · Auth gate endpoints     │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         │ local dev            │ hosted mode          │
         ▼                      ▼                      │
┌─────────────────┐    ┌─────────────────┐           │
│ Explorer engine │    │ Supervisor +    │           │
│ (in-process)    │    │ crawl-worker    │           │
└────────┬────────┘    └────────┬────────┘           │
         │                      │                      │
         └──────────────────────┼──────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  engine/                                                                │
│  explorer · capture · identity · safety · ranking · classify · families │
│  browser/ (Playwright session, snapshot, actions)                       │
│  llm/ (WIP) state_labeler · action_ranker · safety_judge via LiteLLM   │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
   SQLite / Postgres     Local artifacts          graph.json
   (runs, states,         (screenshots,            + context pack
    edges)                  DOM snapshots)
```

### Exploration loop

1. Navigate to the target URL and stabilize the page.
2. Extract visible, enabled interactive elements (role + name, data attributes, text, href, generated CSS).
3. Rank and collapse candidate actions; filter through the safety policy.
4. Classify the state (page, modal, auth wall, paywall, dead end, risky terminal, ...).
5. Persist the state with screenshot, visible text, forms, CTAs, flags, and replay path.
6. Enqueue safe actions on a journey-aware frontier.
7. For each action: replay the path to the source state, perform the action, observe the result, merge or register a new node, repeat until budgets exhaust.

Budgets (`max_states`, `max_actions`, `max_depth`, `max_wall_seconds`) stop exploration predictably.

### State identity

Two observations are the same state when they match on:

1. Normalized URL (tracking params stripped, volatile IDs templated)
2. Modal-open flag
3. DOM skeleton hash (visible structure only, lists truncated)
4. Action signature (set of positional-stripped interactable selectors)

Fuzzy fallback merges near-identical states when visible-text simhash and screenshot dHash both agree. Timestamp and counter changes never create new states.

### Safety policy

The agent maps products; it does not mutate them. Hard denies cover:

- Payment and purchase flows
- Destructive actions (delete, deactivate, reset)
- Communication (send, invite, share)
- Publishing and legal acceptance
- File uploads and form submissions
- External origins and downloads
- Session termination (logout)

Denied actions are recorded on the state. A state where every candidate action is denied becomes a `risky_terminal` node: the payment wall is mapped, not crossed.

### Engine LLM layer (WIP)

LLM integration is planned as an optional stage inside the engine, not a replacement for the heuristic core. Routing is already defined in `config/models.yaml`; wiring is in progress.

| Role | Purpose | When it runs |
|------|---------|--------------|
| `state_labeler` | Human-readable names and summaries from screenshot + text | After capture, batched per run |
| `action_ranker` | Tie-breaks when heuristic scores are ambiguous | Before enqueue, capped per state |
| `safety_judge` | Classifies actions the rule engine cannot confidently allow or deny | On uncertain candidates only |

Hard safety denies and state identity never defer to a model. LLM calls are budgeted (`max_llm_calls_per_run`) so runs stay predictable in cost. Until this layer ships, the engine runs fully offline with no API keys required.

---

## Tech stack

| Layer | Technologies |
|-------|--------------|
| Browser automation | Playwright (async Chromium) |
| Backend | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy (async) |
| Streaming | Server-Sent Events (`sse-starlette`) |
| Frontend | React 18, TypeScript, Vite, React Flow (`@xyflow/react`) |
| Layout | Dagre |
| Persistence | SQLite (default), Postgres-ready via `FLOWSTATE_DATABASE_URL` |
| Artifacts | Local JSON + PNG screenshots under `data/runs/{run_id}/` |
| Packaging | uv |
| Testing | pytest (134 tests), Vitest (frontend unit tests) |

---

## Quick start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python dependency management
- [Node.js](https://nodejs.org/) 18+ for the UI
- Chromium via Playwright

### Backend

```bash
uv sync
uv run playwright install chromium
```

Optional: copy `.env.example` to `.env` to override the database URL or data directory.

Start the API:

```bash
uv run uvicorn api.main:app --reload --port 8077
```

Health check: `GET http://127.0.0.1:8077/health`

### Frontend

```bash
cd ui
npm install
cp .env.example .env   # optional; defaults to http://localhost:8077
npm run dev
```

Open `http://localhost:5173`, enter a URL, and watch the graph build live.

### CLI (offline use)

Explore a site and export the graph:

```bash
uv run python -m engine explore https://example.com
```

Useful flags:

```bash
uv run python -m engine explore https://example.com \
  --headed \
  --max-states 50 \
  --max-actions 200 \
  --max-depth 4 \
  --out graph.json
```

Capture a single URL as one state:

```bash
uv run python -m engine capture https://example.com
```

Output lands in `data/runs/<run_id>/` (screenshots, optional DOM snapshots), `data/flowstate.db`, and an optional `graph.json` export.

---

## API

All routes are prefixed with `/api`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/runs` | Start an exploration (returns `202` with `run_id`) |
| `GET` | `/runs` | List all runs, newest first |
| `GET` | `/runs/{id}` | Run status and stats |
| `GET` | `/runs/{id}/graph` | Full state graph JSON |
| `GET` | `/runs/{id}/export` | Graph as downloadable JSON attachment |
| `GET` | `/runs/{id}/context` | Deterministic context pack (`?format=markdown` or `json`) |
| `GET` | `/runs/{id}/events` | Server-Sent Events stream of run progress |
| `POST` | `/runs/{id}/auth/resume` | Resume a run paused at an auth gate |
| `POST` | `/runs/{id}/auth/skip` | Skip auth and continue without authenticating |

### Start a run

```bash
curl -s -X POST http://127.0.0.1:8077/api/runs \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","max_depth":2,"max_actions":20}'
```

Response includes `run_id`, `events_url`, and `graph_url`.

### Stream live progress

```bash
curl -N http://127.0.0.1:8077/api/runs/<run_id>/events
```

The stream replays buffered history, so subscribing at any time yields the full event sequence: `run_started`, `state_new`, `edge_created`, `actions_blocked`, `state_deduped`, `auth_gate`, `run_finished`, and more.

Screenshots referenced by graph nodes are served from `/artifacts/runs/<run_id>/screenshots/<hash>.png`.

### Request body (POST /runs)

| Field | Type | Notes |
|-------|------|-------|
| `url` | string | Required. Public `http` or `https` URL |
| `auth_mode` | `"guest"` \| `"login"` | Default `guest` |
| `credentials` | `{ username, password }` | Required for login mode; never persisted or exported |
| `headless` | boolean | Default `true` |
| `max_states` | integer | Exploration budget |
| `max_actions` | integer | Exploration budget |
| `max_depth` | integer | Exploration budget |
| `max_wall_seconds` | integer | Wall-clock budget |
| `save_dom_snapshots` | boolean | UI runs typically set `false` |

---

## Graph output

Each **state node** carries:

- `id`, `url`, `title`, `state_type`, `page_role`
- `screenshot` path, `visible_text` excerpt
- `flags` (modal, auth required, payment required, dead end, risky terminal)
- `path`: ordered replay steps from the root
- `surface_items`: viewport-grounded affordances with exploration status
- `visible_ctas`, form metadata, denied actions

Each **edge** carries:

- `from`, `to`, `action` kind, human-readable `label`
- `selector`, `role`, `href`, `locator` fallbacks
- `collapsed_count` when sibling collapse grouped repeated items

Example state types: `page`, `modal`, `form`, `auth_wall`, `paywall`, `dropdown`, `tab`, `wizard_step`, `dead_end`, `risky_terminal`, `external`.

---

## UI

The React frontend provides:

- **Landing page** with URL input, auth mode, and exploration budgets
- **Live workspace** with run status, export menu, and auth gate banner
- **Graph view** (React Flow): page nodes, interaction subgraphs, dynamic family groups, auto-layout via Dagre
- **Agent view**: live screenshot, current action, filterable event log, exploration counters
- **Node panel**: screenshot, URL, state type, flags, replay path, surface inventory by region, parent/child edges
- **Export**: graph JSON, context pack (Markdown or JSON)

Click a node to inspect it. Counters in the agent view filter the event log by outcome (deduped, denied, replay failed, noop, and more).

---

## Configuration

Run behavior is defined in `config/default_run.yaml` and validated against `engine.schemas.RunConfig`. Unknown keys are rejected.

Notable defaults:

| Setting | Default | Purpose |
|---------|---------|---------|
| `budgets.max_states` | 250 | Stop after this many distinct states |
| `budgets.max_actions` | 1000 | Stop after this many performed actions |
| `budgets.max_depth` | 8 | Maximum navigation depth from root |
| `capture.max_scroll_steps` | 4 | Scroll to discover below-the-fold affordances |
| `exploration.url_family_cap` | 3 | Cap distinct states per dynamic URL family |

LLM role routing (WIP, engine stage) lives in `config/models.yaml` with per-role model strings and a per-run call budget. The rest of the run config stays LLM-free.

Environment variables (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLOWSTATE_DATABASE_URL` | SQLite in `data/` | Async SQLAlchemy connection |
| `FLOWSTATE_DATA_DIR` | `data` | Screenshots and run artifacts |
| `FLOWSTATE_RUN_CONFIG_PATH` | `config/default_run.yaml` | Run configuration file |
| `FLOWSTATE_HOSTED_MODE` | unset | Delegate crawls to the supervisor worker |

---

## Project structure

```
api/                  FastAPI routes, run manager, SSE pub/sub
engine/
  browser/            Playwright session, page stabilization, action discovery
  db/                 SQLAlchemy models and async session factory
  explorer.py         Core exploration loop and frontier scheduling
  identity.py         URL normalization, hashing, dedup match rule
  safety.py           Rule-based action policy
  ranking.py          Heuristic scoring and sibling collapse
  classify.py           State type and flag detection
  families.py           Dynamic URL family inference
  capture.py            Observe page and persist state artifacts
  export.py             Graph JSON and context pack generation
ui/src/
  api/                  HTTP client and SSE event stream
  features/graph/       React Flow graph, node panel, layout
  features/agent-view/  Live screenshot, event log, counters
  features/runs/        Landing page, workspace, run state reducer
supervisor/           Hosted mode: one container per crawl
worker/               Disposable Chromium worker (stdin/stdout protocol)
deploy/               Hosted deployment notes and seccomp profile
config/               Run and model configuration YAML
tests/                Unit and integration tests (offline fixture site)
tests/fixtures/demo_site/   Local HTML fixture (modal, tabs, forms, paywall, families)
```

---

## Testing

```bash
# Backend (134 tests)
uv run pytest

# Frontend
cd ui && npm run test
```

Test coverage highlights:

| Module | What is tested |
|--------|----------------|
| `test_identity.py` | URL normalization, simhash/dHash thresholds, dedup match rule |
| `test_safety.py` | Deny rules for payment, destructive, external, forms, logout |
| `test_ranking.py` | Action scoring and sibling collapse |
| `test_families.py` | Dynamic URL family promotion and sampling |
| `test_capture.py` / `test_explore.py` | Real Chromium against the offline fixture site |
| `test_api.py` | Run lifecycle, graph export, SSE streaming, context pack |
| `test_auth.py` | Auth gate pause, resume, skip, and event ordering |
| `test_network_policy.py` | Public URL validation and DNS egress checks |
| `test_worker_contract.py` | Hosted worker artifact manifest and PNG validation |

The fixture site under `tests/fixtures/demo_site/` includes pages, modals, tabs, dropdowns, a contact form, payment-like checkout, dead ends, profile families, and external/logout traps.

---

## Hosted deployment

For production, the public API does not run Chromium directly. A private supervisor starts one disposable `crawl-worker` container per run, streams events over the same SSE contract, and destroys the container after import.

Security controls include:

- No published ports on worker containers
- Rootless container runtime
- Outbound firewall denying loopback, RFC1918, and cloud metadata addresses
- Seccomp profile aligned with Playwright crawling recommendations
- Credentials never written to disk or stdout

See [`deploy/README.md`](deploy/README.md) for supervisor wiring and environment variables.

---

## Design principles

1. **States are behavioral, not navigational.** Same URL with a modal open is a different state than the base page.
2. **Map boundaries, do not cross them.** Payment walls and auth gates are valuable graph nodes.
3. **Keep the graph legible.** Collapse siblings, cap fanout, drop no-op self-loops, infer family edges.
4. **Replay everything.** Every state stores an action path from the root for reconstruction.
5. **Heuristics first, models second.** Deterministic identity and safety; optional LLMs live in the engine for labeling and tie-breaking only (WIP).
6. **Affordable at scale.** Prefer hashes and DOM structure over vision-model inference for dedup and traversal; reserve model calls for cases heuristics cannot resolve.
7. **Thin boundaries.** Routes delegate to engine services; the frontend never runs exploration logic.

---

## Roadmap

- [ ] **Engine LLM layer (WIP):** LiteLLM wiring in `engine/` for state labeling, action tie-breaking, and safety gray areas (`config/models.yaml`)
- [ ] In-UI path replay (backend replay paths are already stored on every state)
- [ ] Postgres and object storage backends (protocols exist; local disk and SQLite are the v1 defaults)

---



