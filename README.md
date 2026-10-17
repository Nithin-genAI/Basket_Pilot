# BasketPilot — Phase 1: TinyFish Browser Proof of Concept

Search an Indian quick-commerce retailer for a shopping list and get back **validated,
structured product data**: name, brand, package size, displayed price, availability and
product URL.

Phase 1 proves one real browser integration against one real retailer (Blinkit). It is
deliberately not a product — there is no frontend, no database, no ordering, and no
second retailer adapter.

---

# Phase 2 + 3: parent agent, TinyFish Browser, basket optimization

Phase 2 takes a whole shopping list, searches real retailers, and works out the best
*practical* way to buy it. Everything below is additive — the Phase 1 command is
unchanged and still returns its 60 products.

## The exact command

```bash
python -m basketpilot compare \
  --list "2L Amul milk, a dozen eggs, brown bread" \
  --pincode 560001 --retailer blinkit --engine tinyfish_browser
```

Real output from `runs/compare_e2e4.log`:

```
── retailers ──
  OK    Blinkit             35 products via tinyfish_browser

── recommendation ──
  blinkit  ·  ₹188  ·  1 platform(s)  ·  2/3 items  ·  estimated
    [Blinkit] Nutri Hatch White Eggs · 12 pcs — ₹107
    [Blinkit] The Health Factory Zero Maida Whole Wheat Bread · 250 g — ₹55
    [??] Amul Lactose Free Milk · 250 ml — ₹26
         not counted as covered: only a different size is available: pack is 250 ml
         but the request was 2000ml. Approval needed before this is bought.
    · covers 2/3 items; 1 held for approval (only a different size available): milk
    · total is an estimate: blinkit: delivery fee not verified; ...

No order was placed and no payment was made — this is a recommendation only.
```

Note the `[??]` line. The list asked for **2 L** of Amul milk; Blinkit returned a 250 ml
carton. That is a real product at a real price, and it is *not* what was asked for — so
it is shown, priced, and held for approval, and it does not count toward coverage. An
earlier version of this pipeline silently bought it.

## What Phase 2 found, by testing rather than assuming

| Finding | Evidence |
|---|---|
| **TinyFish's Browser API reaches Blinkit; the Agent API does not.** | `POST api.browser.tinyfish.ai` → 201 with a `cdp_url`; the Agent API is Cloudflare-blocked. |
| **The two TinyFish surfaces disagree about country codes.** | `proxy_config.country_code: "IN"` is accepted by the Browser API and rejected with HTTP 400 by the Agent API. The docs' "any ISO 3166-1 alpha-2" claim holds for one and not the other. |
| **The proxy is a genuine Indian residential IP.** | Egress reported Mumbai, Maharashtra, AS55836 Reliance Jio Infocomm. |
| **The Fetch API is useless for products here.** | It returns SPA shells — BigBasket navigation chrome, Blinkit footer text. Free triage, no catalogue. |
| **Blinkit challenges the *search* navigation, not the landing page.** | The landing page returns its real title; the first search of a run intermittently returns no XHR at all. |
| **A warmed Browser Context Profile is available and the API is enabled.** | `GET /v1/profiles` returns a profile; `domain_count: 0` means none has been warmed yet. |

### Two bugs the run log found, and what they actually were

Both were diagnosed from a real log, not from reasoning:

1. **A false-positive challenge detector.** The detector treated the string
   `challenge-platform` in the page HTML as evidence of a block. That string is
   Cloudflare's Turnstile *script tag*, which Blinkit serves on healthy pages too. The
   log proved it: the marker was reported present, the very next search on the same page
   returned 12 products, and the marker never cleared. It was burning 45 s per query
   waiting for a challenge that did not exist.

2. **A client-side route that never fired the search XHR.** Blinkit's search is a
   client-side route, so navigating to a search URL the SPA already believes it is on
   does not re-issue the request — the run then waits 35 s for a request that was never
   made. That is why the *first* query of a run came back empty while later ones
   succeeded. Retries now re-warm on the retailer's home page first, which makes the next
   search a real route change. After this fix, all three queries returned products
   (milk 11, eggs 12, bread 12) with no challenge waits at all.

## The warmed profile — one manual step

