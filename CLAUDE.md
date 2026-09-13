# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A multi-tenant, multi-region lead generation and outreach agent built on LangGraph
(`src/`), plus a small FastAPI settings service (`backend/`) and a React/Vite dashboard
called **LeadFlow** (`dashboard/`). All three are one repository and one deployment per
business.

Read `README.md` (the agent), `backend/README.md` (the settings service and secrets) and
`dashboard/README.md` (the product rules) before making non-trivial changes — they carry
the reasoning behind most of what looks arbitrary here.

## Commands

Python runs from the **project root**, always. `backend/main.py` and the nodes do
`from src...`, so `python -m backend.main` from `backend/` fails with
`No module named 'backend'`.

```bash
# environment (Python 3.12 recommended; 3.14 risks source builds for lxml/playwright)
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt -r backend/requirements.txt
playwright install chromium        # only needed for live enrichment, not dry runs

# tests (pytest.ini sets testpaths=tests and -q; conftest.py puts the root on sys.path)
pytest
pytest tests/test_suppression_gate.py -v
pytest tests/test_gemini_model.py::test_the_default_generation_model_is_the_supported_one

# a full graph run against fixtures -- the intended way to check any agent change
python -m src.cli.run_batch --tenant example_tenant --dry-run
python -m src.cli.run_batch --tenant example_tenant --graph      # Mermaid, then exit
#   other flags: --limit N  --niche <id>  --region <code>  --no-resume

# the human-in-the-loop CLIs
python -m src.cli.approve --tenant example_tenant
python -m src.cli.linkedin_queue --tenant example_tenant list

# the settings service (BACKEND_PORT -> PORT -> 8000; BACKEND_HOST defaults to loopback)
python -m backend.main

# the dashboard
cd dashboard && npm install
npm run dev          # 5173, proxies /api to the backend port
npm run build        # tsc -b && vite build -- must be zero errors, zero warnings
npm run typecheck
```

There is no frontend test runner and no linter configured. The dashboard's guarantees
(no banned vocabulary in the DOM, no horizontal overflow, no Send button on LinkedIn)
were verified with ad-hoc Playwright passes; re-verify the same way when touching UI copy.

## Architecture

### The graph is per-lead, the batch is not

`src/graph.py` compiles one `StateGraph(LeadState)` that runs **per lead** on its own
checkpointed thread (`.data/checkpoints/{tenant_id}.sqlite`). Anything inherently
batch-level -- discovery fan-out, the Hunter top-N decision -- happens in
`src/cli/run_batch.py` *before* threads open, because a per-lead node cannot fan one
state into many. `run_batch(tenant_id, *, dry_run, limit, resume, niche, region)` is the
single entry point; `backend/runs.py` calls the same function on a thread.

Nodes are `src/nodes/n*.py`, N0 through N9. **N9 is the single exit** -- every terminal
path routes through the CRM node before `END`, so no lead leaves without a row saying
what happened to it.

### Two vocabularies for the same node, and they differ

`graph.py` names its nodes for edges (`discovery`, `qualification`); each node function
declares its own name to the `@node("n1_discovery")` decorator, and *that* is what
reaches the progress sink and `wrapper.__node_name__`. Keying anything on the edge names
produces a screen where every stage reports "skipped" while the run works perfectly.
`backend/runs.py::STAGE_FOR_NODE` maps the decorator names to dashboard stages and
`check_stage_coverage()` fails a test if a node ever reports into nothing.

### `@node` is the one cross-cutting seam

`src/reliability.py` gives every node retry-once semantics, the Section 8 logging
contract, and a catch-all that flags the lead `needs_manual_review` instead of crashing
the batch. Its exception handling is load-bearing:

- `SkipLead` -> applies the carried updates (ordinary control flow)
- `GraphBubbleUp` -> **re-raised**; this is LangGraph's own control flow and `interrupt()`
  travels through it. Swallowing it turns every approval pause into a failure.
- `ConfigError` -> **re-raised**; N0 must halt the batch rather than run against defaults.

It is also where `src/progress.py` is called from, which is why adding progress reporting
needed no change to 13 node signatures. `progress` has a no-op default sink, one sink at
a time; installing one must never become a precondition for running a batch.

### Credentials resolve through the store first

`src/settings.py` **redefines `env()` at module level** (`# type: ignore[no-redef]`)
after the primitive readers. For any variable the provider registry knows about, the
workspace's encrypted store is consulted first and the process environment second -- so a
key entered on the Connections screen takes effect with no file editing, and a headless
install that never opens the dashboard is unaffected. The variable list comes from
`src/providers/registry.py::ENV_VAR_SOURCES` (derived from each `ProviderSpec.env_vars`),
never a hand-kept copy, because a copy drifts in the worst direction: a key that stores,
shows as connected, and is never read.

`src/settings.py` also owns every per-tenant path (`ROOT` is file-relative, so cwd governs
import resolution only, not data paths) and `_assert_safe_tenant_id`.

### `src/providers/registry.py` is the only list of vendors

`backend/providers.py` imports from it and attaches a credential check; the dashboard
renders whatever it declares. **A vendor name hardcoded in a router or a React component
is a bug.** Adding a provider is one `PROVIDERS` entry plus one check function.

### The backend edits the config file, it does not shadow it

`backend/workspace.py` is an editor for `config/tenants/<id>.yaml` -- ruamel round-trip so
the comments survive, validated with the agent's own `_validate` before the write lands,
atomic. There is no second copy of the user's configuration. Identity fields carry
recognisable placeholders (`[your name - set in Settings]`, `*.invalid`) rather than
blanks, because the validator requires them; `GET /api/profile` reports placeholders as
empty strings so a fresh install shows a genuinely blank profile.

