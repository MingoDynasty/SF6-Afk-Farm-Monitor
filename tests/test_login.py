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
from login import earliest_target_expiry, extract_cookies, verify_cookies
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

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "verified"
    # The probe runs with the captured cookies, praise_date coerced to int.
    assert seen["config"].buckler_id == "id"
    assert seen["config"].buckler_praise_date == 123


def test_verify_cookies_rejected(make_config: Callable[..., ConfigData]) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise AuthExpiredError("expired")

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "rejected"


def test_verify_cookies_unverified_on_other_error(
    make_config: Callable[..., ConfigData],
) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("network down")

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "unverified"


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


def test_earliest_target_expiry_picks_soonest_target_cookie() -> None:
    later: SimpleCookie = SimpleCookie()
    later["buckler_id"] = "x"
    later["buckler_id"]["expires"] = "Wed, 09 Jun 2027 10:00:00 GMT"
    sooner: SimpleCookie = SimpleCookie()
    sooner["buckler_r_id"] = "y"
    sooner["buckler_r_id"]["expires"] = "Mon, 09 Jun 2025 10:00:00 GMT"
    # A non-target cookie expiring even sooner must be ignored.
    other: SimpleCookie = SimpleCookie()
    other["_ga"] = "z"
    other["_ga"]["expires"] = "Sun, 09 Jun 2024 10:00:00 GMT"

    assert earliest_target_expiry([later, sooner, other]) == datetime(
        2025, 6, 9, 10, 0, tzinfo=timezone.utc
    )


def test_earliest_target_expiry_from_cookiejar_epoch() -> None:
    epoch = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
    cookies = [_CookiejarCookie("buckler_id", "x", expires=epoch)]
    assert earliest_target_expiry(cookies) == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_earliest_target_expiry_none_when_no_expiry() -> None:
    # SimpleCookie morsels default the expires attribute to "" (a session cookie).
    assert earliest_target_expiry(_simplecookies([("buckler_id", "x")])) is None