A cold browser clears Blinkit's challenge only sometimes. A profile that has already
passed it starts from a state that does not need to. The setup is three documented calls
and **one human step in a browser** — this tool never sees, asks for, or logs a password:

```bash
python tools/setup_profile.py create --name blinkit-560001     # done: prof_49f953ae3ed84aba
python tools/setup_profile.py connect --profile-id prof_49f953ae3ed84aba
#   → prints a cdp_url and opens a browser on the TinyFish session.
#     Log in to Blinkit there and set the delivery pincode to 560001.
python tools/setup_profile.py save --profile-id prof_49f953ae3ed84aba --session-id <sid>
```

Then reuse it:

```bash
python -m basketpilot compare --list "..." --pincode 560001 \
  --retailer blinkit --engine tinyfish_browser --profile-id prof_49f953ae3ed84aba
```

The run log records whether a profile was used, so a warm result can never be read as
cold or the reverse. Without `--profile-id` the pipeline runs cold, which is the default.

## Tests

```bash
pytest -m "not browser"    # 79 tests, ~0.2s, no network, no key, no browser
pytest -m browser          # real runs against real retailers; costs credit
```

The offline suite is the default. Live tests are marked and excluded by
`addopts = "-m 'not browser'"` in `pyproject.toml`, so they cannot run by accident.

## The safety boundary

No order is submitted, no payment is made, no item is added to a cart. The workflow ends
at a ranked recommendation and the evidence behind it. It is built so a later phase can
insert an explicit approval gate before any purchase action.

## The exact command

```bash
python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001 --engine auto
```

**Read this before you run it.** `auto` gives Engine A a fair shot first, and Engine A
currently hangs on these retailers — so the command above spends up to `--max-duration`
seconds (480 by default) waiting on TinyFish before escalating to the local browser that
actually returns data. That is the architecturally correct order and the brief's mandated
one, but it is slow, and the wait is dead time rather than work.

For the same data in about three minutes, skip straight to the engine that works:

```bash
python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001 --engine cloak
```

Both are real runs against live Blinkit. The `auto` path additionally records the Engine A
failure inside the run log under `primary_engine_failure`, so nothing is hidden by taking
the fast path.

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
| Location persists | On a later run the modal **never appears** | The location step treats "no modal" as success, not failure. Waiting for a modal that will not come is a guaranteed timeout — the probe uses a short 8s window for exactly this reason |
| Home page search bar | Is a div that opens an overlay — the home page has **no search `<input>` at all** | We navigate to the search route (`/s/?q=<query>`) instead of driving the box |
| Product cards | Are divs with click handlers — there are **no `<a href>` product links** | Extraction reads Blinkit's own JSON, not anchors |
| Product ids in the DOM | There are **none** — no `data-product-id`, no `data-prid`, no `/prid/` anywhere in the rendered grid (verified: `tools/probe_dom_id.py`) | The DOM rung therefore cannot produce a `product_url`. Those rows say so explicitly instead of leaving the field silently null |
| Search API | `POST /v1/layout/search?q=<query>` returns `{is_success, response: {snippets: [...]}}`; products are `widget_type == "product_card_snippet_type_2"` | Read passively off the wire; we do **not** replay the call, so the rotating `auth_key`/`session_uuid` headers are never our problem |
| Search XHR body | Is **sometimes lost to a race** — Blinkit's SPA navigates again immediately after the response, and a body not read before that navigation is gone: *"Response body is not available for a response that was navigated away from"* | The JSON rung is attempted **twice** before the DOM rung is used, because JSON is the only rung that yields product URLs. One unlucky capture is not evidence the API is down |
| Product URL shape | `/prn/<slug>/prid/<id>`; the slug is cosmetic, the id is the key | Synthesised from the id + name, then **verified against the live site**: 3/3 constructed URLs returned HTTP 200 with the product name and an ADD button (`tools/probe_url.py`) |

**Playwright (Engine B)**

