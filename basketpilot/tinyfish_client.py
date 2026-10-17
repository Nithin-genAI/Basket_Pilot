"""Engine A — the TinyFish Agent API client.

Calls the documented REST endpoints directly rather than going through the
``tinyfish`` SDK. Every field used here is documented at the REST layer, so this
avoids guessing SDK method signatures, keeps the raw error body available for
diagnosis, and makes the polling loop explicit.

Endpoints (https://docs.tinyfish.ai/agent-api):
    POST /v1/automation/run-async   start a run, returns a run_id
    GET  /v1/runs/{id}              poll for status and result
"""

from __future__ import annotations

import sys
import time
from typing import Any

import httpx

from .config import RUN_ASYNC_PATH, RUNS_PATH, Settings, require_tinyfish_key
from .models import Product, RetailerResult

# Terminal run statuses returned by the polling endpoint.
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

# Error codes where retrying the *same* engine is pointless but another engine
# might succeed. These trigger the CloakBrowser rescue.
FALLBACK_CODES = {
    "SITE_BLOCKED",
    "TASK_FAILED",
    "MAX_STEPS_EXCEEDED",
    "TIMEOUT",
    "CANCELLED",
    "NO_PRODUCTS_EXTRACTED",
    "TRANSPORT_ERROR",
}

# Error codes caused by configuration, billing, or our own bad input. Falling
# through to another engine would mask a real problem, so we stop and say so.
FATAL_CODES = {
    "MISSING_API_KEY",
    "INVALID_API_KEY",
    "UNAUTHORIZED",
    "INVALID_INPUT",
    "INSUFFICIENT_CREDITS",
    "BILLING_REJECTED",
    "FREE_ALLOWANCE_EXHAUSTED",
    "DAILY_LIMIT_EXCEEDED",
    "FORBIDDEN",
    "CONTENT_POLICY_VIOLATION",
    "VAULT_RECONNECT_REQUIRED",
    "FEATURE_NOT_AVAILABLE",
}

# Transient platform problems — worth retrying against the same engine.
RETRYABLE_CODES = {
    "SERVICE_BUSY",
    "INTERNAL_ERROR",
    "BACKEND_UNAVAILABLE",
    "RETRY_REQUIRED",
    "RATE_LIMIT_EXCEEDED",
    "UNKNOWN",
}


# Keywords TinyFish accepts in output_schema. Anything outside this set is rejected
# with a 400 before the run starts — including `description` and `title`, which is
# the trap, since attaching a description to each property is the reflexive habit.
ALLOWED_SCHEMA_KEYWORDS = frozenset(
    {
        "anyOf",
        "enum",
        "format",
        "items",
        "maxItems",
        "maximum",
        "minItems",
        "minimum",
        "nullable",
        "properties",
        "propertyOrdering",
        "required",
        "type",
    }
)


class SchemaError(ValueError):
    """Raised when a locally-built output_schema would be rejected by TinyFish."""


def validate_schema(schema: Any, path: str = "$") -> None:
    """Reject locally any schema TinyFish would reject, before spending a round trip.

    Catches the documented traps: disallowed keywords, non-object top level,
    ``type: [x, "null"]`` unions, and `enum` without a string type.
    """
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema node must be an object")

    for key in schema:
        if key not in ALLOWED_SCHEMA_KEYWORDS:
            raise SchemaError(
                f"{path}: keyword {key!r} is not in TinyFish's allowlist "
                f"({sorted(ALLOWED_SCHEMA_KEYWORDS)})"
            )

    if path == "$":
        if schema.get("type") != "object":
            raise SchemaError("$: top-level schema must have type 'object'")

    node_type = schema.get("type")
    if isinstance(node_type, list):
        raise SchemaError(
            f"{path}: use nullable: true instead of a type union like {node_type!r}"
        )
    if "enum" in schema and node_type != "string":
        raise SchemaError(f"{path}: enum requires type 'string'")

    for child_path, child in _child_schemas(schema, path):
        validate_schema(child, child_path)


def _child_schemas(schema: dict[str, Any], path: str) -> list[tuple[str, Any]]:
    children: list[tuple[str, Any]] = []
    for name, sub in (schema.get("properties") or {}).items():
        children.append((f"{path}.{name}", sub))
    if "items" in schema:
        children.append((f"{path}[]", schema["items"]))
    for index, branch in enumerate(schema.get("anyOf") or []):
        children.append((f"{path}.anyOf[{index}]", branch))
    return children


