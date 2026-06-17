"""Interactive login: capture the Buckler session cookies without DevTools.

Opens an embedded browser at the CFN/Buckler site; you log in normally (MFA and
any captcha are handled natively by the Capcom ID page). The three session
cookies — including the ``httpOnly`` ``buckler_id`` that ``document.cookie``
cannot see — are read from the webview's native cookie store, verified against
the real API, and written into ``config.toml``. The credential never leaves the
machine, and cookie *values* are never logged (only names, for diagnostics).

Run:

    uv sync --group login        # one-time: install the optional browser dep
    uv run python login.py

Or, for a one-off without changing the synced env:

    uv run --with "pywebview>=5" python login.py

After it succeeds, **restart the monitor** (``app.py``) — cookies are read once
at startup. See docs/LOGIN_CAPTURE_PROPOSAL.md for the full design.
"""

from __future__ import annotations

import dataclasses
import sys
import tomllib
from collections.abc import Callable, Iterator
from http.cookies import Morsel
import time
from typing import Any, Optional, TextIO

from pydantic import ValidationError

from api_service import AuthExpiredError, get_character_win_rates
from config import ConfigData, load_config, update_buckler_cookies
from model import WinRateResponse

START_URL = "https://www.streetfighter.com/6/buckler/en/"
TARGET_COOKIES = ("buckler_id", "buckler_r_id", "buckler_praise_date")
POLL_INTERVAL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 300.0

WinRateFetcher = Callable[[ConfigData], WinRateResponse]


def _iter_cookie_pairs(cookies: Any) -> Iterator[tuple[str, Optional[str]]]:
    """Yield (name, value) across the shapes pywebview backends return.

    WebView2 (Windows) returns a list of ``http.cookies.SimpleCookie`` (each a
    dict of name -> Morsel); other backends may return ``cookiejar.Cookie``
    objects, a bare Morsel, or a plain ``{name: value}`` dict.
    """
    if isinstance(cookies, (Morsel, dict)):
        cookies = [cookies]
    for cookie in cookies or []:
        if isinstance(cookie, Morsel):
            yield cookie.key, cookie.value
        elif isinstance(cookie, dict):
            for name, value in cookie.items():
                if isinstance(value, Morsel):
                    yield value.key, value.value
                else:
                    yield name, value
        else:
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if name is not None:
                yield name, value


def extract_cookies(cookies: Any) -> dict[str, str]:
    """Return the present, non-empty target cookies as ``{name: value}``."""
    found: dict[str, str] = {}
    for name, value in _iter_cookie_pairs(cookies):
        if name in TARGET_COOKIES and value:
            found[name] = value
    return found


def capture_cookies(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    start_url: str = START_URL,
    message_stream: TextIO = sys.stderr,
) -> Optional[dict[str, str]]:
    """Open the login window and poll until all three cookies are captured.

    Returns the captured ``{name: value}`` dict, or ``None`` (pywebview missing,
    timeout, or the window closed before login completed).
    """
    try:
        import webview  # type: ignore[import-not-found]  # optional dependency (see --group login)
    except ImportError:
        print(
            "login needs the optional dependency 'pywebview'.\n"
            "  install it with:  uv sync --group login   (or: uv run --with \"pywebview>=5\" python login.py)\n"
            "  Until then, capture the cookies manually: log into the CFN site, open\n"
            "  DevTools -> Network, and copy buckler_id / buckler_r_id /\n"
            "  buckler_praise_date from a request's Cookie header into config.toml.",
            file=message_stream,
        )
        return None

    captured: dict[str, Optional[dict[str, str]]] = {"cookies": None}

    def _poll(window: Any) -> None:
        deadline = time.time() + timeout
        last_names: list[str] = []
        while time.time() < deadline:
            try:
                cookies = window.get_cookies()
            except Exception as error:  # pylint: disable=broad-exception-caught
                # The backend may not be ready to serve cookies yet.
                print(f"login: (get_cookies not ready yet: {error})", file=message_stream)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Log the set of cookie NAMES (never values) when it changes, so a
            # failure on some other backend is diagnosable.
            names = sorted({name for name, _ in _iter_cookie_pairs(cookies) if name})
            if names != last_names:
                last_names = names
                print(f"login: cookies visible now: {names or '(none)'}", file=message_stream)

            found = extract_cookies(cookies)
            if len(found) == len(TARGET_COOKIES):
                captured["cookies"] = found
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        try:
            window.destroy()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    print(
        "login: opening the CFN/Buckler login window -- log in normally; it closes "
        "automatically once your cookies are captured.",
        file=message_stream,
    )
    window = webview.create_window("Log in to CFN / Buckler", start_url)
    webview.start(_poll, window)
    return captured["cookies"]


def verify_cookies(
    config: ConfigData,
    cookies: dict[str, str],
    *,
    win_rate_fetcher: Optional[WinRateFetcher] = None,
) -> str:
    """Probe the real API with the captured cookies.

    Returns ``"verified"`` (the API accepted them), ``"rejected"`` (auth
    expired — the capture is unusable, do not write), or ``"unverified"`` (some
    other error, e.g. the network was down — the cookies are probably fine).
    """
    fetcher = get_character_win_rates if win_rate_fetcher is None else win_rate_fetcher
    verify_config = dataclasses.replace(
        config,
        buckler_id=cookies["buckler_id"],
        buckler_r_id=cookies["buckler_r_id"],
        buckler_praise_date=int(cookies["buckler_praise_date"]),
    )
    try:
        fetcher(verify_config)
    except AuthExpiredError:
        return "rejected"
    except Exception:  # pylint: disable=broad-exception-caught
        return "unverified"
    return "verified"


def main() -> int:
    try:
        config = load_config()
    except FileNotFoundError:
        print(
            "login: config.toml not found. Copy example.toml to config.toml and set "
            "user_code / target_season_id first, then re-run.",
            file=sys.stderr,
        )
        return 1
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        # login.py only needs user_code / target_season_id from config.toml; the
        # cookie fields it is about to overwrite must still be present and the
        # right shape for the file to load (buckler_praise_date is an int, so it
        # cannot be left blank). Guide the user instead of dumping a traceback.
        print(
            "login: config.toml is invalid, so login cannot start. Set user_code and "
            "target_season_id, and keep the buckler_* fields present with "
            "buckler_praise_date numeric (the example.toml placeholders are fine). "
            f"Then re-run.\n  details: {error}",
            file=sys.stderr,
        )
        return 1

    cookies = capture_cookies()
    if cookies is None:
        print("login: no cookies captured (window closed or timed out). Nothing written.", file=sys.stderr)
        return 1

    try:
        praise_date = int(cookies["buckler_praise_date"])
    except ValueError:
        print(
            "login: captured buckler_praise_date is not numeric; aborting without writing.",
            file=sys.stderr,
        )
        return 1

    status = verify_cookies(config, cookies)
    if status == "rejected":
        print(
            "login: the captured cookies were rejected by the API (login may not have "
            "completed). Nothing written -- try again.",
            file=sys.stderr,
        )
        return 1

    update_buckler_cookies(cookies["buckler_id"], cookies["buckler_r_id"], praise_date)
    if status == "unverified":
        print(
            "login: wrote the cookies to config.toml but could NOT verify them (network "
            "error?). If the monitor still alerts, re-run login.",
            file=sys.stderr,
        )
    else:
        print("login: captured and verified cookies -> wrote them to config.toml.", file=sys.stderr)
    print("login: restart the monitor (app.py) to pick up the new cookies.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