| Behaviour | Effect | How it is handled |
|---|---|---|
| Response bodies | Are readable only until the page navigates again: *"Response body is not available for a response that was navigated away from"* | The network recorder reads each body **inside** its response handler, the instant it arrives. A deferred read loses them — measured: deferring cost eggs its `product_url` on every row |
| Event emitters | A response is emitted to the **context first, then the page**, as two sequential emits — not one nested pair | The recorder attaches to the **context**. Reading a body is a blocking Playwright call that re-enters the sync API, so a *page* handler runs it inside `page.emit(...)` — the same dispatch that drives `expect_response`'s waiter. The waiter then tears itself down mid-iteration and pyee's naked `list.pop` raises `KeyError: <function Waiter.reject_on_event.<locals>.listener ...>`. Moving to the context keeps the read out of that dispatch while still seeing every response |
| `time.sleep` vs `wait_for_timeout` | Sync-API response events are dispatched **only while the main thread is inside a Playwright call**, so a `sleep`-and-poll loop never pumps the loop and every capture looks missing | Waiting is done with `page.expect_response(...)`, which both pumps the event loop and waits for the specific response |

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

One product, verbatim from a real run (the `warnings` array, which carries the product id,
stock count and source URL, is elided here for width):

```json
{
  "retailer": "Blinkit",
  "engine": "cloak",
  "status": "COMPLETED",
  "products": [
    {
      "query": "milk",
      "name": "Amul Taaza Toned Milk",
      "availability": "in_stock",
      "brand": "Amul",
      "package_size": "500 ml",
      "price_displayed": "₹30",
      "price_numeric": 30.0,
      "currency": "INR",
      "product_url": "https://blinkit.com/prn/amul-taaza-toned-milk/prid/19512",
      "evidence": "Amul Taaza Toned Milk | Amul | 500 ml | ₹30",
      "source": "cloak_json"
    }
  ]
}
```

`availability` is derived from Blinkit's own integer `inventory` count, not inferred from
the presence of a button: `> 0` is `in_stock`, `0` is `out_of_stock`.

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
   verbatim on-screen text the price was read from. If the digits of the price do not
   appear in that evidence, the price is dropped. This turns "never invent a price" from
   a promise into a check. For the **TinyFish** rung this is a genuine independent check,
   because the agent supplies `evidence` separately from the price. For the **JSON** rung
   the extractor builds `evidence` from the same card it read the price from, so it cannot
   catch a misread on its own — the independent check there is the payload price
   comparison below.
4. **The displayed price is cross-checked against the payload's own price.** Blinkit
   attaches a numeric price to each card's add-to-cart action. If that disagrees with the
   displayed string, the mismatch is recorded as a warning and the *displayed* value is
   kept — the string a human reads on the card is the one that has to be right.
5. **`minItems: 0` in the schema.** Setting a minimum above zero would pressure the
   agent to invent a product to satisfy validation. Empty is explicitly allowed, and an
   empty result is treated as a failure.
6. **A name alone is not a result.** A row with no price and no explicit out-of-stock
   marker usually means a skeleton page, not a search result, so it does not count
   toward coverage.
7. **`COMPLETED` is not success.** TinyFish reports `COMPLETED` when the *infrastructure*
   worked, which is not the same as the goal succeeding. A completed run with no usable
   products is a failure and triggers the fallback.

Every product carries `source` (`tinyfish` | `cloak_json` | `cloak_dom`) and its
extraction warnings, so each figure can be traced to where it came from.

The gate is exercised by `python tools/verify.py`, which covers the cases that matter — a
fabricated numeric with no readable price behind it, a numeric derived from the display,
evidence that does not corroborate, evidence that does, `"500 ml"` not being read as a
price, `"₹1,299"` parsing to `1299.0`, provenance warnings surviving validation, and a
name-only row not counting as usable.

## Diagnostics

```bash
python tools/verify.py                                 # all invariants, offline, no key
python tools/selftest_tinyfish.py                      # is Engine A working at all?
python -m basketpilot "milk" --engine cloak --headed   # watch the browser work
python tools/smoke_cloak.py                            # is the site blocking us at all?
python tools/probe_url.py                              # do constructed product URLs resolve?
python tools/probe_dom_id.py                           # does the DOM expose product ids?
python tools/diagnose_tinyfish_retailer.py --url https://www.jiomart.com/ --label JioMart
```

`tools/verify.py` needs no network and no API key. It runs 29 checks over the invariants
this project claims — the anti-fabrication gate, what counts as a usable result, query
attribution, the unconfirmed-location rule, fallback diagnostics, dedupe, and which
emitter the network recorder listens on — and exits non-zero if any of them regresses. It
is the fast check to run after touching extraction or validation.

