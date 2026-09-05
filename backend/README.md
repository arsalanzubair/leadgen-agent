# backend/ — the settings service

A small FastAPI app that does the three things in the dashboard which have to
*really* work rather than look right:

- store the user's own API keys, encrypted;
- edit who they want to find;
- remember the standing instructions they give Leo.

Everything else the dashboard shows is lead data, which it reads from its own
sample layer until the agent grows an HTTP surface of its own.

It **imports from `src/`** rather than duplicating anything — the same config
loader, the same validator, the same LLM client. A change to the agent's rules
is a change here too, automatically.

```bash
pip install -r requirements.txt -r backend/requirements.txt
python -m uvicorn backend.main:app --port 8000 --reload
```

The dashboard's dev server proxies `/api` to port 8000, so the client uses the
same same-origin path in development that it will use behind a reverse proxy in
production. No CORS special case, no environment-specific base URL.

---

## The one thing that must not be got wrong

`SECRETS_MASTER_KEY` encrypts every saved credential. **Generate it once, keep
it, and never commit it.**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put it in `.env` as `SECRETS_MASTER_KEY=...`, or better, in whatever secret
manager the host provides.

If it is not set, the service generates one into `.secrets/master.key` on first
run with owner-only permissions. That is a convenience for a single machine,
not a design: **Windows ignores the permission bits**, and a key sitting next
to its own ciphertext protects against very little. Prefer the environment
variable anywhere that matters.

**Lose the master key and every saved credential is unrecoverable.** They are
not damaged, they are simply undecryptable, and the only remedy is re-entering
them in Settings → Connections. The store says so explicitly when it hits an
`InvalidToken` rather than reporting the key as absent — a credential that
silently reads as empty looks exactly like one that was never set, and costs
somebody an afternoon.

`.gitignore` already excludes `.secrets/` and `config/tenants/*/`.

---

## How a key travels

```
Connections screen  ──POST /integrations/{id}/test──▶  one real call to the provider
                                                        nothing saved
                    ──POST /integrations/{id}───────▶  check runs AGAIN, then
                                                        Fernet-encrypted to
                                                        config/tenants/<id>/secrets.enc
agent run           ──src.settings.env("GROQ_API_KEY")─▶ encrypted store first,
                                                          .env second
```

Three properties hold, and the code is arranged so they are hard to break:

**A saved key is never returned by any endpoint, in any form.** Not masked, not
partially, not on request. `GET /api/integrations` returns a state and a
`last4`, and `last4` is four characters from the *end* of the value — enough to
recognise which key you pasted, useless for reconstructing it. (Taking the
*start* would be worse: `sk-`, `AIza` and `gsk_` prefixes are the identifying
part.)

**Nothing is stored without a passing test.** The client disables Save until a
test succeeds, but a disabled button is a courtesy — `POST /integrations/{id}`
re-runs the check server-side and refuses on failure. That is the actual gate.

**`secrets_store.get()` has exactly one intended caller**: `src/settings.py`,
inside the agent process that needs the plaintext to make a real API call. No
route in this package calls it.

---

## Routes

| Route | What it does |
|---|---|
| `GET /api/health` | Is the service up, is the store readable, where the master key came from. Never a key. |
| `GET /api/workspace` | The internal namespace, for the Technical details panel. |
| `GET /api/integrations` | Every provider and whether it is connected. Status only. |
| `POST /api/integrations/{id}/test` | One real, cheap call. Saves nothing. |
| `POST /api/integrations/{id}` | Re-checks, then saves encrypted. |
| `DELETE /api/integrations/{id}` | Forgets it. |
| `GET/PUT /api/profile` | The business profile. |
| `GET/POST/PUT/DELETE /api/niches` | The audiences. |
| `POST /api/niches/draft` | Description → structured audience, using the *user's own* AI key. Returns a draft; saves nothing. |
| `GET/PUT /api/rules` | Match bar, approval requirement, blocked domains, follow-up plan. |
| `GET/POST/DELETE /api/agent-rules` | Leo's standing instructions. |

---

## Editing the config, not shadowing it

`workspace.py` is an **editor** for `config/tenants/<id>.yaml`. There is no
second copy of the user's configuration anywhere.

Two things it is careful about:

**Comments survive.** That file is heavily commented, and those comments are the
documentation for anybody who opens it by hand. Writes go through ruamel's
round-trip loader; PyYAML's `safe_dump` would strip every one of them.

**Writes are validated first, and atomic.** The new document is checked with the
agent's own `_validate` before it replaces the old one, so the UI cannot write a
config that would halt the next run. A rejected save leaves the file exactly as
it was, and the validator's full list of problems is passed through to the
screen — it reports every problem at once, and truncating that list just means a
second failed save.

### Placeholders

The shipped `example_tenant.yaml` has **template values** in the identity
fields, not anybody's real name:

```yaml
from_name: "[your name - set in Settings]"
from_email: set-your-sending-address@example.invalid
```

`workspace._is_placeholder` recognises bracketed values and reserved
`.invalid` / `.example` domains, and `GET /api/profile` reports them as empty
strings. That is what makes a fresh install show a genuinely blank profile with
its prompts, instead of template text the user never typed — or worse, a
fabricated sender name that could end up at the bottom of real mail.

The fields cannot simply be blank: the agent's validator requires them, because
a batch that runs with no sender identity produces real email to real strangers
with nothing to identify who sent it. So clearing a field in the UI writes the
placeholder back rather than emptying it.

---

## Providers

`providers.py` is the single source of truth for provider metadata — what each
one is for in the user's terms, what fields it needs, its free-tier allowance,
and where to get a key. The frontend renders whatever this declares, so there
is no TypeScript copy to drift.

Every provider on this screen is BUILT. There is no "coming soon" entry and
no `enabled=False` spec: if a user can pick it from a dropdown, there is an
adapter behind it, a credential check in front of it, and a real call at the
end of it. Listing something a user cannot actually use is a way of being wrong
in the one place they are trying to decide what to buy.

Most have a free tier and a fresh install runs entirely on those. The rest —
ChatGPT, Claude, DeepSeek, an SMTP mailbox of your own, Anymail Finder, HubSpot
and Pipedrive — are metered by the provider and are chosen rather than fallen
back to.

Each test call is the cheapest endpoint the provider offers — usually an
account or model listing — because checking a key should not consume the
allowance it is checking. Anymail Finder is the sharpest case: its search
endpoints bill per verified address, so the check reads the account instead,
and pressing Test costs nothing.

The failure messages are the part worth reading. `401` tells a user nothing;
"that key was rejected, check for a stray space" tells them what to do. Two
cases were worth special handling:

- **Gmail** rejects a normal Google password, which is the single most common
  mistake. The message says "16-character app password, two-step verification
  must be on" rather than relaying Google's own wording.
- **Google Sheets** credentials can be perfectly valid and still fail on the
  thing everybody forgets — sharing the sheet with the service account. So the
  check opens the actual spreadsheet, and the failure names the address to
  share it with.
- **HubSpot** tokens are usually valid and under-scoped rather than wrong, so
  the check reads a page of contacts and the failure names the two scopes the
  private app needs.
- **Pipedrive** tokens only work against their own company, and the wrong
  subdomain returns a 404 that reads like a bad token. The failure names the
  address as well as the token.

Adding a provider means one entry in `PROVIDERS` and one check function. The UI
follows on its own.
