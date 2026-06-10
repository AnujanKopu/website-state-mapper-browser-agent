# FlowState — Web App State Mapper Agent

FlowState is a browser agent that explores a live web app and reverse-engineers it into an
**interactive state graph**. A "state" is not a URL: modals, form steps, auth walls, paywalls,
expanded dropdowns, and wizard screens are all first-class nodes, and every edge is a concrete,
replayable user action ("Clicked 'Start free trial'").

## Status

Milestone **M0** — runnable foundation. The engine can capture a single URL as a state:
screenshot, DOM snapshot, visible text, interactable elements, identity fingerprint, and
database records.

Upcoming milestones: exploration loop + state dedup (M1), LLM labeling/ranking via LiteLLM (M2),
FastAPI + React Flow UI with live graph (M3), path replay + demo polish (M4).

## Architecture

```
engine/
├── __main__.py        CLI entrypoint (python -m engine)
├── config.py          Env settings + run-config YAML loading
├── schemas.py         Pydantic domain models (RunConfig, Interactable, CapturedState, ...)
├── identity.py        URL normalization + content hashing -> state fingerprints
├── storage.py         StorageBackend protocol + LocalStorage (S3/R2 drop-in later)
├── capture.py         State capture orchestration (reused by the explorer loop in M1)
├── browser/
│   ├── session.py     Playwright lifecycle + page guards (dialogs, downloads)
│   ├── snapshot.py    Page stabilization + observation (text, HTML, screenshot)
│   └── actions.py     Interactive element discovery (single in-page script)
└── db/
    ├── models.py      SQLAlchemy tables: runs, state_nodes, edges
    └── session.py     Async engine/session factory (SQLite default, Postgres-ready)
```

Design notes:

- **Storage-agnostic artifacts.** Screenshots/DOM snapshots are addressed by relative keys
  through a `StorageBackend` protocol; local disk today, S3/R2 later without touching callers.
- **Database-agnostic persistence.** Plain String/JSON columns via async SQLAlchemy; switching
  SQLite -> Postgres is a `FLOWSTATE_DATABASE_URL` change.
- **Heuristics-first identity.** Fingerprints come from normalized URL + visible-text hash
  (DOM skeleton hash, simhash, and screenshot pHash layer in during M1). LLMs are never in the
  identity loop.
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

Capture a single URL:

```bash
uv run python -m engine capture https://example.com
```

Watch the browser while it works:

```bash
uv run python -m engine capture https://example.com --headed
```

Output: a run summary in the terminal, a screenshot + DOM snapshot under
`data/runs/<run_id>/`, and `runs` / `state_nodes` rows in `data/flowstate.db`.

## Tests

```bash
uv run pytest
```

`tests/test_capture.py` drives real Chromium against the offline fixture site in
`tests/fixtures/demo_site/` and asserts on artifacts, visibility filtering, and database state.