`tools/smoke_cloak.py` is standalone: it launches the stealth browser, navigates to
Blinkit, reports the HTTP status, the `cf-mitigated` header and the page title, writes a
screenshot to `runs/`, and tells you plainly whether the challenge was cleared. Run it
first whenever something is not working.

`tools/diagnose_tinyfish_retailer.py` is a **diagnostic, not a retailer adapter** — it
points Engine A at an arbitrary retailer to separate "TinyFish cannot do Indian retail"
from "this one retailer is too hard". It adds no retailer to the product and its output is
not part of the CLI's normal path.

For Engine A, the run log carries `request_id` (TinyFish asks for it in support requests)
and the raw error body with its `code` and `category`.

## Known limitations

- **Blinkit may block both engines.** A Cloudflare managed challenge plus a datacentre IP
  is a bad combination, and the stealth browser does not solve CAPTCHAs — it only aims to
  prevent them appearing. If both engines fail, the tool says so and exits non-zero. It
  does not fake a pass.
- **Blinkit is a React SPA with generated CSS class names**, so the DOM-extraction rung is
  brittle by design. That is why the captured-JSON rung is tried first, and twice.
- **Rows from the DOM rung carry no `product_url`.** This is structural, not a bug: the
  search page exposes no product ids in any attribute or href (`tools/probe_dom_id.py`
  found none). Before the JSON rung was retried, this showed up as a run with URLs on only
  24 of 48 rows; after the retry it does not appear at all in a healthy run, because the
  JSON rung is now given a second chance instead of being abandoned. The rung still exists
  as a last resort, and when it is used every affected row says in its warnings why the URL
  is missing.
- **The location gate must be cleared** before any catalogue is visible. Both engines
  treat an unconfirmed location as a failure, not a warning. This matters more than it
  sounds: Blinkit's search API answers happily for an unserved pincode and returns a dozen
  real products — for some *other* location. Prices that are real but attached to the wrong
  place are exactly the silent substitution this project is not allowed to make, so a run
  whose location step failed returns `LOCATION_NOT_SET`, exits non-zero, and marks every
  row `location unverified` while keeping them for inspection.
- **The `out_of_stock` branch is unexercised against live data.** `availability` is derived
  from Blinkit's own integer `inventory` field, which is sound, but every row returned in
  testing had stock, so the `inventory == 0` path has only been tested synthetically.
