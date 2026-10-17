"""Warm a TinyFish Browser Context Profile by hand, then save it for reuse.

Why this exists
---------------
Blinkit sits behind Cloudflare, and it fires a Turnstile challenge on the *search*
navigation rather than only on landing. A cold browser clears it only sometimes, which
is why the same query succeeds on one run and returns nothing on the next. A profile
that has already passed the challenge once — and already has the delivery pincode set —
starts from a state that does not need to clear it again.

TinyFish's documented workflow for this is three calls:

    POST {base}/v1/profiles                      -> create, get profile_id
    POST {base}/v1/profiles/{id}/setup-session   -> {session_id, cdp_url, ...}
    POST {base}/v1/profiles/{id}/save            -> persist whatever the session holds

The middle step is the point: it hands back a ``cdp_url`` for a live browser, and the
human logs in through *that*. This tool never sees, asks for, or logs a password — the
login happens in the browser TinyFish is running, between you and the retailer.

Usage
-----
    python tools/setup_profile.py create --name blinkit-560001
    python tools/setup_profile.py connect --profile-id <id>     # log in in the browser
    python tools/setup_profile.py save --profile-id <id> --session-id <sid>
    python tools/setup_profile.py list
    python tools/setup_profile.py show --profile-id <id>

``connect`` prints a ``cdp_url`` and, when a local Playwright is installed, offers to
open a visible browser attached to it so you can log in and set the pincode normally.
It waits for you to press Enter, then tells you the session id to pass to ``save``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from basketpilot.config import load_settings  # noqa: E402

# The profile API lives on the agent host. The Browser API (api.browser.tinyfish.ai)
# creates anonymous sessions; profiles belong to the account and are created here.
DEFAULT_BASE = "https://agent.tinyfish.ai"
TIMEOUT = httpx.Timeout(90.0, connect=20.0)


def base_url() -> str:
    return (os.environ.get("TINYFISH_BASE_URL") or DEFAULT_BASE).rstrip("/")


def api_key() -> str:
    settings = load_settings()
    key = settings.tinyfish_api_key
    if not key:
        print("TINYFISH_API_KEY is not set. Add it to .env first.", file=sys.stderr)
        raise SystemExit(2)
    return key


def _headers() -> dict[str, str]:
    return {"X-API-Key": api_key(), "Content-Type": "application/json"}


def _explain(response: httpx.Response) -> str:
    """Turn a status code into the thing the user actually needs to know."""
    if response.status_code == 404:
        return (
            "404 — the profiles API is not enabled on this account, or the path is "
            "wrong. Profiles are a separate surface from the Browser API; if the "
            "Browser API works and this 404s, the feature is gated."
        )
    if response.status_code == 401:
        return "401 — the API key was rejected."
    if response.status_code == 402:
        return "402 — insufficient credit for a browser session."
    return f"HTTP {response.status_code}: {response.text[:400]}"


def _request(method: str, path: str, **kwargs) -> dict:
    url = f"{base_url()}{path}"
    try:
        response = httpx.request(method, url, headers=_headers(), timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        print(f"{method} {url} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if response.status_code >= 400:
        print(f"{method} {url} -> {_explain(response)}", file=sys.stderr)
        raise SystemExit(1)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:2000]}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_list(_: argparse.Namespace) -> int:
    payload = _request("GET", "/v1/profiles")
    profiles = payload.get("profiles", payload if isinstance(payload, list) else [])
    if not profiles:
        print("No profiles on this account.")
        return 0
    print(f"{'profile_id':<40} {'name':<28} updated")
    for profile in profiles:
        print(
            f"{profile.get('id') or profile.get('profile_id', '?'):<40} "
            f"{str(profile.get('name', '')):<28} {profile.get('updated_at', '')}"
        )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    body: dict = {"name": args.name}
    if args.default:
        body["set_as_default"] = True
    payload = _request("POST", "/v1/profiles", json=body)
    profile_id = payload.get("id") or payload.get("profile_id")
    print(json.dumps(payload, indent=2))
    if profile_id:
        print(f"\nprofile_id = {profile_id}")
        print(f"next:  python {Path(__file__).name} connect --profile-id {profile_id}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    print(json.dumps(_request("GET", f"/v1/profiles/{args.profile_id}"), indent=2))
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    """Open a setup session and let the human log in through it."""
    body: dict = {}
    if args.url:
        body["url"] = args.url
    payload = _request("POST", f"/v1/profiles/{args.profile_id}/setup-session", json=body)

    session_id = payload.get("session_id") or payload.get("id")
    cdp_url = payload.get("cdp_url")
    print(json.dumps(payload, indent=2))

    if not cdp_url:
        print(
            "\nNo cdp_url came back, so there is nothing to attach to. The setup "
            "session may not be enabled for this account — see the response above.",
            file=sys.stderr,
        )
        return 1

    print(
        "\n" + "=" * 74 + "\n"
        "LOG IN THROUGH THIS BROWSER — I never see your password.\n"
        + "=" * 74 + "\n"
        f"cdp_url    : {cdp_url}\n"
        f"session_id : {session_id}\n\n"
        "Do this in the window that opens:\n"
        "  1. Go to https://blinkit.com/\n"
        "  2. Set the delivery location to pincode 560001\n"
        "  3. Let the page settle until products are visible\n"
        "  4. Come back here and press Enter\n\n"
        "If no window opens, attach your own Playwright client to the cdp_url above:\n"
        "  from playwright.sync_api import sync_playwright\n"
        "  with sync_playwright() as p:\n"
        "      browser = p.chromium.connect_over_cdp(cdp_url)\n"
        "      page = browser.contexts[0].pages[0]\n"
        "      page.goto('https://blinkit.com/')\n"
        "      input('log in, set 560001, then press Enter')\n"
        "      # do NOT call browser.close() — it would close TinyFish's browser\n"
    )

    if not args.no_browser:
        _open_visible_browser(cdp_url)

    try:
        input("\nPress Enter once you are logged in and 560001 is set... ")
    except (EOFError, KeyboardInterrupt):
        print("\n(no input — continuing)")

    print(
        f"\nNow save what the session holds:\n"
        f"  python {Path(__file__).name} save --profile-id {args.profile_id} "
        f"--session-id {session_id}\n"
    )
    return 0


def _open_visible_browser(cdp_url: str) -> None:
    """Attach a visible local browser to the setup session, if Playwright is present."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("(Playwright is not installed locally; use the cdp_url manually.)")
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://blinkit.com/", wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:  # noqa: BLE001
                print(f"(could not pre-navigate: {type(exc).__name__}: {exc})")
            print("A browser window is open on the TinyFish session. Log in there.")
            # Deliberately no browser.close(): this is a CDP connection to TinyFish's
            # browser, and closing it would tear down the very session we are warming.
    except Exception as exc:  # noqa: BLE001
        print(f"(could not open a local browser: {type(exc).__name__}: {exc})")


