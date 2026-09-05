# LeadFlow — the dashboard

A lead-generation and outreach product for the person who owns the business,
not for whoever maintains the pipeline behind it.

That distinction is the whole design brief. A cleaning company, a law firm, a
dental clinic or an agency should be able to open this, plug in their own name
and their own API keys, and use it without ever learning what any of the
machinery is called.

```bash
# the settings service, from the project root — see backend/README.md
python -m uvicorn backend.main:app --port 8000 --reload

# the dashboard, in a second terminal
npm install
npm run dev            # http://localhost:5173
npm run build          # zero TypeScript errors, zero warnings
npm run api            # shorthand for the command above
```

The dev server proxies `/api` to port 8000, so the client uses the same
same-origin path in development that it will use behind a reverse proxy in
production. No CORS special case, no environment-specific base URL.

---

## The two rules that shape everything

### 1. No internal vocabulary reaches a user

The product describes what is happening to the user's leads, in the words they
would use. Not the framework, not "state", not step names, not step numbers —
and there is no advanced view that brings them back.

`src/lib/statusLabels.ts` is the **only** place a backend value becomes
English. `blocked_optout` stays `blocked_optout` in the data model, because
that fidelity is what makes the service layer swappable; it becomes "Opted out
— will not be contacted" at render time and nowhere else.

This is enforced, not asserted. A Playwright pass loads every route and greps
the rendered DOM — visible text **plus** `title`, `aria-label`, `placeholder`
and `alt` — for a list of banned patterns, because a string that survives in a
tooltip is exactly what a visual check misses:

```
--- banned vocabulary in the rendered UI ---
  none
```

Two real leaks that check caught, both of which look fine on screen:

- **`next_action` rendered raw.** The backend writes operator lines like
  `archived: fit 43 is below the tenant threshold 60`. The leads table was
  showing them verbatim in the "Next step" column. It now shows
  `nextStepFor(lead).headline`, which is a recommended action rather than a
  debug string — and is more useful anyway.
- **`[FR translation pending - dry run]`** inside the drafted copy. The marker
  itself is faithful and stays; the words "dry run" came out.

### 2. Nobody's identity is baked in

There is no sample person and no placeholder person anywhere. The Home greeting
is name-free until the user tells the product their name. Every sender
identity — the email "From" line, the postal address in the footer, the
LinkedIn account label — is read live from the account row at the bottom of
the sidebar, which is now the only editor for it, and where it is empty the
UI says so:

> Add your name

Message bodies **stop where the signature would start**. The signature and the
required footer are composed at render time from the profile. A name baked into
a message body is a name that can end up on real mail.

The shipped `config/tenants/example_tenant.yaml` carries template values
(`[your name - set in Settings]`, `set-your-sending-address@example.invalid`)
rather than blanks, because the agent's validator requires those fields — a
batch with no sender identity produces real email to real strangers with
nothing identifying who sent it. The backend recognises the templates and
reports them as empty, which is what makes a fresh install show a genuinely
blank profile with its prompts.