### The frontend's service seam

`dashboard/src/services/index.ts` is the single wiring decision. `localApi.ts` holds lead
data in the browser for the session and **starts empty**; `liveApi.ts` spreads it and
overrides the calls the service really answers (parse, start, poll a run, read leads),
feeding real leads back in via `__ingest`, which merges by `lead_id` and never overwrites
-- local approvals would otherwise be undone. `workspaceApi.ts` is real, always, with no
mock: a key that appears to save but has not is worse than an error.

`http.ts` holds the only base URL: `import.meta.env.VITE_API_BASE_URL || "/api"` --
same-origin by default, so dev (Vite proxy), production (reverse proxy) and Vercel
(`dashboard/vercel.json` rewrite) all use the identical path and there is no CORS case.
Note `toQuery` drops values equal to `"all"`; that sentinel has caused a real bug.

`src/lib/statusLabels.ts` is the **only** place a backend value becomes English. Values
stay raw in the data model; they become prose at render time and nowhere else.

## Rules this codebase enforces, not preferences

- **LinkedIn is never automated.** N6b queues a message for a human to send from their own
  account. No Send button on `/linkedin`, ever, and no wording implying the product
  controls the account. Tests assert N6b and its CLI import no browser driver.
- **A saved API key is never returned by any endpoint, in any form.** `GET /api/integrations`
  returns a state and `last4` (from the *end* of the value). `secrets_store.get()` has
  exactly one intended caller: `src/settings.py`. No route calls it.
- **Nothing is stored without a passing live test.** The disabled Save button is a
  courtesy; `POST /api/integrations/{id}` re-runs the check server-side and refuses on
  failure. That is the gate.
- **`SECRETS_MASTER_KEY` (Fernet) encrypts every saved credential.** Losing it makes them
  permanently undecryptable. Never overwrite or print real values in `.env`.
- **No internal vocabulary in any user-facing string**, including tooltips,
  `aria-label`, `placeholder`, `alt` and console logs: no framework names, "state",
  "checkpoint", node ids, "dry run", "tenant", "niche", "ICP", "fit score".
- **No identity is hardcoded** -- no sample person, no sample leads, no invented metrics.
  Every roll-up is computed from what is actually present; empty states are the truth.
- **Free-tier ceilings are enforced by a durable counter** in `src/counters.py`, never by
  a comment. A metered provider is never in `FALLBACK_ORDER` (a test asserts it).
- **Multi-tenancy is structural.** Every per-tenant resource resolves from `tenant_id`;
  `lead_id` is `sha1(tenant_id + dedupe_key)`; `SuppressionList` has no cross-tenant read
  path at all.
- **`GLOBAL_DRY_RUN=true` is the kill switch** -- N6a/N6b print instead of sending
  regardless of any CLI flag or tenant setting.

## Gotchas that have cost time here

- **No node may take a parameter named `config`.** LangGraph reserves it for
  `RunnableConfig` and injects its own. Tenant config arrives as `tenant_config`.
- **`LeadState` is a `TypedDict`, not Pydantic**, and enums are stored as plain strings
  (`Channel.EMAIL` exists for node code; state holds `"email"`), so checkpoints, Sheets
  rows and fixtures round-trip identically. Validation is bought back explicitly with
  `validate_lead_state()`.
- **`sequence_step` counts *completed* touches and only N8 changes it**, so inside N8 the
  touch being scheduled is `next_step + 1`. Off by one silently skips a cadence step.
- **N4's compliance footer is appended after translation and is never model-translated** --
  N5.5 matches exact phrases from the region profile. `compliance_profiles.yaml` and
  `FOOTER_PHRASES` are a matched pair; a new language needs an entry in both.
- **FastAPI 0.141 defers `include_router` flattening to startup.** Inspecting `app.routes`
  before startup shows `_IncludedRouter` placeholders -- that is not a wiring bug. Check
  `openapi.json` on a running server instead.
- **Git Bash (MSYS) rewrites arguments beginning with `/`**, mangling `curl -w` formats and
  `python -c` strings containing `/api`. Use `MSYS_NO_PATHCONV=1` or a script file.
- **Heredocs collapse `\\` before Python sees it**, turning `\b` into a literal backspace
  byte. Write regex-heavy files with the Write tool and verify with `cat -A`.
- Bare `python` on this machine is a dependency-free 3.14; use `.venv/Scripts/python.exe`.
- Tests must not write into `config/tenants/` -- monkeypatch the path helper to `tmp_path`
  (see `tests/test_run_progress.py::clean_registry`).

## Deployment

Backend on Render, frontend on Vercel.

- **Render** -- Root Directory blank (the repo root), Build
  `pip install -r requirements.txt -r backend/requirements.txt`, Start
  `python -m backend.main`. Set `BACKEND_HOST=0.0.0.0` (the default is loopback and only
  warns), `SECRETS_MASTER_KEY`, and `GEMINI_MODEL=gemini-3.6-flash`.
- **Vercel** -- `dashboard/vercel.json` rewrites `/api/:path*` to the Render URL, with the
  SPA fallback `/:path*` -> `/index.html` **after** it; reversing the order swallows API
  calls.
- **Unresolved:** `config/tenants/<id>/secrets.enc` and `runs.json` sit on Render's
  ephemeral filesystem, and mounting a disk at `config/tenants` would shadow the tracked
  `example_tenant.yaml`. Needs either an env-configurable store path or a startup step
  seeding the YAML onto the disk.