def cmd_save(args: argparse.Namespace) -> int:
    payload = _request(
        "POST",
        f"/v1/profiles/{args.profile_id}/save",
        json={"session_id": args.session_id},
    )
    print(json.dumps(payload, indent=2))
    print(
        f"\nSaved. profile_id = {args.profile_id}\n"
        "Reuse it with:\n"
        "  python -m basketpilot compare --list \"2L Amul milk, a dozen eggs, brown bread\" \\\n"
        f"    --pincode 560001 --retailer blinkit --engine tinyfish_browser \\\n"
        f"    --profile-id {args.profile_id}\n"
        "\nNote: the compare command's --profile-id support must be present for this to "
        "take effect; if it is not yet wired through, the run will say so rather than "
        "silently ignoring the flag."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list profiles on this account")
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="create a new Browser Context Profile")
    p_create.add_argument("--name", required=True, help="a human label, e.g. blinkit-560001")
    p_create.add_argument("--default", action="store_true", help="set as the account default")
    p_create.set_defaults(func=cmd_create)

    p_show = sub.add_parser("show", help="show one profile")
    p_show.add_argument("--profile-id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_connect = sub.add_parser("connect", help="open a setup session and log in by hand")
    p_connect.add_argument("--profile-id", required=True)
    p_connect.add_argument("--url", default="https://blinkit.com/")
    p_connect.add_argument("--no-browser", action="store_true",
                           help="print the cdp_url instead of opening a local browser")
    p_connect.set_defaults(func=cmd_connect)

    p_save = sub.add_parser("save", help="persist the warmed session into the profile")
    p_save.add_argument("--profile-id", required=True)
    p_save.add_argument("--session-id", required=True)
    p_save.set_defaults(func=cmd_save)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
