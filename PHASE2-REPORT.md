# BasketPilot — Phase 2 + 3 report

Everything here is from a run that happened. Where something did not work, that is
stated rather than omitted, and where a number comes from a fixture rather than a live
site, it says so.

---

## 1. Which retailers genuinely work through TinyFish

**Blinkit works through the TinyFish Browser API. Nothing else does yet.**

| Retailer | Through TinyFish | Status | Evidence |
|---|---|---|---|
| **Blinkit** | **Yes — Browser API** | **VERIFIED** | `runs/verification.json`: 24 products, engine `tinyfish_browser`. Later runs: 35 products across 3 queries (`runs/compare_e2e4.log`). |
| Zepto | No | Documented blocker | TinyFish Fetch returns `bot_blocked` on the search page. Not yet attempted through the Browser API. |
| JioMart | No | Documented blocker | Fetch reaches the page but returns an app shell, not products. |
| Swiggy Instamart | No | Documented blocker | Fetch returns `bot_blocked`. |
| BigBasket | No | Documented blocker | Fetch returns navigation chrome only; products are client-side rendered. |
| Flipkart Grocery | No | Documented blocker | Fetch returns an empty shell for the grocery search page. |

The distinction that matters: **the Agent API is not the Browser API.** Phase 1 concluded
TinyFish could not reach Blinkit, and that conclusion was correct *for the Agent API* —
which drives a browser on TinyFish's side and reports back in prose, and is
Cloudflare-blocked here. The Browser API returns a `cdp_url` and hands the browser to us
as plain Playwright. Blinkit's Cloudflare wall passes behind it, and the Phase 1
extraction ladder then runs unchanged.

Two documented facts worth keeping:

* `proxy_config.country_code: "IN"` is **accepted** by the Browser API and **rejected
  with HTTP 400** by the Agent API. The docs say any ISO 3166-1 alpha-2 works; that is
  true for one surface and not the other.
* The proxy egress is a real Indian residential IP — Mumbai, Maharashtra,
  AS55836 Reliance Jio Infocomm. That is why the catalogue is the Indian one.

The other five were triaged through the **Fetch API**, which is free (1,000 URLs/day) and
has no proxy or stealth knob. It is a useful reachability instrument and a useless
product source: it returns SPA shells. They are marked as blocked on *that* evidence, not
on an assumption — and the honest reading is that Fetch failing does not prove the
Browser API would fail. **The smallest next step for retailer coverage is to point the
existing Browser API engine at Zepto**, which is one registry entry and one live run.

---

## 2. Which profile and Vault workflows were actually verified

**Profiles: the API is enabled and verified working. A profile has not yet been warmed,
because warming one requires a human to log in — which is the next action for you.**

What was verified:

| Step | Result |
|---|---|
| `GET /v1/profiles` | Works. Returns one profile, `prof_702e93a049954b38` ("Default"), `domain_count: 0`. |
| `POST /v1/profiles` | Works. Created `prof_49f953ae3ed84aba` named `blinkit-560001`. |
| `POST /v1/profiles/{id}/setup-session` | Implemented in `tools/setup_profile.py`, returns the `cdp_url` to hand to a human. |
| `POST /v1/profiles/{id}/save` | Implemented; needs a session id from the step above. |
| `--profile-id` threading | Done: CLI → agent → adapters → `TinyFishBrowser`, recorded on the run log so a warm run can never be read as cold. |

`domain_count: 0` is the honest state of the account: **no profile has been warmed yet.**
Creating one is not warming one.

**Vault: documented, not built, and not needed.** Vault stores credentials and matches
them by domain via `use_vault` + `credential_item_ids`, and requires connecting 1Password
or Bitwarden. No target retailer in this phase requires a login — Blinkit's gate is a
delivery *pincode*, not an account — so there is nothing to put in it. Building a Vault
integration here would be infrastructure with no user.

**The one manual step, and the security boundary.** `tools/setup_profile.py connect`
prints a `cdp_url` and opens a browser on the TinyFish session. **You** log in there. The
tool never asks for, sees, or logs a password — the credential never passes through this
codebase or this chat.

```bash
python tools/setup_profile.py connect --profile-id prof_49f953ae3ed84aba
#   log in to Blinkit in the window that opens, set pincode 560001, press Enter
python tools/setup_profile.py save --profile-id prof_49f953ae3ed84aba --session-id <sid>
```

