# BasketPilot — Phase 1: TinyFish Browser Proof of Concept

Search an Indian quick-commerce retailer for a shopping list and get back **validated,
structured product data**: name, brand, package size, displayed price, availability and
product URL.

Phase 1 proves one real browser integration against one real retailer (Blinkit). It is
deliberately not a product — there is no frontend, no database, no ordering, and no
second retailer adapter.

## The exact command

```bash
python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001 --engine auto
```

## Setup

```bash
uv venv --python 3.12
uv pip install -e .          # or: uv pip install httpx python-dotenv cloakbrowser

cp .env.example .env         # then put your real key in .env
```

Get a TinyFish API key at <https://agent.tinyfish.ai/api-keys>. New accounts include $8
of free credit, which is far more than this POC needs. The key looks like
`sk-tinyfish-...`.

`.env` is gitignored. Never commit a real key.

### The one-liner that needs no API key

The local stealth browser needs no key at all:

```bash
python -m basketpilot "milk" --engine cloak --headed
```

First run downloads a stealth Chromium build (~200 MB, cached in `~/.cloakbrowser`).

## Why there are two engines

Blinkit sits behind a **Cloudflare managed challenge** — a plain request to
`https://blinkit.com/` returns HTTP 403 with `cf-mitigated: challenge`. On top of that,
quick-commerce sites hide their entire catalogue until a delivery location is set, so
even a browser that clears the challenge sees an empty page until a pincode is chosen.

| Engine | What it is | Network position |
|---|---|---|
| **A — TinyFish** | The mandated Phase 1 integration. Remote agent browser via the official Agent API. | Datacentre IP |
| **B — CloakBrowser** | Local stealth Chromium, Playwright-compatible. | **Your residential IP** |

Engine B exists because a datacentre IP is exactly the profile Cloudflare challenges
hardest, and because TinyFish's documented `proxy_config.country_code` enum is
`US, GB, CA, DE, FR, JP, AU` — **India is not in it**. That is now confirmed rather
than suspected: requesting `country_code: "IN"` returns **HTTP 400**, and the client
automatically retries without a proxy (see *Platform behaviours* below).

## Platform behaviours found by testing

These were discovered by running against the live API and the live site, not read off
the docs. Each one is handled in code; they are recorded here because each cost real
time to find.

**TinyFish Agent API**

| Behaviour | Effect | How it is handled |
|---|---|---|
| `proxy_config.country_code: "IN"` | **HTTP 400** — India is not in the supported enum | Proxy is dropped and the request retried once, without one |
| `agent_config` (`max_steps`, `max_duration_seconds`) | **HTTP 403 FORBIDDEN**: *"Your account is not enabled for custom max steps yet. Join the beta program at /beta"* | `agent_config` is dropped and the request retried on platform defaults; the client remembers, so later runs skip the round trip |
| `COMPLETED` status | Means the *infrastructure* worked — **not** that the goal succeeded. A run can complete with zero products | Treated as a failure for fallback purposes unless at least one usable product came back |

The two refusals interact: dropping the proxy can surface the beta gate, so `start_run`
walks a small ladder (drop proxy → drop `agent_config` → drop both) rather than doing
one-shot nested retries, which is what allowed a 403 to escape an earlier version.

**Blinkit**

| Behaviour | Effect | How it is handled |
|---|---|---|
| Root URL | HTTP 403 + `cf-mitigated: challenge` to plain HTTP; a real stealth browser gets HTTP 200 | Challenge detection reads the `cf-mitigated` header, the URL and the HTML — a challenge page is full HTML and a naive parser reads it as "no products" |
| Location modal | **Auto-opens on landing.** Its overlay swallows clicks, so clicking "Select Location" always times out | We never click it: we wait for the modal's own input (`input[placeholder='search delivery location']`) and type into that |
| Location persists | On a later run the modal **never appears** | The location step treats "no modal" as success, not failure. Waiting for a modal that will not come is a guaranteed 30s timeout |
| Home page search bar | Is a div that opens an overlay — the home page has **no search `<input>` at all** | We navigate to the search route (`/s/?q=<query>`) instead of driving the box |
| Product cards | Are divs with click handlers — there are **no `<a href>` product links** | Extraction reads Blinkit's own JSON, not anchors |
| Search API | `POST /v1/layout/search?q=<query>` returns `{is_success, response: {snippets: [...]}}`; products are `widget_type == "product_card_snippet_type_2"` | Read passively off the wire; we do **not** replay the call, so the rotating `auth_key`/`session_uuid` headers are never our problem |

`--engine auto` (the default) runs TinyFish first and escalates to the local browser
only when the failure is one another engine could plausibly survive. It never escalates
on a billing or auth error, because that would hide a broken key behind a rescue that
appears to work.