- **CloakBrowser's free tier allows one concurrent session.** Do not run two `--engine
  cloak` invocations at once.
- **Zepto is configured but untested.** `--retailer zepto` exists so the documented
  fallback is one flag away, not a rewrite. Phase 1 deliberately proves one retailer.

## What happened with Engine A (TinyFish) — the honest result

Engine A is the mandated Phase 1 integration, and it **works**. The same key, the same
client and the same schema complete a real browser task against a neutral site — verified
repeatedly, most recently run `9b3947d0-82ff-4666-97eb-78beae9eeb02`:
`COMPLETED`, `num_of_steps: 2`, products with real prices returned
(`python tools/selftest_tinyfish.py`).

Against the Phase 1 target retailers it did not return data on any attempt:

| Target | TinyFish outcome |
|---|---|
| Blinkit, run 1 | Stayed `RUNNING` for ~22 minutes with `num_of_steps: None`, then cancelled (`POST /v1/runs/{id}/cancel` → HTTP 200 → `CANCELLED`) |
| Blinkit, run 2 | Stayed `RUNNING`, never terminal, `num_of_steps: None` |
| JioMart — the control (`tools/diagnose_tinyfish_retailer.py`) | Stayed `RUNNING` past a 420s deadline, `num_of_steps: None`, ended `TIMEOUT` |
| Blinkit, run 3 (inside `--engine auto`) | **`COMPLETED`** in ~2 minutes — and reported `location_set: false`, i.e. it reached the site but could not clear the delivery-location gate. Surfaced as `LOCATION_NOT_SET`, which triggered the fallback |
| Blinkit, run 4 | **`COMPLETED`** in 19 steps — and named the cause itself (below) |

So there are two distinct behaviours, and the honest summary is that Engine A is
**intermittent** rather than uniformly broken:

1. **Most runs hang.** Accepted, never steps, never terminal. `num_of_steps: None`
   throughout is the telling detail — the agent never got as far as taking a step, which
   points at reachability or scheduling for this class of site rather than at the agent
   struggling with a hard page.
2. **Sometimes it runs, reaches Blinkit, and gets blocked or cannot clear the location
   gate.** Runs 3 and 4 did exactly that and said so themselves.

Run 4 is the clearest evidence, because the agent diagnosed its own failure:

> *"Blinkit returned an 'access denied / sorry, you have been blocked' page. A browser
> reconfiguration with a US proxy was attempted but the block persisted. No location could
> be set to pincode 560001 and no product search could be performed. Task cannot be
> completed due to this blocking page."*

That is Cloudflare, from TinyFish's own browser, in the agent's own words — and it is the
mechanism the whole two-engine design was built around. Note the US proxy: `country_code:
"IN"` is not in TinyFish's documented enum and returns **HTTP 400** (observed on this run
too), so with the proxy dropped the egress is a datacentre IP from a supported region,
which is the profile Cloudflare challenges hardest. Engine B clears the same challenge
because it runs from a residential Indian IP.

The location-gate failure in run 3 matches what we found independently: Blinkit's location
modal auto-opens and its overlay swallows clicks, which is why Engine B has to type into
the modal's own input rather than click anything.

The JioMart run is what makes the hangs diagnosable rather than a guess. JioMart answers
plain HTTP 200 and sits behind no Cloudflare challenge, so if Blinkit's Cloudflare were the
cause, JioMart should have sailed through. It did not.

And the selftest passed *after* both hangs — same key, same client, same session. So this is
**not** a platform outage, **not** a bad key and **not** a broken integration: the plumbing
works, and the failures are specific to these Indian retail targets. Distinguishing
"cannot reach" from "never scheduled" needs TinyFish-side visibility we do not have, so we
do not claim which it is.

What we do claim, because it is what was observed: **Engine A cannot be relied on for these
retailers today, and Engine B produced every product in this repository.**

This is reported, not worked around. A poll past its deadline returns `status: TIMEOUT` with
`_last_observed_status` and a message saying the run may still be going server-side, rather
than hanging forever or reporting a silent empty success. The hangs cost 480s and 420s of
wall clock, which is why `--engine cloak` is the fast path. And when `auto` does fall back,
the primary engine's own diagnostics — its step count, its warnings and the agent's
structured output, including the note explaining the failure — are carried into the merged
run log rather than reduced to an error code.

## Platforms tested

Phase 1 proves **one** retailer end to end rather than seven shallowly. All seven target
platforms were probed with a plain HTTP request first, to rank the risk honestly before
committing to one:

| Platform | Plain HTTP to root | Verdict |
|---|---|---|
| **Blinkit** | **403**, `cf-mitigated: challenge` | **Chosen and proven.** Hardest target, so proving it proves the approach. Products returned. |
| Zepto | 202 via CloudFront | Configured as the documented fallback; not built in Phase 1 |
| Swiggy Instamart | 202 via CloudFront | Not attempted in Phase 1 |
| BigBasket | 403 | Not attempted in Phase 1 |
| JioMart | 200 | **Probed with Engine A as a control** — it hung too, while a neutral site completed in the same session, which is how we know the TinyFish failure is specific to these targets. No adapter built. |
| Amazon Fresh | 200 | Not attempted in Phase 1 |
| Flipkart Grocery | 404 | Not attempted in Phase 1 |

Exactly one retailer was actually integrated, as the brief required. Blinkit was tested with
**both** engines; JioMart was tested with Engine A only, as a diagnostic control, and no
adapter was written for it.

A 403 to plain HTTP is not the same as a block to a real browser — Blinkit returns 403 to
`curl` and HTTP 200 to the stealth browser. That distinction is the whole reason the
CloakBrowser engine exists, and it is why the ranking above is a starting point rather
than a conclusion.

**Authentication: none was required.** Blinkit's catalogue is browsable without an
account — it gates on *delivery location*, not on login. No TinyFish Browser Context
Profile or Vault credential was needed, and none is configured. Login persistence is
therefore **not claimed** — it was never exercised, and it is untested. A profile would
only become relevant if a later phase needs cart or order access.

## Status: what works and what failed

**Works**

- One real integration, against live Blinkit, returning validated structured data. A run
  against `milk, bread, eggs` at pincode 560001 returned **60 products**, every one of them
  with a name, brand, package size, displayed price, numeric price and product URL — the
  complete field list the brief asked for, at 100% coverage, verified programmatically
  rather than by eye.
- Prices and pack sizes are real, read off Blinkit's own search response, and traceable:
  every row carries the product id, the stock count and the source URL it came from, and
  60/60 traced back to a captured search response.
- Product URLs resolve — verified against the live site, 3/3 constructed URLs returned
  HTTP 200 showing the right product.
- Query attribution is clean: no cross-query leakage between milk, bread and eggs.
- The anti-fabrication gate holds under nine adversarial checks, including a fabricated
  numeric being dropped and `"500 ml"` not being read as a price.
- Failure paths are honest: a missing key exits 2 with an actionable message; a hung
  TinyFish run becomes an explicit `TIMEOUT` with the last observed status; an unserved
  pincode returns `LOCATION_NOT_SET` and exits non-zero instead of passing off another
  location's prices as an answer; an empty search is screenshotted and warned about rather
  than reported as success.
- `--json-only` is genuinely pipe-clean. stdout carries the JSON payload and nothing else:
  the CLI banner and both engines' progress logs go to stderr, so
  `python -m basketpilot ... --json-only | jq` works.

**Failed**

- **Engine A (TinyFish) cannot be relied on for Blinkit or JioMart.** Most runs stay
  `RUNNING` indefinitely with `num_of_steps: None` and never reach a terminal state. When
  it does run, Blinkit serves it Cloudflare's block page — the agent said so itself:
  *"Blinkit returned an 'access denied / sorry, you have been blocked' page... a browser
  reconfiguration with a US proxy was attempted but the block persisted."* Meanwhile the
  same client completes a real task against a neutral site in the same session, so the
  plumbing is fine and the failure is target-specific. See *What happened with Engine A*
  above. This is the most important negative result in Phase 1, because Engine A was the
  mandated integration.
- **Blinkit's search XHR body is intermittently lost** to a race with the SPA's own
  navigation. Handled by retrying the JSON rung once — which is what took URL coverage from
  50% to 100% — but the underlying race is a site behaviour we work around rather than fix.
- **The DOM fallback rung cannot produce product URLs**, because Blinkit's search page
  exposes no product ids anywhere. Structural, verified, and now recorded per-row.
- **No authentication was needed, and no profile was tested.** Blinkit gates on delivery
  location, not login, so TinyFish Browser Context Profiles and Vault were not exercised.
  Login persistence is therefore **not claimed** — it is untested.
- **Six of the seven target platforms were not integrated.** One was proven, as the brief
  required; the other six are documented as untested rather than implied to work.

## Next smallest step toward TrueForge

The seam already exists. `basketpilot.normalize.search()` returns one `RetailerResult`
regardless of which engine produced the data, so TrueForge has a single callable to target
rather than two code paths.

The smallest next step is to freeze that into a public entry point and **confront where the
browser runs** — which is the real blocker, not the plumbing:

```python
# basketpilot/api.py — the one seam TrueForge calls
def compare(queries: list[str], pincode: str) -> list[RetailerResult]: ...
```

Adding Zepto then becomes a config entry rather than a rewrite, which is the point.

But there is a dependency worth naming before anyone wires this up. Engine A does not work
for these retailers, so the working path is Engine B, and Engine B is a **local stealth
Chromium on a residential Indian IP** — which is precisely why it clears Cloudflare where a
datacentre IP does not. A TrueForge integration that runs server-side would move that
browser to a datacentre, and the most likely outcome is that Blinkit starts returning 403 to
it. So the next step is not "expose a function"; it is **decide where the browser lives** —
a local sidecar TrueForge calls over HTTP, or a residential-proxy egress — and only then
expose the seam. Wiring `compare()` into a server first would produce an integration that
passes its unit tests and returns nothing in production.

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
tools/
├── verify.py                      # offline invariant suite — run this after changes
├── selftest_tinyfish.py           # is Engine A working at all?
├── smoke_cloak.py                 # is the site blocking us at all?
├── probe_url.py                   # do constructed product URLs resolve?
├── probe_dom_id.py                # does the DOM expose product ids?
└── diagnose_tinyfish_retailer.py  # Engine A against any retailer (diagnostic only)
runs/                   # timestamped run logs (gitignored)
```