Prospect data used to be the exception -- sample *leads* were kept, on the
grounds that they are not the user's identity. They are gone now too. See
[No sample data](#no-sample-data).

---

## Layout

```
src/
  types/        lead.ts        a 1:1 mirror of the backend contract
                agent.ts       runs, queues, progress
                workspace.ts   the user's own configuration
                campaign.ts    UI-only groupings, kept separate on purpose
  services/     types.ts       two contracts: LeadgenApi and WorkspaceApi
                localApi.ts    lead data, held for the session, starts empty
                api.ts         the real HTTP client for lead data (unused, typed)
                workspaceApi.ts  the REAL client for the user's config
                index.ts       the single wiring decision
  lib/          statusLabels.ts  the ONLY place a value becomes English
                matchQuality.ts  the six buckets, against real fields
                leadFields.ts    the primitive yes/no questions about a lead
                outcome.ts       what happened, and what to do next
                format.ts        numbers, dates, text — no labels
  components/   layout, find, leads, settings, ui
  pages/        Home, Leads, Email, LinkedIn, Settings — and a redirect
                for every route the product used to have
```

### Two service contracts, and why

**`LeadgenApi`** — lead and campaign data. Served by `localApi.ts`, which holds
it in the browser for the session and **starts empty**. `api.ts` is the real
HTTP implementation, fully typed and compiled, and documents which existing
backend function each route would wrap. Swapping them is one line:

```ts
// src/services/index.ts
const USE_IN_SESSION_LEAD_DATA = true;   // flip, set VITE_API_BASE_URL, done
```

Both implement the same interface, so the compiler checks the swap.

**`WorkspaceApi`** — the user's business profile, API keys, audiences and rules
for Leo. **This one is real, always, and has no mock.** A key that appears to
save but has not is worse than an error, and a rule that vanishes on reload is
not a saved rule. When the service is not running, every screen says exactly
that and gives the command to start it, rather than showing a form that
silently fails.

---

## Product rules the code enforces

**Test Mode and Live Mode never look alike.**
`components/layout/ModeIndicator.tsx` owns all four treatments — the top-bar
pill, the banner above every message body, the inline row tag, and the chooser.
Test Mode carries **diagonal hatching as well as colour**, because colour alone
is not enough of a difference for something this consequential, and it survives
a colourblind viewer, a bad monitor and a screenshot. Choosing Test Mode is one
click; choosing Live Mode is two, and the second spells out what it means.

**LinkedIn is never presented as automatic.** There is no Send button on
`/linkedin` and there never will be. The verbs are "Copy message",
"Open LinkedIn", "I sent it", "They accepted" — each describes something a
person did. The screen says so outright rather than leaving the user to infer
it, and an interaction test asserts a bare `Send` button does not exist.

**Nothing runs before the user has seen what was understood.** Find Leads has
three states — compose, review, running — and review cannot be skipped.
Somebody who has just typed a sentence about their own business has to see it
read back correctly before they will trust anything after it.

**Pre-flight fails before the button, not after.** `components/find/Preflight.tsx`
works out what is missing *for this particular plan* and disables the run with
the specific remedy ("This needs an AI connection. **Connect one**").
Blocking issues stop the run; warnings do not, because Test Mode genuinely does
not need a mailbox and saying otherwise would be false.

**A suppressed lead is not a lead status.** Anyone who has opted out, or whom
local rules forbid contacting, is absent from the active list rather than shown
as one state among many — presenting "Opted out" next to "Good match" invites
somebody to try anyway. A footnote says how many are hidden and why.

**No API key is readable.** Settings shows a provider's state and the last four
characters of what was saved. Nothing more is available to this client, from any
endpoint. Editing a connection starts from an empty field, because the stored
value cannot be fetched at all.

**Every number uses tabular figures** through the `<Num>` primitive (JetBrains
Mono, `font-variant-numeric: tabular-nums`), so columns of scores line up.

---

## Match quality

The six buckets are defined against real backend fields, never an invented
threshold — the bar is always the user's own `fit_score_threshold`. Two details
in `lib/matchQuality.ts` are worth knowing:

**`approval_status` defaults to `pending`.** It is the backend's initial value,
not a signal, so "needs review" also requires that a draft exists. Without
that, every business found in the last five minutes would claim to be waiting
on the user.

**Not every lead has a bucket.** A lead you rejected, having scored 82, is not
a good match (it is archived), is not "not a match" (it cleared the bar), and
is not unreachable. Rather than stretch a bucket or invent a seventh,
`matchQuality` returns `null` and the caller shows the lead's outcome instead —
which reads better there anyway.

One real bug found while testing this: `isAwaitingReview` originally also
required `!last_touch_at`, which excluded every lead part-way through a
sequence. Approval is required for **every** touch, not just the first, so that
emptied the approvals queue of exactly the follow-ups somebody needed to
action — and did it silently, with the screen cheerfully saying "you are all
caught up".

---

## Design system

Tokens in `src/index.css`, wired into Tailwind in `tailwind.config.js`. Light,
whitespace-driven, and built on Tailwind's stock **slate** scale plus exactly
one custom teal. Emphasis comes from weight, size and space; the teal is spent
on one primary action per screen and nothing else.

| Token | Value |
|---|---|
| `--bg` / `--bg-page` | `#FFFFFF` / `#F8FAFC` (slate-50) |
| `--border` / `--border-strong` | `#E2E8F0` / `#CBD5E1` |
| text primary / secondary / muted | `#0F172A` / `#475569` / `#64748B` |
| `--accent` / `--accent-contrast` | `#20808D` / `#FFFFFF` |
| `--accent-light-bg` / `--accent-soft-bg` | `#F0FDFA` / `#E6F5F6` |
| `--accent-logo-lt` | `#39C8C2` — the logo mark's gradient only |

Typeface is Inter throughout. **No monospace anywhere, and no tabular
numerals**: `.num` survives as an empty class so its call sites keep compiling,
and figures now render in the same face as the prose around them.

**Deliberately absent**, all of which the previous design had: dark surfaces,
heatmap cells, thick coloured section dividers, a second accent, and the
direction-aware KPI delta. A number on a card is now a number.

**Three compositions, in `components/ui/patterns.tsx`,** shared by Leads, Email
and LinkedIn so the three screens cannot drift into looking like three
products:

- **`KpiCard` / `KpiRow`** — a circular teal-tinted icon badge, the label, then
  the number. `value` is a string so the caller chooses between `0` and `--`;
  an average rating over no leads is `--`, because printing `0.0` there states
  something false.
- **`TabPills`** — filled pills, not underline tabs. The active one is the only
  solid accent object in its region, which is what lets it be read without a
  label saying "selected".
- **`PanelEmpty`** — the full-width bordered card that *is* the page when the
  page has nothing in it: soft teal icon badge, bold heading, one muted line,
  one solid CTA. Distinct from `EmptyState`, which is the small dashed
  placeholder used *inside* a widget.

**Radius is a three-step scale, not one value**: `6px` controls, `10px` cards,
`16px` the composer. `Card` takes a `weight` prop so importance is expressed
through shape.

**Accent is rationed.** On Settings, only the row the product genuinely cannot
run without gets a primary button; the others are secondary. A column of
identical teal buttons would make the AI connection look no more urgent than
the ones most people never touch.

Motion is 150–200ms and only ever in response to an interaction: the
composer's send button on press, a suggestion card lifting on hover, a tab pill
on tap. Nothing animates ambiently. No glows.

## Screens

Four, and Settings. Every string quoted in the spec's Home and Leads sections
is used verbatim rather than paraphrased.

| Screen | Route | Shape |
|---|---|---|
| Home | `/` | Greeting, one centred composer, the reach line, six suggestion cards in a 3x2 grid, and "Refresh suggestions". Submitting **runs the search** — there is no confirmation step between typing and running. |
| Leads | `/leads` | Date range, Filter, New Lead; five KPIs; then two tabs — "Filtered leads" lists the searches themselves, "All leads" is the flat table. |
| Email | `/email` | Waiting for Review, Follow-ups Due, Sent, Replied, then "Needs attention" / "All email". Approve, Edit and Reject happen on the row. |
| LinkedIn | `/linkedin` | Waiting for Review, Ready to Send, Waiting to Connect, Follow-ups Due. Copy Message / Open LinkedIn / Mark as Sent, then Mark as Connected. **Never a Send button.** |
| Settings | `/settings` | Connections and nothing else: a plain list grouped by job, each row a name, one line of purpose, and Connect/Connected. |

**There is no Lead Detail page.** Every action happens on the list row, and a
lead's signals, message and history expand in place — which costs the user
neither their scroll position nor their filters.

## No sample data

There is none, anywhere. `data/mockData.ts` — 900 lines of businesses, contact
names, draft messages, runs, activity and free-tier usage, transcribed from a
real dry run — has been deleted, and `localApi.ts` starts empty.

Every roll-up is computed from what is actually present: the four numbers on
Home, the funnel, the campaign cards, every chart on Performance, the sending
budget. At rest that is nothing, so each screen falls through to the empty
state that was already built for it, and Home shows its first-run checklist.

Three pieces of invention went with it, and they are the reason this matters
more than tidiness:

- **A fixed `+14% / +9% / +25%`** on the Home cards, which described a movement
  between two runs when there had only ever been one.
- **A synthetic ramp** behind the first eleven days of every Performance chart,
  so the line had a shape. A footnote admitted it. A chart that draws work
  nobody did is worse than a flat one.
- **`2 of 25` Hunter lookups and `2 of 15` LinkedIn actions**, the only sample
  numbers a user could act on — deciding not to run a search because a made-up
  bar looked nearly full. `getQuotas` now returns nothing, and the screen's own
  words ("These appear once a mailbox has been connected and used") are true.

The audiences that used to sit in `data/workspaceDefaults.ts` are gone for the
same reason. They were shown only when the settings service was unreachable,
but four industries nobody had chosen is the product inventing the user's
configuration back at them. Sending rules stay — a region list and a follow-up
plan are things a fresh workspace genuinely ships with. With no audiences,
Find Leads blocks with "You have not described anyone to look for yet", which
is the truth.

Mutations (approving a draft, marking a LinkedIn item, starting a run) still
update the in-memory copy, so the product behaves like a real system within a
session, and a reload clears it.

---

## What was removed

The pipeline visualisation that used to live at `/agent/runs/:id` — a React
Flow canvas of the backend's 13 processing steps — is gone, along with
`@xyflow/react`, the route and every step identifier. It is archived in
`_reference/graph-view/`, outside `src/`, where Vite never sees it and `tsc`
never checks it. There is no advanced mode that brings it back.

The same information is now sequential progress in plain language:
"Finding businesses" → "Checking how well they match" → "Preparing outreach".

Removed when the product was cut back to four screens: the Overview
dashboard, the separate Find Leads screen and its mandatory
plan-confirmation step, the Lead Detail page, the standalone Approvals and
Follow-ups queues, Performance, Activity, Business Profile, Targeting, Rules
for Leo, the Leo widget, and the top bar. Every one of those routes still
resolves — to whichever screen absorbed it — so an old bookmark lands
somewhere sensible rather than on a 404.

The **Translation capability is gone from the UI entirely**: no DeepL row, no
per-message language selector, no language tag in the leads table. The
backend capability is untouched; nothing in the product offers it.

Also gone: the workspace switcher and any sign-up flow. One deployment serves
one business. The backend still namespaces its files by an internal id, and the
client carries it as `scope` on every read, but nothing ever asks the user to
pick one and it appears only under "Technical details".

---

## Verification

Checked with a real browser and a real backend, not by inspection.

- **Five screens plus a redirect for every retired route** — zero TypeScript
  errors, zero console errors, zero page errors.
- **Banned vocabulary: none**, across visible text *and* attribute values on
  every route. The sweep also asserts the provider names the spec removed from
  the UI (Groq, Ollama, DeepL) do not appear — which caught Gemini describing
  itself as doing "the same job as Groq".
- **Zero horizontal overflow at 390px, 820px and 1440px.** Tables become cards
  and the sidebar becomes a drawer below `lg`.
- **Interaction flows pass**, including the ones that need the real service:
  setting a name in the account row and seeing it in the Home greeting; the
  sidebar collapse surviving a reload; the session search toggle; the Leads
  tabs actually switching. That last one found a genuine bug — the tab shared
  a URL setter with the filters, whose "no filter" sentinel is also spelled
  `all`, so selecting "All leads" deleted its own parameter and bounced
  straight back to the first tab.

The backend's **534 tests still pass**. The only Python touched was user-facing
copy in the provider registry: three `free_tier` strings that said "enforced in
code", and Gemini's purpose line naming a provider that is no longer shown.