def build_output_schema(queries: list[str]) -> dict[str, Any]:
    """Build the structured-output schema for a search run.

    Two constraints shape this beyond the keyword allowlist: the top level must be an
    object, and a list must live in a top-level array property — a schema describing a
    single object returns exactly one object no matter what the goal text asks for.

    ``minItems: 0`` is deliberate. Setting a minimum above zero would apply schema
    pressure on the agent to invent a product to satisfy validation, which is exactly
    the fabrication we are trying to prevent. Empty is explicitly permitted, and an
    empty result is treated as a failure by the caller.
    """
    return {
        "type": "object",
        "properties": {
            "products": {
                "type": "array",
                "minItems": 0,
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "enum": queries},
                        "name": {"type": "string"},
                        "brand": {"type": "string", "nullable": True},
                        "package_size": {"type": "string", "nullable": True},
                        "price_displayed": {"type": "string", "nullable": True},
                        "currency": {"type": "string", "nullable": True},
                        "availability": {
                            "type": "string",
                            "enum": ["in_stock", "out_of_stock", "unknown"],
                        },
                        "product_url": {"type": "string", "nullable": True},
                        # Verbatim on-screen text. Verified in Python against the price,
                        # which makes "never invent a price" a check rather than a promise.
                        "evidence": {"type": "string"},
                    },
                    "required": ["query", "name", "availability", "evidence"],
                    "propertyOrdering": [
                        "query",
                        "name",
                        "brand",
                        "package_size",
                        "price_displayed",
                        "currency",
                        "availability",
                        "product_url",
                        "evidence",
                    ],
                },
            },
            "location_set": {"type": "boolean"},
            "pincode_observed": {"type": "string", "nullable": True},
            "notes": {"type": "string", "nullable": True},
        },
        "required": ["products", "location_set"],
        "propertyOrdering": ["products", "location_set", "pincode_observed", "notes"],
    }


def build_goal(retailer_name: str, pincode: str, queries: list[str]) -> str:
    """Compose the natural-language goal.

    Because the schema cannot carry `description` fields, every piece of field-level
    semantics has to live here instead.
    """
    listed = ", ".join(queries)
    return (
        f"On {retailer_name}, first set the delivery location to pincode {pincode}. "
        "A location or address popup will appear: enter that pincode, wait for the "
        f"suggestions to load, and select the matching locality. Do not proceed until "
        f"the site clearly shows a {pincode} delivery address. "
        f"Then search separately for each of these items: {listed}. "
        "For each search, record at most 5 results visible on the results page. "
        "For every product capture: the exact product name as shown, the brand, the "
        "package size or quantity as shown, the price exactly as printed including the "
        "currency symbol, whether it is in stock or out of stock, and the product URL. "
        "For the 'evidence' field, copy the verbatim on-screen text you read the price "
        "and stock status from — for example the exact price string as displayed. "
        "Report only what is visibly present on the page. If the catalogue is empty, if "
        "a search returns no results, or if any single field is not shown, return null "
        "for that field and leave the products list empty. Never estimate, infer, "
        "average, or recall a price from memory, and never invent a product that is not "
        "on the page. Set availability to 'unknown' unless the page clearly indicates "
        "stock status. Set location_set to false if you could not set the delivery "
        "location. If you encounter a CAPTCHA or a bot-challenge page you cannot pass, "
        "stop and report failure rather than returning an empty result. "
        "Return the results as JSON matching the provided schema."
    )