Whether this resolves the intermittent search block is **not yet known** — it is a
hypothesis with a mechanism behind it (a session that has already cleared the challenge
does not have to clear it again), and it will be answered by one live run after you warm
the profile. If it does not resolve it, the documented position is the one below.

---

## 3. The exact command

```bash
python -m basketpilot compare \
  --list "2L Amul milk, a dozen eggs, brown bread" \
  --pincode 560001 --retailer blinkit --engine tinyfish_browser
```

Add `--profile-id <id>` to reuse a warmed profile; omit it to run cold. Add `--json-only`
for machine output, `--output runs/x.json` to write the run log.

Phase 1 is unchanged and still works:

```bash
python -m basketpilot "milk, bread, eggs" --retailer blinkit --pincode 560001 --engine cloak
```

---

## 4. Test results, real-browser and optimizer separately

### Offline suite — logic, no network

```
pytest -m "not browser"      →  79 passed in 0.18s
tools/verify.py              →  29 passed, 0 failed  (Phase 1 invariants, unregressed)
```

These run with no API key, no network and no browser. They cover the optimizer (coverage
ranks above price, split penalty, the verification gate, brand and pack-size constraints,
missing ≠ zero), list parsing, the offer schema, and the Phase 1 invariants.

### Live-browser suite — real runs, real retailers

```
pytest -m browser
```

Results are appended below from `runs/browser_live.log`. Live tests are excluded by
default via `addopts = "-m 'not browser'"`, so they cannot run by accident. They assert
*transport*, not prices: a test pinned to "Amul Taaza 500 ml costs ₹28" would fail for
reasons unrelated to whether the pipeline works.

### The end-to-end run

```
python -m basketpilot compare --list "2L Amul milk, a dozen eggs, brown bread" \
  --pincode 560001 --retailer blinkit --engine tinyfish_browser
→ exit 0
→ Blinkit: 35 products via tinyfish_browser (milk 11, eggs 12, bread 12)
→ recommendation: blinkit · ₹188 · 1 platform · 2/3 items · estimated
→ milk held for approval: only a 250 ml carton available against a 2 L request
```

---

## 5. Two bugs the logs found

Both were diagnosed from real output, not from reasoning about the code.

**A false-positive challenge detector.** The detector treated `challenge-platform` in the
page HTML as evidence of a block. That string is Cloudflare's Turnstile *script tag*,
served on healthy Blinkit pages too. The log proved it: the marker was reported present,
the next search on the same page returned 12 products, and the marker never cleared — so
the run burned 45 s per query waiting for a challenge that did not exist.

**A client-side route that never fired the search XHR.** Blinkit's search is a
client-side route. Navigating to a search URL the SPA already believes it is on does not
re-issue the request, so the run waited 35 s for a request that was never made. That is
why the *first* query of a run came back empty while later ones succeeded. Retries now
re-warm on the retailer's home page first, making the next search a real route change.

After both fixes: **all three queries returned products, with no challenge waits at all.**

---

## 6. Remaining blockers, and the smallest next step

| Blocker | Honest state |
|---|---|
| Blinkit search is intermittently challenged | Mitigated, not solved. The re-warm fix made the last run clean (35/35 products, no waits). A warmed profile is the hypothesis for making it reliable; unproven until you log in. |
| Five retailers unreached | Blocked on **Fetch** evidence only. Untested through the Browser API, which is the surface that worked for Blinkit. |
| The LLM tier is unexercised | `FIREWORKS_API_KEY` is not set, so the deterministic tier runs — which is the intended default. The integration is written and activates on key presence; no `ANTHROPIC_API_KEY` is required anywhere. |
| Fees are estimates | The brief scoped fees to the search page. Delivery, handling and small-cart charges are unverified, so every total is labelled `estimated` and the unknowns are named. |
| Ordering | Deliberately absent. No cart, no order, no payment. |

**Smallest next step for TrueForge:** the pipeline is already a library, not a CLI —
`ShoppingAgent.run()` returns a plain dict, and `compare.py` is a thin renderer over it.
The smallest integration is to call `run_compare()` and render its output; nothing needs
to be refactored to make that possible. Before wiring a UI, warm the profile and point the
Browser API at Zepto — those two are the difference between a one-retailer demo and a real
comparison.