```
--engine auto      TinyFish, then local stealth browser on block   (default)
--engine tinyfish  TinyFish only
--engine cloak     local stealth browser only, no API key needed
```

## Output

The CLI prints JSON on stdout and a human-readable summary on stderr, and writes a full
run log to `runs/<timestamp>-<retailer>-<engine>.json` containing the request payload
(key redacted), terminal status, error code, step count, the raw engine response and the
normalised result. On failure the log is where you diagnose from.

```json
{
  "retailer": "Blinkit",
  "engine": "tinyfish",
  "status": "COMPLETED",
  "products": [
    {
      "query": "milk",
      "name": "Amul Taaza Toned Milk",
      "brand": "Amul",
      "package_size": "500 ml",
      "price_displayed": "₹28",
      "price_numeric": 28.0,
      "availability": "in_stock",
      "product_url": "https://blinkit.com/prid/...",
      "evidence": "Amul Taaza Toned Milk 500 ml ₹28",
      "source": "tinyfish",
      "warnings": []
    }
  ]
}
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Products were extracted and validated |
| 1 | The run failed or produced nothing usable — a diagnosis is printed |
| 2 | Configuration problem, e.g. a missing API key or unknown retailer |

## How fabrication is prevented

The brief's hardest constraint is that no price may be invented. That is enforced in
code, not just requested in the prompt:

1. **The displayed string is the source of truth.** `price_numeric` is parsed in Python
   from the displayed price text. An agent-supplied number is never trusted.
2. **A number with no readable price behind it is dropped.** If there is no
   currency-marked price in `price_displayed`, `price_numeric` is discarded and a
   warning is recorded.
3. **Evidence must corroborate the price.** The schema requires an `evidence` field —
   verbatim on-screen text the agent read the price from. If the digits of the price do
   not appear in that evidence, the price is dropped. This turns "never invent a price"
   from a promise into a check.
4. **`minItems: 0` in the schema.** Setting a minimum above zero would pressure the
   agent to invent a product to satisfy validation. Empty is explicitly allowed, and an
   empty result is treated as a failure.
5. **A name alone is not a result.** A row with no price and no explicit out-of-stock
   marker usually means a skeleton page, not a search result, so it does not count
   toward coverage.
6. **`COMPLETED` is not success.** TinyFish reports `COMPLETED` when the *infrastructure*
   worked, which is not the same as the goal succeeding. A completed run with no usable
   products is a failure and triggers the fallback.

Every product carries `source` (`tinyfish` | `cloak_json` | `cloak_dom`) and its
extraction warnings, so each figure can be traced to where it came from.

## Diagnostics

```bash
python -m basketpilot "milk" --engine cloak --headed   # watch the browser work
python tools/smoke_cloak.py                            # is the site blocking us at all?
```

`tools/smoke_cloak.py` is standalone: it launches the stealth browser, navigates to
Blinkit, reports the HTTP status, the `cf-mitigated` header and the page title, writes a
screenshot to `runs/`, and tells you plainly whether the challenge was cleared. Run it
first whenever something is not working.

For Engine A, the run log carries `request_id` (TinyFish asks for it in support requests)
and the raw error body with its `code` and `category`.

## Known limitations

- **Blinkit may block both engines.** A Cloudflare managed challenge plus a datacentre IP
  is a bad combination, and the stealth browser does not solve CAPTCHAs — it only aims to
  prevent them appearing. If both engines fail, the tool says so and exits non-zero. It
  does not fake a pass.
- **Blinkit is a React SPA with generated CSS class names**, so the DOM-extraction rung is
  brittle by design. That is why the captured-JSON rung is tried first.
- **The location gate must be cleared** before any catalogue is visible. If the agent
  reports `location_set: false`, the result is treated as failed rather than reported as
  "no products found".
- **CloakBrowser's free tier allows one concurrent session.** Do not run two `--engine
  cloak` invocations at once.
- **Zepto is configured but untested.** `--retailer zepto` exists so the documented
  fallback is one flag away, not a rewrite. Phase 1 deliberately proves one retailer.

## Terms of service

Automated access to these retailer sites is contrary to their terms of service, and the
rescue path is designed to evade bot detection. That is a deliberate business decision to
make consciously before this goes beyond a POC — flagged here rather than discovered later.

## Project layout

```
basketpilot/
├── config.py           # .env loading, fail-loud key validation
├── models.py           # Product/RetailerResult, the anti-fabrication gate
├── tinyfish_client.py  # Engine A — TinyFish Agent API
├── cloak_client.py     # Engine B — local stealth browser
├── normalize.py        # retailer registry, engine orchestration, fallback
└── cli.py              # argparse, run logging, exit codes
tools/smoke_cloak.py    # standalone "is the site blocking us?" probe
runs/                   # timestamped run logs (gitignored)
```
