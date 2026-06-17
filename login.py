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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import sys
import tomllib
from collections.abc import Callable, Iterator
from http.cookies import Morsel
import time
from typing import Any, NamedTuple, Optional, TextIO

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


def _cookie_expiry(cookie_obj: Any) -> Optional[datetime]:
    """Best-effort absolute expiry (UTC) of one cookie object, or None.

    Handles an ``http.cookies.Morsel`` (``expires`` HTTP-date, else relative
    ``max-age``) and an ``http.cookiejar.Cookie``-like object (``.expires`` as
    epoch seconds). A pure session cookie (no expiry attribute) returns None.
    """
    if isinstance(cookie_obj, Morsel):
        expires = cookie_obj.get("expires")
        if expires:
            try:
                parsed = parsedate_to_datetime(expires)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        max_age = cookie_obj.get("max-age")
        if max_age:
            try:
                return datetime.now(timezone.utc) + timedelta(seconds=int(max_age))
            except (TypeError, ValueError):
                return None
        return None
    epoch = getattr(cookie_obj, "expires", None)
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    return None


def _iter_cookie_objects(cookies: Any) -> Iterator[tuple[str, Any]]:
    """Like ``_iter_cookie_pairs`` but yields (name, cookie_object) so callers
    can read per-cookie attributes such as expiry. A plain ``{name: value}``
    dict carries no object to inspect, so its value is yielded as ``None``.
    """
    if isinstance(cookies, (Morsel, dict)):
        cookies = [cookies]
    for cookie in cookies or []:
        if isinstance(cookie, Morsel):
            yield cookie.key, cookie
        elif isinstance(cookie, dict):
            for name, value in cookie.items():
                yield (value.key, value) if isinstance(value, Morsel) else (name, None)
        else:
            name = getattr(cookie, "name", None)
            if name is not None:
                yield name, cookie


def earliest_target_expiry(cookies: Any) -> Optional[datetime]:
    """Earliest absolute expiry (UTC) among the captured target cookies, or None.

    The session dies when the first required cookie expires, so the earliest is
    the one worth reporting.
    """
    expiries: list[datetime] = []
    for name, obj in _iter_cookie_objects(cookies):
        if name in TARGET_COOKIES and obj is not None:
            expiry = _cookie_expiry(obj)
            if expiry is not None:
                expiries.append(expiry)
    return min(expiries) if expiries else None


def format_expiry(expiry: Optional[datetime]) -> str:
    """Human description of a captured-cookie expiry for the console."""
    if expiry is None:
        return "unknown (this backend did not report a cookie expiry)"
    days = (expiry - datetime.now(timezone.utc)).days
    return f"{expiry:%Y-%m-%d %H:%M UTC} (~{days} days from now)"


class CaptureResult(NamedTuple):
    cookies: dict[str, str]
    expiry: Optional[datetime]  # earliest absolute expiry of the captured cookies (UTC)


def capture_cookies(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    start_url: str = START_URL,
    message_stream: TextIO = sys.stderr,
) -> Optional[CaptureResult]:
    """Open the login window and poll until all three cookies are captured.

    Returns a :class:`CaptureResult` (the ``{name: value}`` dict plus the
    earliest cookie expiry), or ``None`` (pywebview missing, timeout, or the
    window closed before login completed).
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

    captured: dict[str, Any] = {"cookies": None, "expiry": None}

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
                captured["expiry"] = earliest_target_expiry(cookies)
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
    webview.start(_poll, (window,))
    if captured["cookies"] is None:
        return None
    return CaptureResult(cookies=captured["cookies"], expiry=captured["expiry"])


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

    result = capture_cookies()
    if result is None:
        print("login: no cookies captured (window closed or timed out). Nothing written.", file=sys.stderr)
        return 1
    cookies = result.cookies

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
    print(f"login: these cookies are set to expire {format_expiry(result.expiry)}.", file=sys.stderr)
    print("login: restart the monitor (app.py) to pick up the new cookies.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
