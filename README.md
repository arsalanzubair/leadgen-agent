# leadgen-agent

Multi-region, multi-niche, multi-channel lead generation and social outreach
agent, built on LangGraph. Every service in the stack is free or free-tier, and
every free-tier ceiling is enforced by a durable counter in code rather than
trusted to a comment.

**Regions:** US, UK, EU, Canada, Australia, Middle East (UAE/Saudi)
**Niches:** config-driven — local business (dental, salon, gym) and B2B/SaaS
**Channels:** email (automated) and LinkedIn (**semi-automated by design**)

> **LinkedIn is deliberately not automated.** The system discovers, qualifies,
> drafts, compliance-checks and queues LinkedIn messages, then hands them to a
> human to send through LinkedIn's own interface. There is no headless browser
> and no UI automation touching a real account. That is an account-ban-risk
> decision, not an unfinished feature — two tests assert that N6b and its CLI
> never import a browser driver.

**Status:** all ten build steps complete. 510 tests passing on Python 3.12.10.

---

## Contents

1. [How it works](#how-it-works)
2. [Setup](#setup)
3. [Free-tier API keys — what to get, and when](#free-tier-api-keys)
4. [Running a dry-run batch](#running-a-dry-run-batch)
5. [The approval CLI](#the-approval-cli)
6. [The LinkedIn manual-send queue](#the-linkedin-manual-send-queue)
7. [Adding a new tenant](#adding-a-new-tenant)
8. [Going live](#going-live)
9. [Scheduling](#scheduling)
10. [Analytics with Looker Studio](#analytics-with-looker-studio)
11. [Deliverability checks](#deliverability-checks)
12. [Multi-tenancy](#multi-tenancy)
13. [Free-tier limits enforced in code](#free-tier-limits-enforced-in-code)
14. [Testing](#testing)
15. [The dashboard](#the-dashboard)
16. [Design notes](#design-notes)
17. [Troubleshooting](#troubleshooting)

---

## How it works

One LangGraph `StateGraph(LeadState)` runs **per lead**, on its own checkpointed
thread. Batch-level work (discovery fan-out, the Hunter top-N decision) happens
in `cli/run_batch.py` before threads are opened, because a per-lead node cannot
fan one state into many.

```
START → N0 config load → N1 discovery → N2 enrichment
  N2   ─ unreachable? ─────────────────→ N9 │ N3
  N3   ─ fit_score vs threshold ───────→ N3.5 │ N9 (archive)
  N3.5 → N4 (draft parameterised by channel)
  N4   → N5 human approval  (interrupt / resume, checkpointed)
  N5   ─ approval_status ──────────────→ N5.5 │ N9 (archive)
  N5.5 ─ suppression_status ───────────→ N6a │ N6b │ N9 (archive)
  N6a  ─ channel == both? ─────────────→ N6b │ N7
  N6b  → N7 reply monitoring
  N7   ─ reply_category ──────── interested → N9 + manual handoff
                                 otherwise  → N8
  N8   → N4 (next touch, when due) │ N9
  N9   CRM → END
```

`python -m src.cli.run_batch --tenant example_tenant --graph` prints this as
Mermaid from the compiled graph, so it can never drift from the code.

**N9 is the single exit.** Every terminal path — archived, rejected,
suppressed, unreachable, replied, cadence exhausted, needs-manual-review —
passes through the CRM node before `END`, so no lead can leave the system
without a row explaining what happened to it.

| Node | Does |
|------|------|
| **N0** | Loads and validates `config/tenants/{id}.yaml`. A missing required field **halts** before discovery, naming the field. Never silently defaults. |
| **N1** | Google Places (OSM fallback) for local; Apollo or hand-exported CSV for B2B. Deduplicates against a per-tenant ledger. Thin regions are tagged `low_yield`, not failed. |
| **N2** | Scrapes for contacts and niche-appropriate signals (robots.txt honoured). Hunter.io only for the batch's top-N, hard-capped at 25/month. No email **and** no LinkedIn URL → `unreachable`, excluded downstream. |
| **N3** | LLM scores fit against the niche ICP. Retry once, then `needs_manual_review` rather than blocking the batch. |
| **N3.5** | **Rule-based, no LLM.** Niche default overridden by data availability, narrowed by enabled channels and the region's address rules. |
| **N4** | Drafts channel-appropriate copy citing a **specific** signal. Self-check verifies it; two failures → manual drafting. LinkedIn notes hard-capped at 300 chars by re-prompting, never truncation. Localises via DeepL/LLM. |
| **N5** | `interrupt()` on the per-tenant `SqliteSaver`. Survives process exit; resumable days later. |
| **N5.5** | Runs before **every** send, on **every** channel, on **every** run. Suppression list + region compliance rules. Also auto-suppresses on an opt-out reply. |
| **N6a** | Sends via Gmail SMTP or Brevo. Daily cap per tenant per provider; hitting it queues rather than fails. Region business-hours window — no 3am sends. |
| **N6b** | Queues the message for a human. No LinkedIn automation, ever. |
| **N7** | Polls IMAP/Gmail API. **Out-of-office detected before classification** so an autoreply never counts as a reply. |
| **N8** | Advances the cadence. An `interested` reply exits **immediately** with a same-day manual follow-up. Region touch caps beat cadence length. |
| **N9** | Upserts one row per lead in `CRM_COLUMNS` order, ready for Looker Studio. |

---

## Setup

### 1. Python

**Python 3.12 recommended.** 3.11 and 3.13 work; 3.14 currently risks source
builds for `lxml` and `playwright`.

```bash
# Windows (winget)
winget install --id Python.Python.3.12 --scope user

python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1
# Windows Git Bash
source .venv/Scripts/activate
# macOS / Linux
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Playwright browsers

`pip install` gets the Python package; the browsers are a separate ~150MB
download, needed only for JS-rendered sites during live enrichment. **Dry runs
do not need it.**

```bash
playwright install chromium
```

### 3. Environment

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

**You can run a full dry run right now with an empty `.env`.** Every
integration degrades deliberately when its key is missing:

| Missing key | What happens instead |
|---|---|
| `GROQ_API_KEY` / `GOOGLE_API_KEY` | deterministic `mock` LLM — same prompts, same JSON parsing, same self-check loop |
| `GOOGLE_PLACES_API_KEY` | OpenStreetMap / Nominatim (no key needed) |
| `APOLLO_API_KEY` | CSV import from `data/imports/` |
| `HUNTER_API_KEY` | lookup skipped; scraped address used |
| `DEEPL_API_KEY` | LLM translator |
| `GMAIL_*` / `BREVO_API_KEY` | dry-run sender — prints, sends nothing |
| `IMAP_*` | null reader — no replies found |
| `GOOGLE_SHEETS_SPREADSHEET_ID__<tenant>` | local CSV with the identical header |

Leave **`GLOBAL_DRY_RUN=true`** until you have reviewed real drafts. It is the
kill switch: with it set, N6a and N6b print instead of sending regardless of any
CLI flag or tenant setting.

---

## Free-tier API keys

Signed up in the order you will actually need them.

| Service | Free tier | Where | `.env` variables |
|---|---|---|---|
| **Groq** | generous rate-limited free tier | console.groq.com → API Keys | `GROQ_API_KEY`, `GROQ_MODEL` |
| **Google Gemini** | free tier in AI Studio | aistudio.google.com → Get API key | `GOOGLE_API_KEY`, `GEMINI_MODEL` |
| **Ollama** | free, local, no key | ollama.com, then `ollama pull llama3.1:8b` | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |
| **Google Places (New)** | monthly credit — **set a budget cap** | console.cloud.google.com → enable *Places API (New)* → Credentials | `GOOGLE_PLACES_API_KEY`, `GOOGLE_PLACES_MONTHLY_REQUEST_CAP` |
| **OpenStreetMap** | free, no key; 1 req/sec, real User-Agent required | — | `NOMINATIM_USER_AGENT` |
| **Apollo.io** | free plan (API search not on all accounts) | apollo.io → Settings → Integrations → API | `APOLLO_API_KEY`, `APOLLO_CSV_IMPORT_DIR` |
| **Hunter.io** | **25 lookups/month** | hunter.io → Dashboard → API | `HUNTER_API_KEY`, `HUNTER_MONTHLY_LOOKUP_CAP` |
| **DeepL** | **500k chars/month** | deepl.com/pro-api → Free plan | `DEEPL_API_KEY`, `DEEPL_MONTHLY_CHAR_CAP` |
| **Gmail SMTP** | ~500/day. Needs 2FA + an **App Password**, not your login password | myaccount.google.com → Security → App passwords | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` |
| **Brevo** | ~300/day | brevo.com → SMTP & API → API Keys | `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME` |
| **Google Sheets** | free via a service account | Cloud console → enable Sheets **and** Drive APIs → service account → JSON key | `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SHEETS_SPREADSHEET_ID__<tenant>` |
| **Airtable** | free plan | airtable.com/create/tokens | `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID__<tenant>` |
| **LangSmith** | free tier | smith.langchain.com | `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` |

### Bring your own account

Everything above has a free tier, and a fresh install runs entirely on those.
These do not: they are metered by the provider, they are chosen rather than
fallen back to, and the bill is between the user and the vendor.

They are here because "use whatever you already pay for" is a reasonable thing
to want from a product that stores no keys of its own.

| Service | Cost | Where | `.env` variables |
|---|---|---|---|
| **ChatGPT (OpenAI)** | pay as you go | platform.openai.com → API keys | `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` |
| **Claude (Anthropic)** | pay as you go | console.anthropic.com → API keys | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL` |
| **DeepSeek** | pay as you go, cheapest of the three | platform.deepseek.com → API keys | `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL` |
| **Any SMTP mailbox** | whatever your mail host charges | your host's SMTP settings | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_DAILY_SEND_CAP` |
| **Anymail Finder** | per verified address | anymailfinder.com → API | `ANYMAIL_FINDER_API_KEY`, `ANYMAIL_FINDER_MONTHLY_LOOKUP_CAP` |
| **HubSpot** | free CRM tier is enough | Settings → Integrations → Private Apps | `HUBSPOT_API_KEY` |
| **Pipedrive** | paid, including the trial | Personal preferences → API | `PIPEDRIVE_API_KEY`, `PIPEDRIVE_DOMAIN__<tenant>` |

Two rules keep this safe:

1. **A metered provider is never a fallback.** `FALLBACK_ORDER` in
   `src/integrations/llm.py` contains free tiers and local hardware only, and a
   test asserts it. A workspace that has not chosen to spend money does not
   start spending it because Groq was rate-limited.
2. **Every metered lookup has its own ceiling.** `ANYMAIL_FINDER_MONTHLY_LOOKUP_CAP`
   defaults to 100 and `SMTP_DAILY_SEND_CAP` to 200 — not plan limits, but the
   most a runaway batch can spend before it stops.

**ZoomInfo and Clay are deliberately absent.** ZoomInfo has no self-serve
credential — its API is gated behind a sales contract and authenticated with a
JWT minted from an enterprise key — and Clay has no lookup endpoint at all; its
API pushes rows into a Clay table and enriches them asynchronously, so there is
nothing to call and wait for. For either, point the **Custom endpoint** entry
at your own small shim: it takes a URL and a key, runs server-side, and carries
the same SSRF, timeout and redaction guards as everything else.

**Sheets gotcha:** creating the service account is not enough. You must **share
each tenant's spreadsheet with the service account's email address as an
Editor**, or every write returns 403.

**Sending-domain advice:** use a subdomain (`outreach.yourdomain.com`) for cold
email, never your primary domain. Set up SPF, DKIM and DMARC on it before the
first send, and keep `daily_send_limits` low (the example tenant uses 40/day
against Gmail's 500) — a new domain that jumps to 200/day gets filtered, not
delivered.

---

## Running a dry-run batch

**This is the intended way to test any change before pointing the system at a
real tenant.**

```bash
python -m src.cli.run_batch --tenant example_tenant --dry-run
```

It runs the **full graph** — every node, every conditional edge, real prompts,
real compliance checks — against `tests/fixtures/sample_leads.json` instead of
live discovery, and:

- never sends an email (N6a prints it),
- never queues a real LinkedIn action (N6b prints it),
- never writes the dedupe ledger, so tomorrow's live batch is unaffected,
- never spends a Hunter lookup or a DeepL character.

Useful flags:

```bash
--limit 3                      # cap leads this run
--niche local_salon            # one niche only
--region EU                    # one region only
--no-resume                    # skip in-flight leads whose next touch is due
--graph                        # print the compiled graph as Mermaid and exit
```

The nine fixture leads are chosen so one dry run exercises the awkward paths:
email-only, LinkedIn-only, unreachable, French and Arabic localisation, a
role-based-address compliance veto, a lead with **no signals** (which must fail
the self-check rather than send generic copy), and a deliberate near-duplicate
that dedupe must collapse.

You will see something like:

```
Outcomes: in_sequence_touch_2_scheduled=4  unreachable=1
          needs_manual_review=1  below_threshold=1
          awaiting_manual_linkedin_send=1
```

---

## The approval CLI

Every draft is reviewed by a human before it can be sent (N5).

```bash
python -m src.cli.approve --tenant example_tenant
python -m src.cli.approve --tenant example_tenant --list          # just look
python -m src.cli.approve --tenant example_tenant --channel linkedin
```

The queue is **grouped by channel** and sorted by fit score, showing company,
region, fit reason, every signal found, which signal the copy actually cites,
and the draft itself (with a live character count on LinkedIn notes).

| Key | Action |
|-----|--------|
| `a` | approve as drafted |
| `e` | edit — prompts field by field; `.` keeps the current text, blank line ends input |
| `r` | reject (archived, never sent) |
| `s` | skip — stays queued |
| `q` | quit — everything unreviewed stays queued |

The queue **is** the checkpoint database, so it survives process exit: quit
halfway through on Monday and resume exactly where you were on Thursday. An
over-length LinkedIn note is refused at the edit prompt rather than truncated.

---

## The LinkedIn manual-send queue

```bash
python -m src.cli.linkedin_queue --tenant example_tenant list
python -m src.cli.linkedin_queue --tenant example_tenant show <lead_id>
python -m src.cli.linkedin_queue --tenant example_tenant sent <lead_id>
python -m src.cli.linkedin_queue --tenant example_tenant connected <lead_id>
python -m src.cli.linkedin_queue --tenant example_tenant skip <lead_id>
python -m src.cli.linkedin_queue --tenant example_tenant all
```

Workflow: `list` to see what is waiting, `show` to print one message ready to
copy, send it yourself in LinkedIn, then `sent`.

**`connected` matters operationally.** Cadence step 2 (the post-acceptance DM)
is gated on `requires: connection_accepted`, so it does not become due until you
mark the lead connected. An unaccepted request is abandoned after 21 days
(`abandon_if_unmet_after_days` in `cadences.yaml`) instead of waiting forever.

The queue is capped by `daily_send_limits.linkedin_manual` (15/day in the
example tenant) so it stays a list a person can actually clear.

---

## Adding a new tenant

1. Copy the example and rename it. **The filename must equal `tenant_id`** —
   every per-tenant resource is resolved from that id, and N0 refuses a
   mismatch.

   ```bash
   cp config/tenants/example_tenant.yaml config/tenants/acme_agency.yaml
   ```

2. Edit at minimum:

   | Field | Notes |
   |---|---|
   | `tenant_id` | must match the filename |
   | `regions` | subset of `US, UK, EU, CA, AU, ME` |
   | `language_map` | one entry per region above |
   | `niches[].id` | prefix is meaningful: `local_*` → email-first, `b2b_*`/`saas_*` → LinkedIn-first |
   | `niches[].icp` | `description` and `good_signals` are what N3 scores against and N4 cites |
   | `niches[].discovery` | `search_terms` + `locations` for local; `titles` + `locations` for B2B |
   | `sending_identity` | from-name, from-email, company name, **physical address** (required by US/CA), LinkedIn account label |
   | `fit_score_threshold` | below this, leads archive |
   | `daily_send_limits` | keep well under the provider ceiling |

3. Register it and point its resources at it:

   ```bash
   LEADGEN_TENANTS=example_tenant,acme_agency
   GOOGLE_SHEETS_SPREADSHEET_ID__acme_agency=<sheet id>
   ```

4. Validate before running anything:

   ```bash
   python -m src.cli.run_batch --tenant acme_agency --dry-run --limit 3
   ```

   N0 reports **every** problem at once, so you fix the config in one pass:

   ```
   Halted before discovery:
   tenant config .../acme_agency.yaml is invalid; halting before Discovery.
     - missing required field: tone
     - language_map is missing an entry for region 'ME'
     - niches[b2b_ops].discovery: b2b niches need 'titles' for role-based targeting
   ```

---

## Going live

Work through this in order. Do not skip step 3.

1. **Dry run and read the output.** Every draft, start to finish.
   ```bash
   python -m src.cli.run_batch --tenant <t> --dry-run
   ```
2. **Set up the sending domain.** Subdomain, SPF, DKIM, DMARC. Warm it up.
3. **Send yourself a test.** Set `GMAIL_*`, put your own address in a temporary
   tenant config, set `GLOBAL_DRY_RUN=false`, and run with `--limit 1`. Check
   what arrives — the footer, the opt-out line, the postal address.
4. **Check `config/compliance_profiles.yaml` against your legal advice.** It is
   machine-checkable rules, not legal advice. Have a lawyer review your
   programme for the regions you operate in.
5. **Set a real `fit_score_threshold`** and low `daily_send_limits`.
6. **Turn off the kill switch** — `GLOBAL_DRY_RUN=false` — and run a small live
   batch with `--limit 5`.
7. **Approve manually** for the first several batches. Keep
   `compliance.require_approval_before_send: true` permanently.
8. Only then schedule it.

`runtime.auto_approve_in_dry_run: true` must **never** be relied on for a live
run. It only takes effect when the lead is in dry-run mode, and N6a/N6b refuse
to send under `GLOBAL_DRY_RUN` regardless — but the flag exists purely so an
unattended dry run can exercise the whole graph.

---

## Scheduling

### Local cron / Task Scheduler (recommended for real clients)

`scheduler/run_cron_example.sh` handles venv discovery, per-tenant locking (two
concurrent batches would double-send), logging to `.data/logs/`, and printing
both queues at the end.

```bash
chmod +x scheduler/run_cron_example.sh

# crontab -e — weekdays 09:15, one line per tenant
15 9 * * 1-5 /path/to/leadgen_agent/scheduler/run_cron_example.sh example_tenant
```

Windows Task Scheduler:

- **Program:** `C:\Program Files\Git\bin\bash.exe`
- **Arguments:** `-lc "/c/Users/you/Desktop/leadgen_agent/scheduler/run_cron_example.sh example_tenant"`
- **Start in:** the repository root
- Tick *Run whether user is logged on or not*

### GitHub Actions

```bash
mkdir -p .github/workflows
cp scheduler/github_actions_workflow.yml .github/workflows/leadgen.yml
```

Then in your repository settings:

- **Secrets** (Settings → Secrets and variables → Actions → Secrets): add only
  the keys you have — `GROQ_API_KEY`, `GOOGLE_PLACES_API_KEY`, `HUNTER_API_KEY`,
  `DEEPL_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `IMAP_USERNAME`,
  `IMAP_PASSWORD`, `LANGSMITH_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the
  whole JSON), `GOOGLE_SHEETS_SPREADSHEET_ID`.
- **Variables**: `LEADGEN_DEFAULT_TENANT`, and `LEADGEN_LIVE` — which stays
  unset or `false` until you genuinely want scheduled sending. The workflow
  writes `GLOBAL_DRY_RUN=true` unless `LEADGEN_LIVE` is exactly `true`.

Run it by hand first: Actions → *leadgen batch* → *Run workflow*.

**Caveat.** Runners are ephemeral, so the checkpoint database is persisted
through `actions/cache`. A cache is best-effort storage, not a database — losing
it loses in-flight cadences and pending approvals. Use Actions for scheduled
**dry-run regression checks**, and run live batches from the always-on box.

---

## Analytics with Looker Studio

N9 writes one row per lead in `CRM_COLUMNS` order with clean headers, enum
values as plain strings, `TRUE`/`FALSE` booleans, `a | b` for list fields, and
ISO-8601 timestamps — designed so no transformation layer is needed.

1. Create one Google Sheet per tenant. Share it with your service account email
   as an **Editor**. Put its id in `GOOGLE_SHEETS_SPREADSHEET_ID__<tenant>`.
2. Run a batch. The `Leads` and `Suppression` worksheets are created
   automatically with the correct headers.
3. In Looker Studio: *Create → Data source → Google Sheets*, pick the sheet and
   the `Leads` worksheet, tick *Use first row as headers*.
4. Set field types: `fit_score` → Number; `discovered_at`, `sent_at`,
   `last_touch_at`, `next_touch_due`, `last_updated` → Date & Time; everything
   else Text.

Charts worth building first: reply rate by `region`, by `niche_id`, and by
`channel`; `fit_score` distribution split by `reply_category`; `send_status`
funnel; count of `suppression_status != clear` (your compliance health);
`needs_manual_review` over time (your drafting quality).

Without a sheet configured, the same rows land in
`.data/dry_run/<tenant>__crm.csv` — the identical header, uploadable by hand.

Optionally scaffold a local Streamlit viewer over that CSV; `streamlit` is
already in `requirements.txt`.

---

## Deliverability checks

These are **manual, periodic checks**, not API calls. Do them weekly while
warming a domain.

| Tool | What for | How |
|---|---|---|
| **mail-tester.com** | full spam-score audit of one message | Get the throwaway address it shows, send a real draft to it via `--limit 1` with that address in a test tenant, then read the score. Aim for 9+/10. Fix SPF/DKIM/DMARC and any spam-word flags. |
| **Google Postmaster Tools** | domain and IP reputation, spam-rate, DKIM/DMARC pass rates, as Gmail sees you | postmaster.google.com → add `outreach.yourdomain.com` → verify with the TXT record. Needs volume before it shows data. **Keep the spam rate under 0.1%.** |
| **MXToolbox** | DNS and blacklist health | mxtoolbox.com/SuperTool.aspx — run *SPF Record Lookup*, *DKIM Lookup* (selector `google` for Gmail), *DMARC Lookup*, and *Blacklist Check* on the sending domain and its IP. |

If the spam rate climbs or you appear on a blacklist: stop sending, cut
`daily_send_limits`, re-check that opt-outs are actually being honoured
(`python -m src.cli.linkedin_queue` is unrelated — check the suppression file
under `.data/suppression/`), and warm up again more slowly.

---

## Multi-tenancy

Non-negotiable, and enforced structurally rather than by convention.

| Resource | Resolution |
|---|---|
| tenant config | `config/tenants/{tenant_id}.yaml` |
| checkpoint DB | `.data/checkpoints/{tenant_id}.sqlite` |
| suppression list | `.data/suppression/{tenant_id}.json` |
| dedupe ledger | `.data/counters/{tenant_id}__seen_leads.json` |
| daily send counters | `.data/counters/{tenant_id}__sends__{provider}.json` |
| LinkedIn queue | `.data/queues/{tenant_id}__linkedin.json` |
| CRM sheet / base | `GOOGLE_SHEETS_SPREADSHEET_ID__{tenant_id}` |

Three mechanisms back this up:

- `settings._assert_safe_tenant_id` rejects a `tenant_id` containing anything
  but letters, digits, `_` and `-`, so it can never traverse out of its
  directory.
- `SuppressionList` takes a `tenant_id` in its constructor and has **no**
  cross-tenant read path — the capability does not exist to be misused. A file
  whose stored `tenant_id` disagrees with its filename is refused outright.
- `lead_id` is `sha1(tenant_id + dedupe_key)`, so the same company discovered
  for two tenants yields two distinct leads that cannot collide on a graph
  thread, a CRM row or a suppression entry.

`tests/test_suppression_gate.py` proves tenant A's list is never consulted for
tenant B's leads **and** the reverse.

---

## Free-tier limits enforced in code

Each is a durable counter (`src/counters.py`) storing a count plus a period key,
written atomically so a process killed mid-write cannot silently reset a budget.

| Limit | Value | Counter | On exhaustion |
|---|---|---|---|
| Hunter lookups | 25/month | `hunter_lookups` | lookup skipped; scraped address used |
| DeepL characters | 500k/month | `deepl_chars` | falls back to the LLM translator |
| Google Places requests | 2000/month (configurable) | `places_requests` | falls back to OpenStreetMap |
| Gmail sends | 500/day, **per tenant** | `{tenant}__sends__gmail_smtp` | `rate_limited`, queued for the next run |
| Brevo sends | 300/day, **per tenant** | `{tenant}__sends__brevo` | same |
| LinkedIn manual actions | tenant config (15/day) | queue timestamps | held until the next run |

The tenant's own `daily_send_limits` always wins when it is *lower* than the
provider ceiling. Hunter is additionally budgeted by `hunter_top_n_per_batch`,
and `select_hunter_candidates` clamps to whatever is actually left — asking for
5 when 2 remain spends 2, on the leads with no other contact route.

---

## Testing

```bash
pytest                       # 510 tests
pytest tests/ -v
pytest tests/test_suppression_gate.py -v
```

| File | Tests | Covers |
|---|---|---|
| `test_state.py` | 49 | the data contract, enums, dedupe keys, CRM projection |
| `test_config_load.py` | 38 | N0 halt-on-missing-field, cadence merging, batch targets |
| `test_discovery_enrichment.py` | 38 | N1/N2, dedupe, low-yield, signal detection, Hunter budgeting |
| `test_qualification.py` | 30 | N3 scoring, score coercion, threshold routing |
| `test_channel_selection.py` | 56 | N3.5 — the full truth table, exhaustively |
| `test_personalization.py` | 42 | N4 self-check, the 300-char cap, localisation; N5 interrupt/resume across a simulated restart |
| `test_suppression_gate.py` | 54 | N5.5, compliance rules, auto-suppression, **multi-tenancy both ways** |
| `test_outreach.py` | 31 | N6a business hours and daily caps; N6b queue, and that it never automates LinkedIn |
| `test_replies_and_sequencer.py` | 58 | N7 OOO-before-classification, N8 cadence, the interested-reply exit |
| `test_graph_e2e.py` | 40 | the compiled graph's edges, and a full dry-run batch |
| `test_nodes_with_mocked_apis.py` | 53 | every integration: provider fallback, quotas, error degradation |
| `test_cli.py` | 21 | all three CLIs, including a real interrupt/resume through the review loop |

`test_nodes_with_mocked_apis.py` monkeypatches `socket.connect` to raise, so a
test that accidentally reaches the network fails loudly rather than quietly
spending your free-tier quota.

---

## The dashboard

Everything above is the command line. There is also a web dashboard —
**LeadFlow** — for the people who will actually use this: it is written for a
business owner, not for whoever maintains the pipeline, and it never mentions a
framework, a processing step or an internal identifier.

Two processes:

```bash
# 1. the settings service — stores API keys encrypted, edits targeting
pip install -r requirements.txt -r backend/requirements.txt
python -m uvicorn backend.main:app --port 8000 --reload

# 2. the dashboard, in a second terminal
cd dashboard
npm install
npm run dev            # http://localhost:5173
```

The dev server proxies `/api` to port 8000, so both use the same same-origin
path they will use behind a reverse proxy in production.

**Set `SECRETS_MASTER_KEY` in `.env` before saving any API key through the UI.**
It encrypts them, and losing it makes every saved key permanently
undecryptable. Left blank, one is generated into `.secrets/master.key` — fine
for a single machine, not for anything that matters. See
[backend/README.md](backend/README.md).

### What it is for

| Screen | What it does |
|---|---|
| New | Describe the businesses you want, in your own words. Submitting runs the search. |
| Leads | Every search you have run, and the flat table of every business found. |
| Email | Messages waiting on you, follow-ups due, sent and replied. Approve, edit or reject on the row. |
| LinkedIn | The same, for messages **you** send by hand from your own account. |
| Settings | Connections, and nothing else. |

### Bring your own keys

Connections is real, not a mock. Each provider is tested with one live call
*before* anything is stored, then encrypted into
`config/tenants/<workspace>/secrets.enc`. A saved key is never returned by any
endpoint — you see its last four characters and nothing more.

`src/settings.py` reads that store **first** and `.env` **second**, so a key
entered in the UI takes effect on the next run with no file editing, and a
headless install that never opens the dashboard keeps working exactly as
before.

### Targeting is an editor for your config file

Audiences created in the dashboard are written into
`config/tenants/<workspace>.yaml` — the same file the agent has always read,
comments preserved, validated with the agent's own validator before the write
lands. There is no shadow copy of your configuration.

Any industry works. Describe "boutique law firms in Chicago with outdated
websites" or "machine shops still taking quotes by fax" and it becomes
searchable; there is no built-in list of verticals anywhere in the product.

### What it will not do

- **No LinkedIn automation.** The dashboard writes the message and you send it
  from your own account. There is no Send button on that screen and there never
  will be — see [Design notes](#design-notes).
- **No sending without your approval**, unless you deliberately turn that off.
- **Test Mode by default.** Live Mode takes a second, explicit confirmation.

---

## Design notes

Things that look like choices but are constraints.

- **`LeadState` is a `TypedDict`, not a Pydantic model.** LangGraph merges
  partial dict updates and the checkpointer serialises state every super-step;
  a TypedDict keeps nodes terse and state trivially serialisable, which is what
  makes the N5 interrupt resumable across days. Validation is bought back
  explicitly with `validate_lead_state()` at the boundaries that matter.
  Pydantic is still used *inside* nodes for parsing LLM output.
- **Enums are stored as plain strings.** `Channel.EMAIL` exists for node code;
  state holds `"email"`. Checkpoints, Sheets rows and JSON fixtures round-trip
  identically.
- **`@node` re-raises `GraphBubbleUp`.** Its catch-all implements Section 8, but
  `interrupt()` raises a `GraphInterrupt` through it — swallowing that would
  silently turn every approval pause into a `needs_manual_review` flag.
- **No node takes a parameter named `config`.** LangGraph reserves that name for
  `RunnableConfig` and would inject its own. Tenant config arrives as
  `tenant_config`.
- **N4's compliance footer is appended after translation and is not itself
  translated by the model.** N5.5 matches exact phrases from the region profile;
  a translator paraphrasing "legitimate interest" would fail an otherwise sound
  draft. Localised variants come from `FOOTER_PHRASES` verbatim, so
  `compliance_profiles.yaml` and `FOOTER_PHRASES` are a matched pair — a new
  language needs an entry in both.
- **The N8 → N4 loop only fires when the touch is actually due.** Looping
  unconditionally would run a three-touch cadence inside one invocation and send
  everything in the same minute.
- **`sequence_step` counts completed touches and only N8 changes it**, so inside
  N8 the touch being *scheduled* is `next_step + 1`. Getting that off by one
  silently skips a cadence step.
- **`select_hunter_candidates` is batch-level.** "Top 5 of this batch" is
  inherently comparative; a lead cannot know whether it is in the top 5 of a
  batch it cannot see.
- **N9 never fails a lead.** The row is the record of work already done; losing
  the record is bad, re-sending an email because Sheets was down is worse.
- **`objection` does not auto-suppress, but opt-out language does** — in any
  supported language, whatever the classifier decided. An objection is a live
  sales conversation; "please remove me" is not.
- **A dry run writes nothing durable.** No ledger, no queue, no quota spend, so
  it can be run as often as you like without affecting the live book.

---

## Troubleshooting

**`Halted before discovery: ... is invalid`** — N0 lists every problem at once.
Fix them all and re-run; it never silently defaults.

**Everything comes back `blocked_compliance`** — read
`manual_review_reason`. Usually `sending_identity.physical_address` is empty
(required by US and Canada) or the region needs a role-based address
(`info@`, `hello@`) and you have a personal one.

**Everything comes back `needs_manual_review` from N4** — the drafts are not
citing a specific signal. Check that N2 actually found signals: a site that
blocks scraping or is entirely JS-rendered yields none, and the self-check
correctly refuses to send generic copy. Install the Playwright browser, or add
signals by hand.

**`unknown niche_id`** — the fixture or the lead references a niche the tenant
config does not define. Niche ids must match exactly between
`sample_leads.json` and the tenant's `niches[].id`.

**No leads discovered on a live run** — without `GOOGLE_PLACES_API_KEY` you are
on OpenStreetMap, which has far thinner business data and often no website.
Either add a Places key (with a budget cap) or use the CSV import path.

**Approval queue is empty after a dry run** — dry runs auto-approve so the graph
can complete unattended. Use a live run (with `GLOBAL_DRY_RUN=true` still set,
so nothing sends) to see the interrupt behaviour.

**LangSmith project is empty** — `LANGSMITH_TRACING=true` **and**
`LANGSMITH_API_KEY` are both required. The run banner prints `tracing: on|off`.

**`ZoneInfoNotFoundError` / timezone warnings on Windows** — Windows ships no tz
database. `tzdata` is in `requirements.txt`; re-run `pip install -r
requirements.txt`.

**Sheets writes return 403** — the spreadsheet is not shared with the service
account. Open the sheet, Share, paste the `client_email` from the JSON key,
Editor.

---

## Licence and intent

Built to be run and resold as a service. It is deliberately conservative in the
three places that carry real risk: LinkedIn is never automated, nothing is sent
without a human approving it, and an opt-out is honoured automatically rather
than depending on anyone remembering.
