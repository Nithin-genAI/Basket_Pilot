"""Configuration loading for BasketPilot.

Secrets live in ``.env`` at the project root and are never read from anywhere else.
Configuration problems raise :class:`ConfigError` immediately rather than surfacing
later as a confusing empty result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
RUNS_DIR = PROJECT_ROOT / "runs"

DEFAULT_BASE_URL = "https://agent.tinyfish.ai"

# TinyFish automation endpoints (see https://docs.tinyfish.ai/key-concepts/endpoints).
RUN_SYNC_PATH = "/v1/automation/run"
RUN_ASYNC_PATH = "/v1/automation/run-async"
RUNS_PATH = "/v1/runs"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    tinyfish_api_key: str | None
    tinyfish_base_url: str
    runs_dir: Path

    @property
    def has_tinyfish_key(self) -> bool:
        return bool(self.tinyfish_api_key)


def load_settings() -> Settings:
    """Load settings from the environment, reading ``.env`` if present.

    A missing ``TINYFISH_API_KEY`` is *not* an error here. Engine B (CloakBrowser)
    does not need it, so ``--engine cloak`` must still work without a key. Callers
    that require the key ask for it explicitly via :func:`require_tinyfish_key`.
    """
    load_dotenv(ENV_PATH)

    key = (os.environ.get("TINYFISH_API_KEY") or "").strip()
    base_url = (os.environ.get("TINYFISH_BASE_URL") or "").strip() or DEFAULT_BASE_URL

    return Settings(
        tinyfish_api_key=key or None,
        tinyfish_base_url=base_url.rstrip("/"),
        runs_dir=RUNS_DIR,
    )


def require_tinyfish_key(settings: Settings) -> str:
    """Return the API key or explain precisely how to supply one."""
    if not settings.has_tinyfish_key:
        raise ConfigError(
            "TINYFISH_API_KEY is not set, so the TinyFish engine cannot run.\n"
            "\n"
            f"Fix it by creating {ENV_PATH} with:\n"
            "\n"
            "    TINYFISH_API_KEY=sk-tinyfish-...\n"
            "\n"
            "Get a key at https://agent.tinyfish.ai/api-keys (new accounts include $8 free credit).\n"
            f"A template is committed at {PROJECT_ROOT / '.env.example'} — copy it with:\n"
            "\n"
            "    cp .env.example .env\n"
            "\n"
            "Alternatively run with --engine cloak to use the local stealth browser, "
            "which needs no API key."
        )
    return settings.tinyfish_api_key  # type: ignore[return-value]
