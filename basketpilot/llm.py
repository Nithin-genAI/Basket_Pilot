"""The optional LLM tier, backed by Fireworks AI.

Fireworks serves an OpenAI-compatible chat-completions API, so this is a thin
``httpx`` client rather than a vendor SDK — one fewer dependency, and the request
shape is visible in the code.

The design rule that matters: **this module is optional and its absence is not an
error.** Every function returns ``None`` when no key is configured or when the call
fails, and every caller falls back to a deterministic path. That is what makes the
pipeline work with no key at all, which is the state the project is in today.

The second rule: **the LLM never computes money.** It may split a messy shopping list
into items and it may suggest which of two similar products the user meant. It never
decides a basket, a total, or a ranking — those come from
:mod:`basketpilot.optimizer`, which is pure arithmetic and testable without a network.

Configure with ``FIREWORKS_API_KEY`` in ``.env``. ``FIREWORKS_MODEL`` overrides the
model; the default is a small instruct model that is sufficient for list parsing and
is cheap enough to call per request.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import httpx

FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"

# A small, fast, cheap instruct model. Overridable, because model availability on
# Fireworks changes and a pinned name that has been retired should be a one-line fix
# rather than a code change.
DEFAULT_MODEL = "accounts/fireworks/models/llama-v3p1-8b-instruct"

# Deliberately short. This tier resolves ambiguity; it does not do long reasoning,
# and a slow call would block a pipeline whose deterministic path already works.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_TOKENS = 1024


def api_key() -> str | None:
    key = os.environ.get("FIREWORKS_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return key or None


def model_name() -> str:
    return os.environ.get("FIREWORKS_MODEL", "").strip() or DEFAULT_MODEL


def available() -> bool:
    return api_key() is not None


def complete(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = MAX_TOKENS,
    temperature: float = 0.0,
) -> str | None:
    """One chat completion. Returns ``None`` on any failure, never raises.

    Temperature defaults to 0 because every use here is an extraction or a
    classification, where reproducibility is worth more than variety.
    """
    key = api_key()
    if not key:
        return None

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            response = client.post(
                FIREWORKS_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "model": model_name(),
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
    except httpx.HTTPError as exc:
        print(f"  [llm] request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    if response.status_code != 200:
        # Say what went wrong, but never echo the key or the full request.
        print(
            f"  [llm] Fireworks returned HTTP {response.status_code}: {response.text[:200]}",
            file=sys.stderr,
        )
        return None

    try:
        payload = response.json()
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        print(f"  [llm] could not read the response: {type(exc).__name__}", file=sys.stderr)
        return None


def complete_json(prompt: str, *, system: str | None = None, **kwargs: Any) -> Any | None:
    """A completion parsed as JSON, tolerating the usual model chatter around it.

    Models wrap JSON in prose or fences often enough that a strict ``json.loads`` on
    the raw text fails on responses that are perfectly good. The salvage here is
    narrow — strip fences, take the outermost object or array — rather than a
    general-purpose repair, because guessing at malformed JSON is how a parser starts
    inventing data.
    """
    raw = complete(prompt, system=system, **kwargs)
    if not raw:
        return None
    return parse_json_loose(raw)


def parse_json_loose(raw: str) -> Any | None:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None