class TinyFishClient:
    """Thin client over the TinyFish automation API."""

    def __init__(self, settings: Settings, *, verbose: bool = True) -> None:
        self.settings = settings
        self.api_key = require_tinyfish_key(settings)
        self.verbose = verbose
        # `agent_config` (max_steps / max_duration_seconds) is behind a beta flag on
        # some accounts: sending it returns HTTP 403 FORBIDDEN with "not enabled for
        # custom max steps". None = not yet known, so we probe once and remember.
        self._custom_limits_ok: bool | None = None
        # The documented proxy country enum is US/GB/CA/DE/FR/JP/AU — "IN" is not in
        # it and is refused with a 400. Learned the same way.
        self._proxy_ok: bool | None = None
        self._client = httpx.Client(
            base_url=settings.tinyfish_base_url,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    def __enter__(self) -> TinyFishClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            # stderr, not stdout: stdout carries the JSON payload and nothing else, so
            # that `--json-only` can be piped straight into a parser.
            print(f"  [tinyfish] {message}", file=sys.stderr, flush=True)

    def start_run(
        self,
        *,
        url: str,
        goal: str,
        output_schema: dict[str, Any],
        browser_profile: str = "stealth",
        proxy_country: str | None = "IN",
        max_steps: int = 120,
        max_duration_seconds: int = 300,
    ) -> tuple[str | None, dict[str, Any]]:
        """Start an asynchronous run. Returns ``(run_id, response_body)``.

        Two optional request features can be refused by the platform, and each refusal
        has to be recovered from independently — otherwise one retry masks the other
        (dropping the proxy can surface the beta gate, and vice versa). So this walks a
        small ladder, dropping whichever feature the API objects to, and remembers what
        it learns so later runs go straight to the working shape:

          1. as requested
          2. without ``proxy_config``      if HTTP 400 (unsupported country code)
          3. without ``agent_config``      if HTTP 403 (max_steps is beta-gated)
          4. without either

        We deliberately do not retry with a *different* proxy country: a non-Indian
        egress would either be blocked outright or serve a wrong-geo catalogue, and a
        wrong catalogue is worse than no catalogue.
        """
        use_proxy = bool(proxy_country) and self._proxy_ok is not False
        use_agent_config = self._custom_limits_ok is not False
        notes: list[str] = []

        for _ in range(3):
            payload = self._build_payload(
                url=url,
                goal=goal,
                output_schema=output_schema,
                browser_profile=browser_profile,
                proxy_country=proxy_country if use_proxy else None,
                max_steps=max_steps,
                max_duration_seconds=max_duration_seconds,
                include_agent_config=use_agent_config,
            )
            self._log(
                f"POST {RUN_ASYNC_PATH} profile={browser_profile} "
                f"proxy={proxy_country if use_proxy else 'none'} "
                f"agent_config={'yes' if use_agent_config else 'no'}"
            )
            response = self._client.post(RUN_ASYNC_PATH, json=payload)
            body = _safe_json(response)

            if response.status_code < 400:
                self._custom_limits_ok = use_agent_config
                self._proxy_ok = use_proxy
                if notes:
                    body["_request_retries"] = notes
                break

            # max_steps / max_duration_seconds are beta-gated on some accounts.
            if response.status_code == 403 and _is_beta_gate(body) and use_agent_config:
                self._custom_limits_ok = False
                use_agent_config = False
                notes.append("dropped agent_config after HTTP 403 beta gate")
                self._log(
                    "HTTP 403: this account is not enabled for custom max_steps "
                    "(beta). Retrying with the platform defaults."
                )
                continue

            # The documented proxy country enum is US/GB/CA/DE/FR/JP/AU; anything
            # else (IN included) is refused with a 400.
            if response.status_code == 400 and use_proxy:
                self._proxy_ok = False
                use_proxy = False
                notes.append(f"dropped proxy_config after HTTP 400 (country={proxy_country})")
                self._log(
                    f"HTTP 400 with proxy_country={proxy_country!r} — retrying without a proxy"
                )
                continue

            break

        if response.status_code >= 400:
            self._log(f"HTTP {response.status_code} from start_run")
            return None, body

        run_id = _find_run_id(body)
        if run_id is None:
            self._log("start call returned 2xx but no recognisable run id")
        else:
            self._log(f"run_id={run_id} status={body.get('status')}")
        return run_id, body

    def _build_payload(
        self,
        *,
        url: str,
        goal: str,
        output_schema: dict[str, Any],
        browser_profile: str,
        proxy_country: str | None,
        max_steps: int,
        max_duration_seconds: int,
        include_agent_config: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": url,
            "goal": goal,
            "browser_profile": browser_profile,
            "output_schema": output_schema,
        }
        # Only send agent_config if the account is known to accept it, or if we have
        # not yet found out. Sending it blind costs a 403 round trip on gated accounts.
        if include_agent_config and self._custom_limits_ok is not False:
            payload["agent_config"] = {
                "max_steps": max_steps,
                "max_duration_seconds": max_duration_seconds,
            }
        if proxy_country:
            payload["proxy_config"] = {"enabled": True, "country_code": proxy_country}
        return payload

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel a run. Returns the API's response body.

        Worth having because a run can sit in RUNNING indefinitely without reaching a
        terminal status — observed against Blinkit, where a run was still RUNNING after
        22 minutes with no steps reported. Without this, the only options are to keep
        polling or to abandon a run that is still consuming credit.
        """
        try:
            response = self._client.post(f"{RUNS_PATH}/{run_id}/cancel")
            body = _safe_json(response)
        except httpx.HTTPError as exc:
            return {"error": {"code": "TRANSPORT_ERROR", "message": str(exc)}}
        self._log(f"cancel {run_id} -> HTTP {response.status_code} {body.get('status')}")
        return body

    def poll_run(self, run_id: str, *, timeout_seconds: int = 900) -> dict[str, Any]:
        """Poll until the run reaches a terminal status, or the deadline passes."""
        deadline = time.monotonic() + timeout_seconds
        interval = 5.0
        last: dict[str, Any] = {}

        while time.monotonic() < deadline:
            try:
                response = self._client.get(f"{RUNS_PATH}/{run_id}")
                last = _safe_json(response)
            except httpx.HTTPError as exc:
                self._log(f"poll transport error ({exc}); retrying")
                time.sleep(interval)
                continue

            status = last.get("status")
            self._log(f"status={status}")

            if status in TERMINAL_STATUSES:
                return last

            time.sleep(interval)
            interval = min(interval * 1.3, 15.0)

        # Overwrite rather than setdefault: the API returns `"error": null` and
        # `"status": "RUNNING"` explicitly, so setdefault() would leave both in place
        # and the timeout would surface as "RUNNING with no diagnosable error" — which
        # tells the reader nothing and hides that we simply stopped waiting.
        last_status = last.get("status")
        self._log(f"polling timed out after {timeout_seconds}s (last status: {last_status})")
        return {
            **last,
            "status": "TIMEOUT",
            "_last_observed_status": last_status,
            "error": {
                "code": "TIMEOUT",
                "category": "SYSTEM_FAILURE",
                "message": (
                    f"The run was still {last_status} after {timeout_seconds}s of polling, "
                    "so we stopped waiting. It may still be running server-side — the "
                    "run id in this log can be polled again to find out."
                ),
            },
        }

    def search(
        self,
        *,
        retailer: str,
        retailer_url: str,
        pincode: str,
        queries: list[str],
        browser_profile: str = "stealth",
        proxy_country: str | None = "IN",
        max_steps: int = 250,
        max_duration_seconds: int = 480,
    ) -> RetailerResult:
        """Run one search across all queries in a single browser session.

        One run rather than one per query: clearing a bot challenge and setting a
        delivery location is the expensive part of the session, so we pay for it
        once and search repeatedly inside it.
        """
        result = RetailerResult(
            retailer=retailer,
            engine="tinyfish",
            queries=queries,
            status="PENDING",
            delivery_pincode=pincode,
        )

        try:
            run_id, start_body = self.start_run(
                url=retailer_url,
                goal=build_goal(retailer, pincode, queries),
                output_schema=build_output_schema(queries),
                browser_profile=browser_profile,
                proxy_country=proxy_country,
                max_steps=max_steps,
                max_duration_seconds=max_duration_seconds,
            )
        except httpx.HTTPError as exc:
            result.status = "FAILED"
            result.error_code = "TRANSPORT_ERROR"
            result.error_message = f"Could not reach the TinyFish API: {exc}"
            return result

        result.raw["start_response"] = start_body
        result.request_id = start_body.get("request_id")

        # The start call itself can fail (bad key, bad input, no credits).
        if run_id is None:
            error = start_body.get("error") or {}
            result.status = "FAILED"
            result.error_code = error.get("code") or f"HTTP_{start_body.get('_http_status', '?')}"
            result.error_category = error.get("category")
            result.error_message = (
                error.get("message")
                or start_body.get("message")
                or "Start call failed with no error detail."
            )
            return result

        result.run_id = run_id
        final = self.poll_run(run_id, timeout_seconds=max_duration_seconds + 120)
        result.raw["final_response"] = final

        result.status = final.get("status", "FAILED")
        result.num_of_steps = final.get("num_of_steps")
        result.started_at = final.get("started_at")
        result.finished_at = final.get("finished_at")
        result.request_id = final.get("request_id") or result.request_id

        error = final.get("error") or {}
        if error:
            result.error_code = error.get("code")
            result.error_category = error.get("category")
            result.error_message = error.get("message")

        payload = final.get("result")
        if isinstance(payload, dict):
            result.products = _products_from_payload(payload)
            note = payload.get("notes")
            if note:
                result.warnings.append(f"agent note: {note}")

            # Distinguishes "searched, nothing matched" from "searched the wrong place",
            # which are very different problems.
            if payload.get("location_set") is False:
                result.warnings.append(
                    "agent reports it could not set the delivery location — any results "
                    "would be for the wrong location"
                )
                result.status = "FAILED"
                result.error_code = result.error_code or "LOCATION_NOT_SET"
                result.error_message = (
                    result.error_message
                    or "Delivery location was never set, so the catalogue was not reachable."
                )

            observed = payload.get("pincode_observed")
            if observed:
                result.raw["pincode_observed"] = observed
                if pincode not in str(observed):
                    result.warnings.append(
                        f"requested pincode {pincode} but agent observed {observed!r}"
                    )
        elif payload is not None:
            result.warnings.append(
                f"unexpected result type {type(payload).__name__}; expected object"
            )

        # TinyFish can report COMPLETED while the *goal* failed; the result body then
        # carries its own failure marker. Surface that as a real failure.
        if result.status == "COMPLETED" and isinstance(payload, dict):
            if payload.get("status") == "failure" or payload.get("error"):
                result.status = "FAILED"
                result.error_code = result.error_code or "TASK_FAILED"
                result.error_message = (
                    result.error_message
                    or str(payload.get("reason") or payload.get("error") or "goal failed")
                )

        if result.status == "COMPLETED":
            usable = [p for p in result.products if p.is_usable]
            if not usable:
                result.warnings.append(
                    "COMPLETED but no usable product extracted (a name with no price "
                    "and no out-of-stock marker usually means a skeleton page) — "
                    "treating as a failure for fallback purposes."
                )
            else:
                missing = [
                    query
                    for query in queries
                    if not any(p.query == query and p.is_usable for p in usable)
                ]
                if missing:
                    result.raw["queries_without_results"] = missing
                    result.warnings.append(
                        f"partial coverage: no usable result for {missing}"
                    )

        return result

    def retryable(self, result: RetailerResult) -> bool:
        return result.error_code in RETRYABLE_CODES

    def should_fall_back(self, result: RetailerResult) -> bool:
        """Whether the CloakBrowser rescue is worth attempting.

        Note this is intentionally permissive: the single most likely Engine A
        outcome against a challenge-protected, location-gated SPA is a well-formed
        ``COMPLETED`` with an empty product list, and treating that as success would
        let a silent block masquerade as a pass.
        """
        if result.succeeded and any(p.is_usable for p in result.products):
            return False
        if result.error_code in FATAL_CODES:
            return False
        return True


def _find_run_id(body: dict[str, Any]) -> str | None:
    """Find a run id anywhere in a response, tolerating shape differences.

    The documented field is ``run_id``, but response envelopes vary. Walking the tree
    for an id-like key costs nothing and avoids a hardcoding bug that would be
    painful to debug mid-hackathon.
    """
    for key in ("run_id", "id", "runId"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value

    for value in body.values():
        if isinstance(value, dict):
            nested = _find_run_id(value)
            if nested:
                return nested

    return None


def _products_from_payload(payload: dict[str, Any]) -> list[Product]:
    """Convert the agent's JSON payload into validated Product objects."""
    raw_items = payload.get("products")
    if not isinstance(raw_items, list):
        return []

    products: list[Product] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        product = Product(
            query=str(item.get("query") or ""),
            name=str(item.get("name") or ""),
            availability=str(item.get("availability") or "unknown"),
            brand=item.get("brand"),
            package_size=item.get("package_size"),
            price_displayed=item.get("price_displayed"),
            price_numeric=item.get("price_numeric"),
            currency=item.get("currency"),
            product_url=item.get("product_url"),
            evidence=item.get("evidence"),
            source="tinyfish",
        )
        product.validate()
        if product.is_valid:
            products.append(product)
    return products


def _is_beta_gate(body: dict[str, Any]) -> bool:
    """True if a 403 is the documented 'custom max steps is beta-gated' refusal.

    Matched on the message rather than the code, because the code is a generic
    FORBIDDEN that could mean other things.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    message = str(error.get("message", "")).lower()
    return "max steps" in message or "beta" in message


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    """Parse a response body as JSON, never raising on malformed content."""
    try:
        body = response.json()
    except ValueError:
        return {"_http_status": response.status_code, "message": response.text[:2000]}
    if isinstance(body, dict):
        body.setdefault("_http_status", response.status_code)
        return body
    return {"_http_status": response.status_code, "body": body}
