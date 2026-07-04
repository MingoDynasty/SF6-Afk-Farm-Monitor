"""Cookie extraction and verification logic for login.py.

The interactive browser capture (`capture_cookies`) is not unit-testable, but
the pure logic around it — pulling the three cookies out of whatever shape a
webview backend returns, and classifying the verification probe — is.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from http.cookies import SimpleCookie

import pytest

import login
from api_service import AuthExpiredError
from config import ConfigData
from login import extract_cookies, target_cookie_expiries, verify_cookies
from model import WinRateResponse

CAPTURED = {"buckler_id": "id", "buckler_r_id": "r", "buckler_praise_date": "123"}


class _CookiejarCookie:
    """Stand-in for an http.cookiejar.Cookie (has .name / .value / .expires)."""

    def __init__(self, name: str, value: str, expires: int | None = None) -> None:
        self.name = name
        self.value = value
        self.expires = expires


def _simplecookies(pairs: Iterable[tuple[str, str]]) -> list[SimpleCookie]:
    """A list of one-morsel SimpleCookies, as WebView2 returns them."""
    result = []
    for name, value in pairs:
        cookie: SimpleCookie = SimpleCookie()
        cookie[name] = value
        result.append(cookie)
    return result


def test_extract_cookies_from_simplecookie_list() -> None:
    cookies = _simplecookies(
        [
            ("buckler_id", "id"),
            ("buckler_r_id", "r"),
            ("buckler_praise_date", "123"),
            ("other", "ignored"),
        ]
    )
    assert extract_cookies(cookies) == CAPTURED


def test_extract_cookies_from_cookiejar_objects() -> None:
    cookies = [
        _CookiejarCookie("buckler_id", "id"),
        _CookiejarCookie("buckler_r_id", "r"),
        _CookiejarCookie("buckler_praise_date", "123"),
    ]
    assert extract_cookies(cookies) == CAPTURED


def test_extract_cookies_skips_empty_and_partial() -> None:
    cookies = _simplecookies([("buckler_id", "id"), ("buckler_r_id", "")])
    # buckler_r_id is empty (excluded) and buckler_praise_date is absent.
    assert extract_cookies(cookies) == {"buckler_id": "id"}


def test_extract_cookies_handles_none() -> None:
    assert extract_cookies(None) == {}


def test_verify_cookies_verified(make_config: Callable[..., ConfigData]) -> None:
    seen: dict[str, ConfigData] = {}

    def fetcher(config: ConfigData) -> WinRateResponse:
        seen["config"] = config
        return WinRateResponse(character_win_rates=[])

    assert (
        verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "verified"
    )
    # The probe runs with the captured cookies, praise_date coerced to int.
    assert seen["config"].buckler_id == "id"
    assert seen["config"].buckler_praise_date == 123


def test_verify_cookies_rejected(make_config: Callable[..., ConfigData]) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise AuthExpiredError("expired")

    assert (
        verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "rejected"
    )


def test_verify_cookies_unverified_on_other_error(
    make_config: Callable[..., ConfigData],
) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("network down")

    assert (
        verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher)
        == "unverified"
    )


def _fail_if_called(*args: object, **kwargs: object) -> None:
    raise AssertionError("the browser must not open when config.toml is unusable")


def test_main_missing_config_returns_1_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing() -> ConfigData:
        raise FileNotFoundError

    monkeypatch.setattr(login, "load_config", raise_missing)
    monkeypatch.setattr(login, "capture_cookies", _fail_if_called)
    assert login.main() == 1


def test_main_invalid_config_returns_1_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A blank/missing buckler_praise_date is the documented foot-gun: the int
    # field fails validation, and login must report it rather than crash or open
    # the browser.
    def raise_invalid() -> ConfigData:
        ConfigData()  # type: ignore[call-arg]  # missing required fields -> ValidationError
        raise AssertionError("unreachable")

    monkeypatch.setattr(login, "load_config", raise_invalid)
    monkeypatch.setattr(login, "capture_cookies", _fail_if_called)
    assert login.main() == 1


def test_target_cookie_expiries_parses_each_target_cookie() -> None:
    id_cookie: SimpleCookie = SimpleCookie()
    id_cookie["buckler_id"] = "x"
    id_cookie["buckler_id"]["expires"] = "Wed, 09 Jun 2027 10:00:00 GMT"
    r_cookie: SimpleCookie = SimpleCookie()
    r_cookie["buckler_r_id"] = "y"
    r_cookie["buckler_r_id"]["expires"] = "Mon, 09 Jun 2025 10:00:00 GMT"
    # A non-target cookie must be ignored even though it has an expiry.
    other: SimpleCookie = SimpleCookie()
    other["_ga"] = "z"
    other["_ga"]["expires"] = "Sun, 09 Jun 2024 10:00:00 GMT"

    assert target_cookie_expiries([id_cookie, r_cookie, other]) == {
        "buckler_id": datetime(2027, 6, 9, 10, 0, tzinfo=timezone.utc),
        "buckler_r_id": datetime(2025, 6, 9, 10, 0, tzinfo=timezone.utc),
    }


def test_target_cookie_expiries_from_cookiejar_epoch() -> None:
    epoch = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
    cookies = [_CookiejarCookie("buckler_id", "x", expires=epoch)]
    assert target_cookie_expiries(cookies) == {
        "buckler_id": datetime(2026, 7, 1, tzinfo=timezone.utc)
    }


def test_target_cookie_expiries_omits_session_cookie() -> None:
    # SimpleCookie morsels default the expires attribute to "" (a session cookie).
    assert target_cookie_expiries(_simplecookies([("buckler_id", "x")])) == {}


def test_format_until_uses_hours_under_two_days() -> None:
    now = datetime(2026, 6, 17, 7, 10, tzinfo=timezone.utc)
    expiry = datetime(2026, 6, 18, 1, 10, tzinfo=timezone.utc)  # 18 h later
    assert login._format_until(expiry, now=now) == "in ~18 h"


def test_format_until_uses_days_beyond_two_days() -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    expiry = datetime(2026, 7, 1, tzinfo=timezone.utc)  # 30 days later
    assert login._format_until(expiry, now=now) == "in ~30 days"


def test_format_until_reports_expired() -> None:
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)
    expiry = datetime(2026, 6, 17, tzinfo=timezone.utc)
    assert login._format_until(expiry, now=now) == "expired"
